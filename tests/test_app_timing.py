"""Testes da sincronizacao entre o tempo simulado e o relogio real."""

import tempfile
import inspect
from pathlib import Path
import unittest
from unittest.mock import patch

from m231pro.ui.app import (
    TestProLabApp,
    acquired_samples,
    chart_position_marks,
    format_chart_force,
    format_csv_number,
    machine_connection_presentation,
    maximize_main_window,
    remaining_delay_ms,
    spaced_chart_marks,
    write_results_csv,
)
from m231pro.core.simulation import Sample, compression_return_template
from m231pro.hardware.backend import MachineState
from m231pro.hardware.backend import MachineTelemetry
from m231pro.hardware.ftdi import FtdiDevice, FtdiProbeResult


class TestAppTiming(unittest.TestCase):
    def test_run_mode_column_shows_only_online_or_offline(self):
        self.assertEqual(
            "Offline",
            TestProLabApp._display_run_mode("Local / maquina offline"),
        )
        self.assertEqual(
            "Online",
            TestProLabApp._display_run_mode("Controlador M231 FTDI nativo (Python)"),
        )

    def test_session_delete_stops_current_run_without_deleting_saved_runs(self):
        class Variable:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Tree:
            rows = {
                "current-run": ("Atual", "data", "Atual", "local", "ativo", 2),
                "run-7": (7, "data", "Ensaio A", "local", "concluído", 10),
                "run-8": (8, "data", "Ensaio B", "local", "concluído", 12),
            }

            def selection(self):
                return tuple(self.rows)

            def item(self, item, option=None, **_kwargs):
                if option == "values":
                    return self.rows[item]
                return {"values": self.rows[item]}

        class Machine:
            def __init__(self):
                self.stop_count = 0

            def stop(self):
                self.stop_count += 1

        class Repository:
            def __init__(self):
                self.deleted = []

            def delete_run(self, run_id):
                self.deleted.append(run_id)
                return True

        app = TestProLabApp.__new__(TestProLabApp)
        app.run_list_tree = Tree()
        app.machine = Machine()
        app.repository = Repository()
        app._session_runs = [
            {"id": 7},
            {"id": 8},
        ]
        app._running = True
        app._paused = True
        app._sample_iter = object()
        app._pending_sample = object()
        app._pause_started_monotonic = 1.0
        app.samples = [object()]
        app._result_program = object()
        app.stage_var = Variable()
        app.time_var = Variable()
        app.status_var = Variable()
        app._draw_chart = lambda: None
        app._refresh_run_list = lambda: None

        with patch("m231pro.ui.app.messagebox.askyesno", return_value=True):
            deleted = app._delete_selected_run()

        self.assertTrue(deleted)
        self.assertEqual(1, app.machine.stop_count)
        self.assertEqual([], app.repository.deleted)
        self.assertEqual([], app._session_runs)
        self.assertFalse(app._running)
        self.assertEqual([], app.samples)
        self.assertIn("3 ensaios", app.status_var.value)

    def test_history_delete_is_permanent_and_removes_session_copy(self):
        class Variable:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Tree:
            rows = {
                "row-7": (7, "data", "Ensaio A", "local", "concluído", 10),
            }

            def selection(self):
                return tuple(self.rows)

            def item(self, item, option=None, **_kwargs):
                if option == "values":
                    return self.rows[item]
                return {"values": self.rows[item]}

            def exists(self, item):
                return item in self.rows

            def delete(self, item):
                del self.rows[item]

        class Repository:
            def __init__(self):
                self.deleted = []

            def delete_run(self, run_id):
                self.deleted.append(run_id)
                return True

        tree = Tree()
        app = TestProLabApp.__new__(TestProLabApp)
        app.run_list_tree = object()
        app.repository = Repository()
        app._session_runs = [{"id": 7}, {"id": 8}]
        app.status_var = Variable()
        app._refresh_run_list = lambda: None
        app._sync_run_checkboxes = lambda _tree: None

        with patch("m231pro.ui.app.messagebox.askyesno", return_value=True):
            deleted = app._delete_selected_run(tree, permanent=True)

        self.assertTrue(deleted)
        self.assertEqual([7], app.repository.deleted)
        self.assertEqual([{"id": 8}], app._session_runs)
        self.assertEqual({}, tree.rows)
        self.assertIn("histórico permanente", app.status_var.value)

    def test_persisted_run_is_kept_in_the_temporary_session_list(self):
        class Repository:
            def __init__(self):
                self.saved = []

            def save_run(self, program, samples, mode, status):
                self.saved.append((program, samples, mode, status))
                return 42

        app = TestProLabApp.__new__(TestProLabApp)
        app.repository = Repository()
        app.machine = type("Machine", (), {"mode_name": "local"})()
        app._result_program = compression_return_template()
        app._current_run_started_at = "2026-11-15T12:00:00+00:00"
        app.samples = [object(), object()]
        app._session_runs = []
        refreshed = []
        app._refresh_run_list = lambda: refreshed.append(True)

        run_id = app._persist_result("concluído")

        self.assertEqual(42, run_id)
        self.assertEqual(1, len(app.repository.saved))
        self.assertEqual(1, len(app._session_runs))
        self.assertEqual(42, app._session_runs[0]["id"])
        self.assertEqual("2026-11-15T12:00:00+00:00", app._session_runs[0]["started_at"])
        self.assertEqual(2, app._session_runs[0]["sample_count"])
        self.assertEqual([True], refreshed)

    def test_force_unit_selector_converts_limit_without_changing_safety_value(self):
        class Variable:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        app = TestProLabApp.__new__(TestProLabApp)
        app.force_unit_var = Variable("N")
        app._displayed_force_unit = "kN"
        app.limit_var = Variable("1")
        app.force_var = Variable()
        app._last_force_kn = 2.0

        app._on_force_unit_changed()

        self.assertEqual("1000", app.limit_var.value)
        self.assertEqual("2000,00 N", app.force_var.value)

    def test_interface_has_no_machine_connection_button(self):
        source = inspect.getsource(TestProLabApp._build_ui)

        self.assertNotIn("Conectar máquina", source)

    def test_safety_load_field_fits_extended_number_formats(self):
        source = inspect.getsource(TestProLabApp._build_ui)

        self.assertIn('(\"Carga limite — segurança\", self.limit_var, 16)', source)

    def test_interface_has_no_mtd_import(self):
        source = inspect.getsource(TestProLabApp._build_ui)

        self.assertNotIn("Importar .MTD", source)
        self.assertFalse(hasattr(TestProLabApp, "_import_original_method"))

    def test_automatic_connection_retries_until_hardware_is_available(self):
        class LocalBackend:
            is_physical = False

        app = TestProLabApp.__new__(TestProLabApp)
        app.machine = type("Machine", (), {"backend": LocalBackend()})()
        app._running = False
        app._auto_connect_job = "current-job"
        started = []
        app._start_hardware_connection = lambda: started.append(True)

        app._auto_connect_hardware()

        self.assertIsNone(app._auto_connect_job)
        self.assertEqual([True], started)

    def test_connection_result_poll_does_not_wait_for_worker(self):
        app = TestProLabApp.__new__(TestProLabApp)
        app._closing = False
        app._connection_poll_job = None
        app._connection_results = __import__("queue").Queue()
        scheduled = []
        app.after = lambda delay, callback: scheduled.append((delay, callback)) or "poll-job"

        app._poll_hardware_connection()

        self.assertEqual("poll-job", app._connection_poll_job)
        self.assertEqual(50, scheduled[0][0])
        self.assertIs(app._poll_hardware_connection.__func__, scheduled[0][1].__func__)

    def test_automatic_connection_activates_native_backend(self):
        class Variable:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class LocalBackend:
            is_physical = False

        class Machine:
            def __init__(self):
                self.backend = LocalBackend()

            def set_backend(self, backend):
                self.backend = backend

        class NativeBackend:
            is_physical = True

            def __init__(self, dll_path, device_index=0):
                self.dll_path = dll_path
                self.device_index = device_index
                self.connected = False

            def connect(self):
                self.connected = True

            def disconnect(self):
                self.connected = False

        device = FtdiDevice(2, "M231", "Controladora", 0, 0, 0, 0)
        probe = FtdiProbeResult(
            True,
            (device,),
            "1 dispositivo detectado.",
            Path("ftd2xx64.dll"),
        )
        app = TestProLabApp.__new__(TestProLabApp)
        app.machine = Machine()
        app.hardware_var = Variable()
        app.status_var = Variable()

        with patch("m231pro.ui.app.probe_project_ftdi", return_value=probe), patch(
            "m231pro.ui.app.NativeM231Backend", NativeBackend
        ):
            connected = app._connect_hardware(automatic=True)

        self.assertTrue(connected)
        self.assertIsInstance(app.machine.backend, NativeBackend)
        self.assertTrue(app.machine.backend.connected)
        self.assertEqual(2, app.machine.backend.device_index)
        self.assertIn("automaticamente", app.status_var.value)

    def test_connection_loss_returns_to_local_mode_and_schedules_reconnection(self):
        class Variable:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class PhysicalBackend:
            is_physical = True

        class Machine:
            def __init__(self):
                self.backend = PhysicalBackend()
                self.disconnected = False

            def telemetry(self):
                raise OSError("USB removido")

            def disconnect(self):
                self.disconnected = True

        app = TestProLabApp.__new__(TestProLabApp)
        app.machine = Machine()
        app.hardware_var = Variable()
        app.machine_state_var = Variable()
        app.status_var = Variable()
        app._running = False
        app._paused = False
        app._sample_iter = None
        app._pending_sample = None
        app._return_direction = 0
        app._auto_connect_job = None
        app.samples = []
        scheduled = []
        app.after = lambda delay, callback: scheduled.append((delay, callback)) or f"job-{delay}"

        app._poll_machine()

        self.assertTrue(app.machine.disconnected)
        self.assertFalse(getattr(app.machine.backend, "is_physical", False))
        self.assertIn("reconectar automaticamente", app.status_var.value)
        self.assertEqual(0, scheduled[0][0])
        self.assertIs(app._auto_connect_hardware.__func__, scheduled[0][1].__func__)
        self.assertEqual(500, scheduled[1][0])

    def test_interface_opens_maximized(self):
        class Window:
            def __init__(self):
                self.requested_state = None

            def state(self, requested_state):
                self.requested_state = requested_state

        window = Window()
        maximize_main_window(window)

        self.assertEqual("zoomed", window.requested_state)

    def test_offline_machine_keeps_local_system_available(self):
        label, detail = machine_connection_presentation(
            FtdiProbeResult(False, (), "Driver nao encontrado.")
        )

        self.assertIn("OFFLINE", label)
        self.assertIn("sistema disponivel", label)
        self.assertIn("continua funcionando normalmente", detail)

    def test_detected_machine_changes_only_connection_presentation(self):
        device = FtdiDevice(0, "M231", "Controladora", 0, 0, 0, 0)
        label, detail = machine_connection_presentation(
            FtdiProbeResult(True, (device,), "1 dispositivo detectado.")
        )

        self.assertEqual("MAQUINA ONLINE", label)
        self.assertIn("nativo em Python", detail)

    def test_local_run_does_not_require_a_hardware_connection(self):
        class Variable:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Button:
            def configure(self, **_kwargs):
                pass

        program = compression_return_template()
        app = TestProLabApp.__new__(TestProLabApp)
        app._collect_program = lambda: program
        app._advance = lambda: None
        app.pause_button = Button()
        app.status_var = Variable()

        app._start()

        self.assertTrue(app._running)
        self.assertIsNotNone(app._engine)
        self.assertIn("sensor", app.status_var.value.lower())

    def test_return_button_uses_initial_position_and_manual_speed(self):
        class Variable:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Machine:
            def __init__(self):
                self.return_request = None

            def return_position(self, target_position_mm, speed_mm_min):
                self.return_request = (target_position_mm, speed_mm_min)

        app = TestProLabApp.__new__(TestProLabApp)
        app._running = False
        app.initial_var = Variable("-12,5")
        app.manual_speed_var = Variable("75")
        app.status_var = Variable()
        app.machine = Machine()

        app._return_position()

        self.assertEqual((-12.5, 75.0), app.machine.return_request)
        self.assertIn("-12.5 mm", app.status_var.value)
        self.assertIn("75 mm/min", app.status_var.value)

    def test_manual_buttons_show_the_confirmed_motion_direction(self):
        class Button:
            def __init__(self):
                self.options = {}

            def configure(self, **kwargs):
                self.options.update(kwargs)

        app = TestProLabApp.__new__(TestProLabApp)
        app.up_button = Button()
        app.manual_stop_button = Button()
        app.down_button = Button()
        app._return_direction = 0

        app._set_manual_motion_indicator(MachineState.JOG_UP)

        self.assertEqual("▲  SUBINDO...", app.up_button.options["text"])
        self.assertEqual("MovementUpActive.TButton", app.up_button.options["style"])
        self.assertEqual("■  PARAR AGORA", app.manual_stop_button.options["text"])

        app._set_manual_motion_indicator(MachineState.JOG_DOWN)

        self.assertEqual("▲  SUBIR", app.up_button.options["text"])
        self.assertEqual("▼  DESCENDO...", app.down_button.options["text"])
        self.assertEqual("MovementDownActive.TButton", app.down_button.options["style"])

        app._set_manual_motion_indicator(MachineState.STOPPED)

        self.assertEqual("▼  DESCER", app.down_button.options["text"])
        self.assertEqual("■  PARAR", app.manual_stop_button.options["text"])

    def test_poll_updates_status_when_course_limit_stops_machine(self):
        class Variable:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class Machine:
            backend = object()

            def telemetry(self):
                return MachineTelemetry(
                    1.0,
                    position_mm=100.0,
                    state=MachineState.STOPPED,
                    hardware_connected=True,
                    alarm="Limite superior de curso atingido: 100 mm",
                )

        class Button:
            def configure(self, **_kwargs):
                pass

        app = TestProLabApp.__new__(TestProLabApp)
        app.machine = Machine()
        app._last_machine_position = 99.0
        app._last_machine_state = MachineState.JOG_UP
        app._return_direction = 0
        app._running = False
        app.up_button = Button()
        app.manual_stop_button = Button()
        app.down_button = Button()
        app.force_var = Variable()
        app.position_var = Variable()
        app.displacement_var = Variable()
        app.machine_state_var = Variable()
        app.status_var = Variable()
        app.after = lambda *_args: None

        app._poll_machine()

        self.assertEqual("Parada", app.machine_state_var.value)
        self.assertIn("PARADA", app.status_var.value)
        self.assertIn("100 mm", app.status_var.value)

    def test_chart_draws_one_load_label_beside_each_real_acquisition(self):
        class RecordingCanvas:
            def __init__(self):
                self.texts = []

            def delete(self, *_args, **_kwargs):
                pass

            def winfo_width(self):
                return 900

            def winfo_height(self):
                return 500

            def create_line(self, *_args, **_kwargs):
                pass

            def create_oval(self, *_args, **_kwargs):
                pass

            def create_text(self, *args, **kwargs):
                self.texts.append((args, kwargs))

        program = compression_return_template()
        samples = [
            Sample(0.0, 1.5, 0.0, 0.0, 1, 0, "inicio", True),
            Sample(3.0, 99.0, 5.0, 5.0, 1, 1, "compressao", False),
            Sample(6.0, 2.25, 10.0, 10.0, 1, 1, "compressao", True),
            Sample(14.0, 3.75, 0.0, 0.0, 1, 2, "descompressao", True),
        ]
        canvas = RecordingCanvas()
        app = TestProLabApp.__new__(TestProLabApp)
        app.chart = canvas
        app.samples = samples
        app.program = program
        app._result_program = program

        app._draw_chart()

        load_labels = [
            kwargs["text"]
            for _args, kwargs in canvas.texts
            if str(kwargs.get("text", "")).endswith(" kN")
        ]
        self.assertEqual(["1.5 kN", "2.25 kN", "3.75 kN"], load_labels)

    def test_chart_and_csv_source_contains_only_real_acquisitions(self):
        samples = [
            Sample(0.0, 0.0, 0.0, 0.0, 1, 0, "inicio", True),
            Sample(1.0, 0.0, 5.0, 5.0, 1, 1, "compressao", False),
            Sample(6.0, 0.0, 10.0, 10.0, 1, 1, "compressao", True),
        ]

        self.assertEqual([samples[0], samples[2]], acquired_samples(samples))

    def test_chart_marks_each_unique_acquired_position_on_left_axis(self):
        samples = [
            Sample(0.0, 0.0, 0.0, 0.0, 1, 0, "inicio", True),
            Sample(6.0, 0.0, 10.0, 10.0, 1, 1, "compressao", True),
            Sample(12.0, 0.0, 20.0, 20.0, 1, 1, "compressao", True),
            Sample(18.0, 0.0, 10.0, 10.0, 1, 2, "descompressao", True),
            Sample(24.0, 0.0, 0.0, 0.0, 1, 2, "descompressao", True),
        ]

        self.assertEqual([0.0, 10.0, 20.0], chart_position_marks(samples))

    def test_chart_separates_compression_and_return_axis_values(self):
        samples = [
            Sample(0.0, 0.0, 0.0, 0.0, 1, 0, "inicio", True),
            Sample(6.0, 0.0, 10.0, 10.0, 1, 1, "compressao", True),
            Sample(12.0, 0.0, 20.0, 20.0, 1, 1, "compressao", True),
            Sample(18.0, 0.0, 10.0, 10.0, 1, 2, "descompressao", True),
            Sample(24.0, 0.0, 0.0, 0.0, 1, 2, "descompressao", True),
        ]

        self.assertEqual(
            [0.0, 10.0, 20.0],
            chart_position_marks(samples, {"inicio", "compressao"}),
        )
        self.assertEqual(
            [0.0, 10.0],
            chart_position_marks(samples, {"descompressao"}),
        )

    def test_chart_axis_marks_keep_enough_vertical_space(self):
        positions = [float(position) for position in range(0, 311, 10)]

        marks = spaced_chart_marks(positions, 0.0, 310.0, 426.0)
        pixel_gaps = [
            (current - previous) / 310.0 * 426.0
            for previous, current in zip(marks, marks[1:])
        ]

        self.assertEqual(0.0, marks[0])
        self.assertEqual(310.0, marks[-1])
        self.assertTrue(all(gap >= 22.0 for gap in pixel_gaps))

    def test_csv_number_format_does_not_add_meaningless_zeroes(self):
        self.assertEqual("0", format_csv_number(0.0))
        self.assertEqual("10", format_csv_number(10.0))
        self.assertEqual("10.25", format_csv_number(10.25))
        self.assertEqual("0", format_csv_number(-0.0000001))

    def test_chart_force_label_identifies_each_acquisition_load(self):
        self.assertEqual("0 kN", format_chart_force(0.0))
        self.assertEqual("10.25 kN", format_chart_force(10.25))
        self.assertEqual("1000 N", format_chart_force(1.0, "N"))
        self.assertEqual("101.971621 kgf", format_chart_force(1.0, "kgf"))

    def test_sample_waits_until_its_real_time(self):
        self.assertEqual(50, remaining_delay_ms(0.05, 100.0, 100.0))
        self.assertEqual(25, remaining_delay_ms(0.05, 100.0, 100.025))

    def test_late_sample_does_not_add_another_delay(self):
        self.assertEqual(0, remaining_delay_ms(0.05, 100.0, 100.2))

    def test_delay_rounds_up_to_avoid_running_fast(self):
        self.assertEqual(1, remaining_delay_ms(0.05, 100.0, 100.0499))

    def test_csv_identifies_that_load_does_not_come_from_a_sensor(self):
        sample = Sample(60.0, 0.0, 100.0, 100.0, 1, 1, "compressao", True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resultado.csv"
            write_results_csv(path, [sample], compression_return_template())
            rows = path.read_text(encoding="utf-8-sig").splitlines()

        self.assertEqual(
            "tempo_s;delta_tempo_s;carga_kn;posicao_mm;deslocamento_mm;"
            "variacao_posicao_mm;velocidade_calculada_mm_min;ciclo;etapa;acao;"
            "intervalo_aquisicao_configurado_mm;espera_configurada_s",
            rows[0],
        )
        self.assertEqual(
            "60;0;0;100;100;0;0;1;1;compressao;10;2",
            rows[1],
        )

    def test_csv_exports_load_in_selected_force_unit(self):
        program = compression_return_template()
        program.force_unit = "N"
        sample = Sample(1.0, 1.25, 1.0, 1.0, 1, 1, "compressao", True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resultado.csv"
            write_results_csv(path, [sample], program)
            rows = path.read_text(encoding="utf-8-sig").splitlines()

        self.assertIn("carga_n", rows[0])
        self.assertEqual("1250", rows[1].split(";")[2])

    def test_csv_exports_only_acquisitions_and_calculates_motion_speed(self):
        program = compression_return_template()
        samples = [
            Sample(0.0, 0.0, 0.0, 0.0, 1, 0, "inicio", True),
            Sample(1.0, 0.0, 1.0, 1.0, 1, 1, "compressao", False),
            Sample(6.0, 0.0, 10.0, 10.0, 1, 1, "compressao", True),
            Sample(8.0, 0.0, 10.0, 10.0, 1, 2, "aguardar_descompressao", True),
            Sample(14.0, 0.0, 0.0, 0.0, 1, 2, "descompressao", True),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resultado.csv"
            write_results_csv(path, samples, program)
            rows = path.read_text(encoding="utf-8-sig").splitlines()

        self.assertEqual(5, len(rows))
        self.assertIn("6;6;0;10;10;10;100", rows[2])
        self.assertIn("8;2;0;10;10;0;0", rows[3])
        self.assertIn("14;6;0;0;0;-10;100", rows[4])


if __name__ == "__main__":
    unittest.main()
