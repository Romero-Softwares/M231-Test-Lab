from __future__ import annotations

import csv
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from m231pro.config import APP_ICON, DRIVER_ROOT, PROJECT_ROOT, RESULTS_DATABASE
from m231pro.core.reporting import generate_run_report_pdf, report_filename
from m231pro.core.storage import ResultsRepository
from m231pro.hardware.backend import LocalMachineBackend, MachineController, MachineState
from m231pro.hardware.ftdi import FtdiProbeResult, probe_project_ftdi
from m231pro.hardware.m231 import NativeM231Backend

from m231pro.core.simulation import (
    FORCE_UNITS,
    Sample,
    SimulationEngine,
    TestProgram,
    TestStep,
    compression_return_template,
    cyclic_template,
    force_from_kn,
    force_to_kn,
)


ACTION_LABELS = {
    "compressao": "Compressão",
    "descompressao": "Descompressão/retorno",
    "manter": "Manter posição",
    "aguardar_descompressao": "Espera antes da descompressão",
    "limite_seguranca": "PARADA POR CARGA",
}
LABEL_ACTIONS = {
    label: action
    for action, label in ACTION_LABELS.items()
    if action not in ("aguardar_descompressao", "limite_seguranca")
}

AUTO_CONNECT_INITIAL_DELAY_MS = 250
AUTO_CONNECT_RETRY_MS = 3000
CONNECT_RESULT_POLL_MS = 50


def apply_application_icon(window: tk.Misc) -> None:
    """Aplica o icone do produto e o herda nas novas janelas Tk."""
    if not APP_ICON.is_file():
        return
    try:
        window.wm_iconbitmap(default=str(APP_ICON))
    except (OSError, tk.TclError):
        # Mantem a interface utilizavel em plataformas sem suporte a arquivos ICO.
        pass


@dataclass(frozen=True)
class HardwareConnectionAttempt:
    probe: FtdiProbeResult
    backend: NativeM231Backend | None = None
    error: Exception | None = None


def machine_connection_presentation(
    result: FtdiProbeResult | None,
) -> tuple[str, str]:
    """Separa o estado da maquina da disponibilidade do sistema local."""
    if result is not None and result.devices:
        return (
            "MAQUINA ONLINE",
            f"{result.message} Controle M231 nativo em Python disponivel para conexao.",
        )

    diagnostic = result.message if result is not None else "Nenhuma maquina conectada."
    return (
        "MAQUINA OFFLINE - sistema disponivel",
        f"{diagnostic} O sistema continua funcionando normalmente em modo local.",
    )


def remaining_delay_ms(sample_time_s: float, started_at: float, now: float) -> int:
    """Retorna quanto falta para exibir uma amostra no tempo real correto."""
    return max(0, math.ceil((started_at + sample_time_s - now) * 1000))


def format_csv_number(value: float) -> str:
    """Formata medições sem zeros decimais artificiais ou -0."""
    rounded = round(float(value), 6)
    if abs(rounded) < 1e-12:
        rounded = 0.0
    return f"{rounded:.6f}".rstrip("0").rstrip(".")


def format_force(value_kn: float, unit: str = "kN", decimals: int | None = None) -> str:
    """Formata uma carga interna em kN na unidade selecionada."""
    converted = force_from_kn(value_kn, unit)
    if decimals is None:
        number = format_csv_number(converted)
    else:
        number = f"{converted:.{decimals}f}".replace(".", ",")
    return f"{number} {unit}"


def format_chart_force(value: float, unit: str = "kN") -> str:
    """Formata a carga que acompanha cada ponto de aquisicao no grafico."""
    return format_force(value, unit)


def acquired_samples(samples: list[Sample]) -> list[Sample]:
    """Mantém uma única definição dos pontos exibidos e exportados."""
    return [sample for sample in samples if sample.acquired]


def chart_position_marks(
    samples: list[Sample], actions: set[str] | None = None
) -> list[float]:
    """Retorna posições únicas, permitindo separar compressão e retorno."""
    return sorted(
        {
            round(float(sample.position_mm), 6)
            for sample in samples
            if actions is None or sample.action in actions
        }
    )


def spaced_chart_marks(
    positions: list[float],
    position_min: float,
    position_max: float,
    plot_height: float,
    min_spacing_px: float = 22.0,
) -> list[float]:
    """Reduz apenas os rótulos do eixo para que textos vizinhos não se sobreponham."""
    unique_positions = sorted({round(float(position), 6) for position in positions})
    if len(unique_positions) <= 1 or position_max <= position_min or plot_height <= 0:
        return unique_positions

    minimum_position_gap = min_spacing_px * (position_max - position_min) / plot_height
    selected = [unique_positions[0]]
    for position in unique_positions[1:-1]:
        if position - selected[-1] >= minimum_position_gap:
            selected.append(position)

    final_position = unique_positions[-1]
    if final_position - selected[-1] >= minimum_position_gap:
        selected.append(final_position)
    elif len(selected) == 1:
        selected[0] = final_position
    else:
        selected[-1] = final_position
    return selected


def maximize_main_window(window: tk.Tk) -> None:
    """Abre a janela principal maximizada no Windows."""
    window.state("zoomed")


def write_results_csv(
    path: str | Path,
    samples: list[Sample],
    program: TestProgram,
) -> None:
    """Grava aquisições e dados suficientes para conferir tempo e velocidade."""
    samples_to_export = acquired_samples(samples)
    force_unit = program.force_unit
    force_column = f"carga_{force_unit.lower()}"
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(
            [
                "tempo_s",
                "delta_tempo_s",
                force_column,
                "posicao_mm",
                "deslocamento_mm",
                "variacao_posicao_mm",
                "velocidade_calculada_mm_min",
                "ciclo",
                "etapa",
                "acao",
                "intervalo_aquisicao_configurado_mm",
                "espera_configurada_s",
            ]
        )
        previous: Sample | None = None
        for sample in samples_to_export:
            delta_time = sample.time_s - previous.time_s if previous else 0.0
            delta_position = sample.position_mm - previous.position_mm if previous else 0.0
            calculated_speed = (
                abs(delta_position) / delta_time * 60.0
                if delta_time > 1e-12
                else 0.0
            )
            writer.writerow(
                [
                    format_csv_number(sample.time_s),
                    format_csv_number(delta_time),
                    format_csv_number(force_from_kn(sample.force_kn, force_unit)),
                    format_csv_number(sample.position_mm),
                    format_csv_number(sample.displacement_mm),
                    format_csv_number(delta_position),
                    format_csv_number(calculated_speed),
                    sample.cycle,
                    sample.step,
                    sample.action,
                    format_csv_number(program.acquisition_interval_mm),
                    format_csv_number(program.wait_before_decompression_s),
                ]
            )
            previous = sample


class TestProLabApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        apply_application_icon(self)
        self.title("M231 Test Lab — Programador de Ensaios")
        self.geometry("1240x760")
        self.minsize(1040, 650)
        maximize_main_window(self)
        self.program = compression_return_template()
        self.samples: list[Sample] = []
        self._result_program: TestProgram | None = None
        self._engine: SimulationEngine | None = None
        self._sample_iter = None
        self._pending_sample: Sample | None = None
        self._started_monotonic = 0.0
        self._pause_started_monotonic: float | None = None
        self._running = False
        self._paused = False
        self._return_direction = 0
        self._last_machine_position: float | None = None
        self._last_force_kn = 0.0
        self._last_machine_state: MachineState | None = None
        self._steps_window: tk.Toplevel | None = None
        self.tree: ttk.Treeview | None = None
        self.run_list_tree: ttk.Treeview | None = None
        self._session_runs: list[dict[str, object]] = []
        self._current_run_started_at = ""
        self._auto_connect_job: str | None = None
        self._connection_poll_job: str | None = None
        self._connection_thread: threading.Thread | None = None
        self._connection_results: queue.Queue[HardwareConnectionAttempt] = queue.Queue()
        self._connection_cancelled = threading.Event()
        self._closing = False
        self.machine = MachineController()
        self.repository = ResultsRepository(RESULTS_DATABASE)
        self._build_style()
        self._build_ui()
        self._load_program_to_ui()
        self.protocol("WM_DELETE_WINDOW", self._close_app)
        self.after(100, self._poll_machine)
        self.status_var.set("Procurando a máquina automaticamente...")
        self._schedule_auto_connect(AUTO_CONNECT_INITIAL_DELAY_MS)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Danger.TButton", foreground="#a40000")
        style.configure(
            "Movement.TButton",
            font=("Segoe UI Symbol", 10, "bold"),
            padding=(8, 5),
        )
        style.configure(
            "MovementStop.TButton",
            font=("Segoe UI Symbol", 10, "bold"),
            foreground="#a40000",
            padding=(8, 5),
        )
        style.configure(
            "MovementUpActive.TButton",
            font=("Segoe UI Symbol", 10, "bold"),
            foreground="#ffffff",
            background="#167447",
            padding=(8, 5),
        )
        style.map(
            "MovementUpActive.TButton",
            background=[("active", "#125c39")],
            foreground=[("active", "#ffffff")],
        )
        style.configure(
            "MovementDownActive.TButton",
            font=("Segoe UI Symbol", 10, "bold"),
            foreground="#ffffff",
            background="#b45309",
            padding=(8, 5),
        )
        style.map(
            "MovementDownActive.TButton",
            background=[("active", "#8f4307")],
            foreground=[("active", "#ffffff")],
        )
        style.configure(
            "MovementStopActive.TButton",
            font=("Segoe UI Symbol", 10, "bold"),
            foreground="#ffffff",
            background="#b42318",
            padding=(8, 5),
        )
        style.map(
            "MovementStopActive.TButton",
            background=[("active", "#8f1c13")],
            foreground=[("active", "#ffffff")],
        )

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=10)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="M231 Test Lab", style="Title.TLabel").pack(side="left")
        ttk.Button(toolbar, text="Novo", command=self._new_program).pack(side="left", padx=(20, 4))
        ttk.Button(toolbar, text="Abrir", command=self._open_program).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Salvar", command=self._save_program).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Exportar CSV", command=self._export_csv).pack(side="left", padx=4)
        ttk.Button(
            toolbar,
            text="Histórico permanente / Relatórios",
            command=self._show_history,
        ).pack(side="left", padx=4)
        ttk.Button(
            toolbar,
            text="Detectar hardware",
            command=self._probe_hardware,
        ).pack(side="left", padx=4)
        connection_label, _connection_detail = machine_connection_presentation(None)
        self.hardware_var = tk.StringVar(value=connection_label)
        hardware_terminal = tk.Frame(
            toolbar,
            background="#111820",
            highlightbackground="#3d4b55",
            highlightcolor="#3d4b55",
            highlightthickness=1,
            borderwidth=0,
        )
        hardware_terminal.pack(side="right")
        tk.Label(
            hardware_terminal,
            text=">",
            background="#111820",
            foreground="#65d46e",
            font=("Consolas", 10, "bold"),
            padx=7,
            pady=6,
        ).pack(side="left")
        tk.Label(
            hardware_terminal,
            textvariable=self.hardware_var,
            background="#111820",
            foreground="#ffb454",
            font=("Consolas", 9, "bold"),
            padx=0,
            pady=6,
        ).pack(side="left", padx=(0, 9))

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        left = ttk.Frame(body, padding=8)
        right = ttk.Frame(body, padding=8)
        body.add(left, weight=1)
        body.add(right, weight=1)

        settings = ttk.LabelFrame(left, text="Configuração do ensaio", padding=8)
        settings.pack(fill="x")
        self.name_var = tk.StringVar()
        self.limit_var = tk.StringVar()
        self.force_unit_var = tk.StringVar(value="kN")
        self._displayed_force_unit = "kN"
        self.initial_var = tk.StringVar()
        self.min_position_var = tk.StringVar()
        self.max_position_var = tk.StringVar()
        self.cycles_var = tk.StringVar()
        self.wait_before_return_var = tk.StringVar()
        self.acquisition_interval_var = tk.StringVar()
        field_rows = (
            (
                ("Nome", self.name_var, 28),
                ("Carga limite — segurança", self.limit_var, 16),
                ("Unidade de carga", self.force_unit_var, 6),
                ("Posição inicial (mm)", self.initial_var, 9),
            ),
            (
                ("Posição mínima (mm)", self.min_position_var, 9),
                ("Posição máxima (mm)", self.max_position_var, 9),
                ("Ciclos", self.cycles_var, 6),
                ("Espera antes de descomprimir (s)", self.wait_before_return_var, 9),
                ("Aquisição a cada (mm)", self.acquisition_interval_var, 9),
            ),
        )
        for row, fields in enumerate(field_rows):
            field_row = ttk.Frame(settings)
            field_row.grid(row=row, column=0, sticky="w")
            for label, variable, width in fields:
                field = ttk.Frame(field_row)
                field.pack(side="left", anchor="n", padx=(0, 12), pady=(0, 5))
                ttk.Label(field, text=label).pack(anchor="w")
                if variable is self.force_unit_var:
                    editor = ttk.Combobox(
                        field,
                        textvariable=variable,
                        values=FORCE_UNITS,
                        state="readonly",
                        width=width,
                    )
                    editor.bind("<<ComboboxSelected>>", self._on_force_unit_changed)
                else:
                    editor = ttk.Entry(field, textvariable=variable, width=width)
                editor.pack(anchor="w", pady=(1, 0))

        presets = ttk.Frame(left, padding=(0, 8))
        presets.pack(fill="x")
        ttk.Label(presets, text="Modelos:").pack(side="left")
        ttk.Button(
            presets,
            text="Compressão + retorno",
            command=lambda: self._apply_template(compression_return_template()),
        ).pack(side="left", padx=5)
        ttk.Button(
            presets,
            text="Cíclico",
            command=lambda: self._apply_template(cyclic_template()),
        ).pack(side="left", padx=5)
        self.steps_button_var = tk.StringVar(value="Etapas programadas")
        ttk.Button(
            presets,
            textvariable=self.steps_button_var,
            command=self._show_steps,
        ).pack(side="left", padx=(14, 5))
        chart_box = ttk.LabelFrame(
            left,
            text="Aquisições do ensaio — posição × tempo",
            padding=6,
        )
        chart_box.pack(fill="both", expand=True)
        self.chart = tk.Canvas(chart_box, background="#ffffff", highlightthickness=0)
        self.chart.pack(fill="both", expand=True)
        self.chart.bind("<Configure>", lambda _event: self._draw_chart())

        controls = ttk.LabelFrame(right, text="Execução", padding=10)
        controls.pack(fill="x")
        run_controls = ttk.Frame(controls)
        run_controls.pack(fill="x")
        ttk.Button(run_controls, text="▶ Executar ensaio", command=self._start).pack(side="left")
        self.pause_button = ttk.Button(run_controls, text="Pausar", command=self._toggle_pause)
        self.pause_button.pack(side="left", padx=5)
        ttk.Button(run_controls, text="■ Parar", style="Danger.TButton", command=self._stop).pack(side="left")
        self.machine_state_var = tk.StringVar(value="Pronta em modo local")
        ttk.Label(run_controls, textvariable=self.machine_state_var).pack(side="right")

        manual = ttk.LabelFrame(controls, text="Movimentação manual", padding=8)
        manual.pack(fill="x", pady=(10, 0))
        self.manual_speed_var = tk.StringVar(value="100")
        speed_controls = ttk.Frame(manual)
        speed_controls.pack(anchor="center")
        ttk.Label(speed_controls, text="Velocidade").pack(side="left")
        ttk.Entry(
            speed_controls,
            textvariable=self.manual_speed_var,
            width=8,
            justify="center",
        ).pack(side="left", padx=(6, 3))
        ttk.Label(speed_controls, text="mm/min").pack(side="left")

        movement_buttons = ttk.Frame(manual, padding=(0, 7, 0, 5))
        movement_buttons.pack(anchor="center")
        self.up_button = ttk.Button(
            movement_buttons,
            text="▲  SUBIR",
            width=15,
            style="Movement.TButton",
            command=lambda: self._manual_jog(1),
        )
        self.up_button.grid(row=0, column=0, padx=3)
        self.manual_stop_button = ttk.Button(
            movement_buttons,
            text="■  PARAR",
            width=15,
            style="MovementStop.TButton",
            command=self._manual_stop,
        )
        self.manual_stop_button.grid(row=0, column=1, padx=3)
        self.down_button = ttk.Button(
            movement_buttons,
            text="▼  DESCER",
            width=15,
            style="Movement.TButton",
            command=lambda: self._manual_jog(-1),
        )
        self.down_button.grid(row=0, column=2, padx=3)

        reference_buttons = ttk.Frame(manual)
        reference_buttons.pack(anchor="center")
        ttk.Button(
            reference_buttons,
            text="↶ Retornar posição",
            command=self._return_position,
        ).pack(side="left", padx=3)
        ttk.Button(
            reference_buttons,
            text="◎ Zerar posição",
            command=self._zero_position,
        ).pack(side="left", padx=3)
        ttk.Button(
            reference_buttons,
            text="Ø Zerar carga",
            command=self._zero_force,
        ).pack(side="left", padx=3)

        metrics = ttk.Frame(right, padding=(0, 12))
        metrics.pack(fill="x")
        self.force_var = tk.StringVar(value="0,00 kN")
        self.position_var = tk.StringVar(value="0,00 mm")
        self.displacement_var = tk.StringVar(value="0,00 mm")
        self.time_var = tk.StringVar(value="0,00 s")
        self.stage_var = tk.StringVar(value="Aguardando")
        metric_fields = (
            ("Carga", self.force_var),
            ("Posição", self.position_var),
            ("Deslocamento", self.displacement_var),
            ("Tempo", self.time_var),
        )
        for column, (label, variable) in enumerate(metric_fields):
            ttk.Label(metrics, text=label, foreground="#555").grid(row=0, column=column, sticky="w", padx=(0 if column == 0 else 22, 0))
            ttk.Label(metrics, textvariable=variable, style="Status.TLabel").grid(row=1, column=column, sticky="w", padx=(0 if column == 0 else 22, 0))
        ttk.Label(metrics, textvariable=self.stage_var).grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

        runs_box = ttk.LabelFrame(right, text="Ensaios desta sessão", padding=6)
        runs_box.pack(fill="both", expand=True, pady=(0, 4))
        run_columns = ("id", "date", "name", "mode", "status", "samples")
        self.run_list_tree = ttk.Treeview(
            runs_box,
            columns=run_columns,
            show="tree headings",
            height=8,
            selectmode="extended",
        )
        self.run_list_tree.heading("#0", text="Sel.", anchor="center")
        self.run_list_tree.column(
            "#0", width=46, minwidth=46, stretch=False, anchor="center"
        )
        run_headings = {
            "id": "#",
            "date": "Data UTC",
            "name": "Ensaio",
            "mode": "Modo",
            "status": "Estado",
            "samples": "Aquisições",
        }
        run_widths = {
            "id": 45,
            "date": 115,
            "name": 155,
            "mode": 75,
            "status": 80,
            "samples": 90,
        }
        for column in run_columns:
            self.run_list_tree.heading(
                column, text=run_headings[column], anchor="center"
            )
            self.run_list_tree.column(
                column,
                width=run_widths[column],
                minwidth=45,
                anchor="center",
            )
        run_scrollbar = ttk.Scrollbar(
            runs_box,
            orient="vertical",
            command=self.run_list_tree.yview,
        )
        run_horizontal_scrollbar = ttk.Scrollbar(
            runs_box,
            orient="horizontal",
            command=self.run_list_tree.xview,
        )
        self.run_list_tree.configure(
            yscrollcommand=run_scrollbar.set,
            xscrollcommand=run_horizontal_scrollbar.set,
        )
        self._enable_run_checkboxes(self.run_list_tree)
        runs_box.columnconfigure(0, weight=1)
        runs_box.rowconfigure(0, weight=1)
        self.run_list_tree.grid(row=0, column=0, sticky="nsew")
        run_scrollbar.grid(row=0, column=1, sticky="ns")
        run_horizontal_scrollbar.grid(row=1, column=0, sticky="ew")

        run_actions = ttk.Frame(right, padding=(0, 4, 0, 0))
        run_actions.pack(fill="x")
        ttk.Button(
            run_actions,
            text="Remover selecionados",
            style="Danger.TButton",
            command=self._delete_selected_run,
        ).pack(side="right")
        self._refresh_run_list()

        self.status_var = tk.StringVar(
            value="Sistema pronto em modo local. A maquina pode permanecer offline."
        )
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=5).pack(fill="x", side="bottom")

    @staticmethod
    def _number(value: str) -> float:
        return float(value.replace(",", "."))

    def _selected_force_unit(self) -> str:
        variable = self.__dict__.get("force_unit_var")
        if variable is not None:
            unit = variable.get()
            if unit in FORCE_UNITS:
                return unit
        program = self.__dict__.get("program")
        return program.force_unit if program is not None else "kN"

    def _load_program_to_ui(self) -> None:
        self.name_var.set(self.program.name)
        self.force_unit_var.set(self.program.force_unit)
        self._displayed_force_unit = self.program.force_unit
        self.limit_var.set(
            format_csv_number(
                force_from_kn(self.program.force_limit_kn, self.program.force_unit)
            )
        )
        self.initial_var.set(f"{self.program.initial_position_mm:g}")
        self.min_position_var.set(f"{self.program.position_min_mm:g}")
        self.max_position_var.set(f"{self.program.position_max_mm:g}")
        self.cycles_var.set(str(self.program.cycles))
        self.wait_before_return_var.set(
            f"{self.program.wait_before_decompression_s:g}"
        )
        self.acquisition_interval_var.set(
            f"{self.program.acquisition_interval_mm:g}"
        )
        if "force_var" in self.__dict__:
            self.force_var.set(
                format_force(
                    self.__dict__.get("_last_force_kn", 0.0),
                    self.program.force_unit,
                    2,
                )
            )
        self._refresh_steps()

    def _on_force_unit_changed(self, _event=None) -> None:
        new_unit = self.force_unit_var.get()
        previous_unit = self._displayed_force_unit
        if new_unit == previous_unit:
            return
        try:
            current_value = self._number(self.limit_var.get())
            limit_kn = force_to_kn(current_value, previous_unit)
        except (ValueError, TypeError):
            self._displayed_force_unit = new_unit
            return
        self.limit_var.set(format_csv_number(force_from_kn(limit_kn, new_unit)))
        self._displayed_force_unit = new_unit
        if "force_var" in self.__dict__:
            self.force_var.set(
                format_force(self.__dict__.get("_last_force_kn", 0.0), new_unit, 2)
            )
        if "chart" in self.__dict__:
            self._draw_chart()

    def _collect_program(self) -> TestProgram:
        program = TestProgram(
            name=self.name_var.get().strip(),
            force_limit_kn=force_to_kn(
                self._number(self.limit_var.get()), self.force_unit_var.get()
            ),
            force_unit=self.force_unit_var.get(),
            initial_position_mm=self._number(self.initial_var.get()),
            position_min_mm=self._number(self.min_position_var.get()),
            position_max_mm=self._number(self.max_position_var.get()),
            cycles=int(self.cycles_var.get()),
            wait_before_decompression_s=self._number(
                self.wait_before_return_var.get()
            ),
            acquisition_interval_mm=self._number(
                self.acquisition_interval_var.get()
            ),
            steps=list(self.program.steps),
        )
        program.validate()
        self.program = program
        return program

    def _show_steps(self) -> None:
        if self._steps_window is not None and self._steps_window.winfo_exists():
            self._steps_window.deiconify()
            self._steps_window.lift()
            self._steps_window.focus_force()
            return

        window = tk.Toplevel(self)
        self._steps_window = window
        window.title("Etapas programadas")
        window.geometry("860x500")
        window.minsize(700, 360)
        window.transient(self)
        window.protocol("WM_DELETE_WINDOW", self._close_steps_window)

        content = ttk.Frame(window, padding=10)
        content.pack(fill="both", expand=True)
        ttk.Label(
            content,
            text="Defina a sequência executada pelo ensaio. Clique duas vezes para editar uma etapa.",
            foreground="#555",
        ).pack(fill="x", pady=(0, 8))

        table = ttk.Frame(content)
        table.pack(fill="both", expand=True)
        columns = ("order", "action", "target", "speed", "duration")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=14)
        headings = {
            "order": "#",
            "action": "Ação",
            "target": "Posição-alvo (mm)",
            "speed": "Velocidade (mm/min)",
            "duration": "Tempo (s)",
        }
        widths = {"order": 45, "action": 220, "target": 150, "speed": 170, "duration": 100}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="center")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self._edit_selected_step())

        step_buttons = ttk.Frame(content, padding=(0, 8, 0, 0))
        step_buttons.pack(fill="x")
        ttk.Button(step_buttons, text="+ Etapa", command=self._add_step).pack(side="left")
        ttk.Button(step_buttons, text="Editar", command=self._edit_selected_step).pack(side="left", padx=4)
        ttk.Button(step_buttons, text="Remover", command=self._remove_step).pack(side="left", padx=4)
        ttk.Button(step_buttons, text="↑", width=3, command=lambda: self._move_step(-1)).pack(side="left", padx=(12, 2))
        ttk.Button(step_buttons, text="↓", width=3, command=lambda: self._move_step(1)).pack(side="left", padx=2)
        ttk.Button(step_buttons, text="Fechar", command=self._close_steps_window).pack(side="right")
        self._refresh_steps()
        self.tree.focus_set()

    def _close_steps_window(self) -> None:
        window = self._steps_window
        self._steps_window = None
        self.tree = None
        if window is not None and window.winfo_exists():
            window.destroy()

    def _refresh_steps(self) -> None:
        if hasattr(self, "steps_button_var"):
            self.steps_button_var.set(f"Etapas programadas ({len(self.program.steps)})")
        if self.tree is None or not self.tree.winfo_exists():
            return
        self.tree.delete(*self.tree.get_children())
        for index, step in enumerate(self.program.steps, start=1):
            if step.action == "compressao":
                target = f"{step.target_position_mm:g}"
                speed = f"{step.speed_mm_min:g}"
                duration = "—"
            elif step.action == "descompressao":
                target = "posição inicial"
                speed = "igual à compressão"
                duration = "—"
            else:
                target = "posição atual"
                speed = "—"
                duration = f"{step.duration_s:g}"
            self.tree.insert(
                "",
                "end",
                values=(index, ACTION_LABELS[step.action], target, speed, duration),
            )

    def _apply_template(self, program: TestProgram) -> None:
        self._stop()
        self.program = program
        self.samples = []
        self._result_program = None
        self._load_program_to_ui()
        self._draw_chart()
        self.status_var.set(f"Modelo carregado: {program.name}.")

    def _new_program(self) -> None:
        self._apply_template(
            TestProgram(steps=[TestStep("compressao", 50.0, 100.0), TestStep("descompressao")])
        )

    def _open_program(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Programa de ensaio", "*.json"), ("Todos", "*.*")])
        if not path:
            return
        try:
            self._apply_template(TestProgram.load(path))
            self.status_var.set(f"Programa aberto: {Path(path).name}")
        except (OSError, ValueError, KeyError, TypeError) as error:
            messagebox.showerror("Não foi possível abrir", str(error))

    def _save_program(self) -> None:
        try:
            program = self._collect_program()
        except (ValueError, TypeError) as error:
            messagebox.showerror("Programação inválida", str(error))
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Programa de ensaio", "*.json")])
        if path:
            program.save(path)
            self.status_var.set(f"Programa salvo: {Path(path).name}")

    def _selected_index(self) -> int | None:
        if self.tree is None or not self.tree.winfo_exists():
            return None
        selected = self.tree.selection()
        return self.tree.index(selected[0]) if selected else None

    def _add_step(self) -> None:
        parent = self._steps_window or self
        dialog = StepDialog(parent, TestStep("compressao", 50.0, 100.0))
        self.wait_window(dialog)
        if dialog.result:
            self.program.steps.append(dialog.result)
            self._refresh_steps()

    def _edit_selected_step(self) -> None:
        index = self._selected_index()
        if index is None:
            messagebox.showinfo(
                "Editar etapa",
                "Selecione uma etapa.",
                parent=self._steps_window or self,
            )
            return
        dialog = StepDialog(self._steps_window or self, replace(self.program.steps[index]))
        self.wait_window(dialog)
        if dialog.result:
            self.program.steps[index] = dialog.result
            self._refresh_steps()

    def _remove_step(self) -> None:
        index = self._selected_index()
        if index is not None:
            del self.program.steps[index]
            self._refresh_steps()

    def _move_step(self, offset: int) -> None:
        index = self._selected_index()
        if index is None or not 0 <= index + offset < len(self.program.steps):
            return
        self.program.steps[index], self.program.steps[index + offset] = (
            self.program.steps[index + offset],
            self.program.steps[index],
        )
        self._refresh_steps()
        if self.tree is not None and self.tree.winfo_exists():
            self.tree.selection_set(self.tree.get_children()[index + offset])

    def _start(self) -> None:
        try:
            program = self._collect_program()
        except (ValueError, TypeError) as error:
            messagebox.showerror("Programação inválida", str(error))
            return
        if "machine" not in self.__dict__:
            self.machine = MachineController()
        try:
            self.machine.configure_limits(program.position_min_mm, program.position_max_mm)
            configure_safety = getattr(self.machine, "configure_safety", None)
            if callable(configure_safety):
                configure_safety(program.force_limit_kn)
            self.machine.begin_test()
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            messagebox.showerror("Não foi possível iniciar", str(error))
            return
        self.samples = []
        self._result_program = deepcopy(program)
        self._engine = SimulationEngine(program)
        self._sample_iter = iter(self._engine.samples())
        self._pending_sample = None
        self._started_monotonic = time.monotonic()
        self._pause_started_monotonic = None
        self._current_run_started_at = datetime.now(timezone.utc).isoformat()
        self._running = True
        self._paused = False
        self.pause_button.configure(text="Pausar")
        self._update_current_run_row()
        if getattr(self.machine.backend, "is_physical", False):
            self.status_var.set(
                f"Ensaio físico em execução via {self.machine.mode_name}; "
                "carga e posição vêm da telemetria M231."
            )
        else:
            self.status_var.set(
                f"Ensaio em execução via {self.machine.mode_name}. "
                f"Carga = {format_force(0.0, program.force_unit)} enquanto não houver sensor conectado."
            )
        self._advance()

    def _probe_hardware(self) -> None:
        result = probe_project_ftdi(DRIVER_ROOT)
        connection_label, connection_detail = machine_connection_presentation(result)
        self.hardware_var.set(connection_label)
        self.status_var.set(connection_detail)

    def _schedule_auto_connect(self, delay_ms: int = AUTO_CONNECT_RETRY_MS) -> None:
        if getattr(self.machine.backend, "is_physical", False):
            return
        if self._auto_connect_job is None:
            self._auto_connect_job = self.after(delay_ms, self._auto_connect_hardware)

    def _auto_connect_hardware(self) -> None:
        self._auto_connect_job = None
        if getattr(self.machine.backend, "is_physical", False):
            return
        if getattr(self, "_running", False):
            self._schedule_auto_connect()
            return
        self._start_hardware_connection()

    @staticmethod
    def _attempt_hardware_connection() -> HardwareConnectionAttempt:
        result = probe_project_ftdi(DRIVER_ROOT)
        if not result.devices or result.dll_path is None:
            return HardwareConnectionAttempt(result)
        backend = NativeM231Backend(
            result.dll_path,
            device_index=result.devices[0].index,
        )
        try:
            backend.connect()
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            try:
                backend.disconnect()
            except (OSError, RuntimeError, TimeoutError, ValueError):
                pass
            return HardwareConnectionAttempt(result, error=error)
        return HardwareConnectionAttempt(result, backend=backend)

    def _start_hardware_connection(self) -> None:
        if self._connection_thread is not None and self._connection_thread.is_alive():
            return
        self._connection_cancelled.clear()
        self.status_var.set("Conectando à M231 sem bloquear a interface...")

        def worker() -> None:
            attempt = self._attempt_hardware_connection()
            if self._connection_cancelled.is_set():
                if attempt.backend is not None:
                    attempt.backend.disconnect()
                return
            self._connection_results.put(attempt)

        self._connection_thread = threading.Thread(
            target=worker,
            name="m231-connection",
            daemon=True,
        )
        self._connection_thread.start()
        self._poll_hardware_connection()

    def _poll_hardware_connection(self) -> None:
        self._connection_poll_job = None
        if self._closing:
            return
        try:
            attempt = self._connection_results.get_nowait()
        except queue.Empty:
            self._connection_poll_job = self.after(
                CONNECT_RESULT_POLL_MS,
                self._poll_hardware_connection,
            )
            return

        self._connection_thread = None
        if not self._apply_hardware_connection(attempt, automatic=True):
            self._schedule_auto_connect()

    def _apply_hardware_connection(
        self,
        attempt: HardwareConnectionAttempt,
        automatic: bool,
    ) -> bool:
        result = attempt.probe
        connection_label, connection_detail = machine_connection_presentation(result)
        self.hardware_var.set(connection_label)
        if not result.devices:
            self.status_var.set(connection_detail)
            return False
        if result.dll_path is None:
            self.status_var.set("Falha ao conectar: a DLL D2XX compatível não foi localizada.")
            return False
        if attempt.error is not None:
            connection_mode = "automaticamente" if automatic else "diretamente"
            self.status_var.set(
                f"Falha ao conectar {connection_mode} com a M231: {attempt.error}"
            )
            return False
        if attempt.backend is None:
            self.status_var.set("Falha ao conectar: a tentativa não retornou um controlador.")
            return False
        self.machine.set_backend(attempt.backend)
        self.hardware_var.set("MÁQUINA ONLINE — controle Python nativo")
        self.status_var.set(
            "Máquina conectada automaticamente por Python/D2XX. "
            "Comandos manuais, zeragens, retorno, execução, pausa e parada estão ativos."
        )
        return True

    def _connect_hardware(self, automatic: bool = False) -> bool:
        if automatic and getattr(self.machine.backend, "is_physical", False):
            return True
        return self._apply_hardware_connection(
            self._attempt_hardware_connection(),
            automatic=automatic,
        )

    def _manual_speed(self) -> float:
        speed = self._number(self.manual_speed_var.get())
        if speed <= 0:
            raise ValueError("A velocidade manual deve ser maior que zero.")
        return speed

    def _manual_jog(self, direction: int) -> None:
        if self._running:
            messagebox.showwarning("Controle manual", "Pare o ensaio antes do movimento manual.")
            return
        try:
            self.machine.jog(direction, self._manual_speed())
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            messagebox.showerror("Controle manual", str(error))
            return
        state = MachineState.JOG_UP if direction > 0 else MachineState.JOG_DOWN
        self._set_manual_motion_indicator(state)
        movement = "SUBINDO" if direction > 0 else "DESCENDO"
        self.status_var.set(f"Máquina {movement}. Use Parar para interromper.")

    def _manual_stop(self) -> None:
        self.machine.stop()
        self._return_direction = 0
        self._set_manual_motion_indicator(MachineState.STOPPED)
        self.status_var.set("Movimento manual parado.")

    def _return_position(self) -> None:
        if self._running:
            messagebox.showwarning("Retornar posição", "Pare o ensaio antes de retornar a posição.")
            return
        try:
            target_position = self._number(self.initial_var.get())
            return_speed = self._manual_speed()
            self.machine.return_position(target_position, return_speed)
        except (OSError, RuntimeError, TimeoutError, ValueError) as error:
            messagebox.showerror("Retornar posição", str(error))
            return
        current_position = self.__dict__.get("_last_machine_position")
        if current_position is not None:
            self._return_direction = (
                1 if target_position > current_position else -1
                if target_position < current_position else 0
            )
            self._set_manual_motion_indicator(MachineState.RETURNING)
        self.status_var.set(
            f"Retornando para {target_position:g} mm a {return_speed:g} mm/min."
        )

    def _set_manual_motion_indicator(self, state: MachineState) -> None:
        """Destaca a direção que a máquina está executando neste momento."""
        if not all(
            hasattr(self, name)
            for name in ("up_button", "manual_stop_button", "down_button")
        ):
            return

        direction = 0
        if state == MachineState.JOG_UP:
            direction = 1
        elif state == MachineState.JOG_DOWN:
            direction = -1
        elif state == MachineState.RETURNING:
            direction = self._return_direction

        self.up_button.configure(
            text="▲  SUBINDO..." if direction > 0 else "▲  SUBIR",
            style="MovementUpActive.TButton" if direction > 0 else "Movement.TButton",
        )
        self.down_button.configure(
            text="▼  DESCENDO..." if direction < 0 else "▼  DESCER",
            style="MovementDownActive.TButton" if direction < 0 else "Movement.TButton",
        )
        self.manual_stop_button.configure(
            text="■  PARAR AGORA" if direction else "■  PARAR",
            style="MovementStopActive.TButton" if direction else "MovementStop.TButton",
        )

    def _zero_position(self) -> None:
        if self._running:
            messagebox.showwarning("Zerar posição", "Pare o ensaio antes de zerar.")
            return
        self.machine.zero_position()
        self.status_var.set("Posição zerada.")

    def _zero_force(self) -> None:
        self.machine.zero_force()
        self.status_var.set("Carga zerada.")

    def _poll_machine(self) -> None:
        try:
            telemetry = self.machine.telemetry()
        except (OSError, RuntimeError, TimeoutError) as error:
            self.hardware_var.set("MÁQUINA OFFLINE — falha de comunicação")
            self.machine_state_var.set("Falha de comunicação")
            if getattr(self.machine.backend, "is_physical", False):
                was_running = getattr(self, "_running", False)
                self._running = False
                self._paused = False
                self._sample_iter = None
                self._pending_sample = None
                self._return_direction = 0
                try:
                    self.machine.disconnect()
                except (OSError, RuntimeError, TimeoutError):
                    pass
                self.machine.backend = LocalMachineBackend()
                if was_running and self.samples:
                    self._persist_result("conexao_perdida")
                self.status_var.set(
                    f"Conexão perdida: {error}. Aguardando a máquina para reconectar automaticamente."
                )
                self._schedule_auto_connect(0)
            else:
                self.status_var.set(str(error))
            self.after(500, self._poll_machine)
            return
        previous_position = self._last_machine_position
        previous_state = self.__dict__.get("_last_machine_state")
        if telemetry.state == MachineState.RETURNING and previous_position is not None:
            position_change = telemetry.position_mm - previous_position
            if abs(position_change) > 1e-9:
                self._return_direction = 1 if position_change > 0 else -1
        elif telemetry.state != MachineState.RETURNING:
            self._return_direction = 0
        self._last_machine_position = telemetry.position_mm
        self._last_force_kn = telemetry.force_kn
        self._last_machine_state = telemetry.state
        self._set_manual_motion_indicator(telemetry.state)
        force_unit = self._selected_force_unit()
        self.force_var.set(format_force(telemetry.force_kn, force_unit, 2))
        self.position_var.set(f"{telemetry.position_mm:.2f} mm".replace(".", ","))
        self.displacement_var.set(
            f"{telemetry.displacement_mm:.2f} mm".replace(".", ",")
        )
        state_labels = {
            MachineState.IDLE: "Pronta em modo local",
            MachineState.JOG_UP: "Máquina SUBINDO",
            MachineState.JOG_DOWN: "Máquina DESCENDO",
            MachineState.RETURNING: "Retornando posição",
            MachineState.RUNNING: "Ensaio em execução",
            MachineState.PAUSED: "Ensaio pausado",
            MachineState.STOPPED: "Parada",
            MachineState.ALARM: f"ALARME: {telemetry.alarm}",
            MachineState.OFFLINE: "Máquina offline",
        }
        self.machine_state_var.set(state_labels[telemetry.state])
        if telemetry.state != previous_state:
            status_messages = {
                MachineState.IDLE: (
                    "Máquina pronta e parada."
                    if telemetry.hardware_connected
                    else "Máquina virtual pronta em modo local."
                ),
                MachineState.JOG_UP: "Máquina SUBINDO.",
                MachineState.JOG_DOWN: "Máquina DESCENDO.",
                MachineState.RETURNING: "Máquina retornando para a posição inicial.",
                MachineState.RUNNING: "Ensaio em execução.",
                MachineState.PAUSED: "Máquina pausada.",
                MachineState.STOPPED: (
                    f"Máquina PARADA — {telemetry.alarm}."
                    if telemetry.alarm
                    else "Máquina PARADA."
                ),
                MachineState.ALARM: f"ALARME — {telemetry.alarm}",
                MachineState.OFFLINE: "Máquina offline.",
            }
            self.status_var.set(status_messages[telemetry.state])
        if telemetry.state == MachineState.ALARM and getattr(self, "_running", False):
            self._running = False
            self._paused = False
            self._sample_iter = None
            self._pending_sample = None
            self.stage_var.set("PARADA DE SEGURANÇA")
            self.status_var.set(f"Ensaio físico interrompido: {telemetry.alarm}")
            if self.samples:
                self._persist_result("alarme")
        self.after(100, self._poll_machine)

    def _close_app(self) -> None:
        try:
            self._closing = True
            self._connection_cancelled.set()
            if self._auto_connect_job is not None:
                self.after_cancel(self._auto_connect_job)
                self._auto_connect_job = None
            if self._connection_poll_job is not None:
                self.after_cancel(self._connection_poll_job)
                self._connection_poll_job = None
            while True:
                try:
                    attempt = self._connection_results.get_nowait()
                except queue.Empty:
                    break
                if attempt.backend is not None:
                    attempt.backend.disconnect()
            self.machine.disconnect()
        finally:
            self.destroy()

    def _advance(self) -> None:
        if not self._running:
            return
        if self._paused:
            self.after(50, self._advance)
            return
        try:
            if self._pending_sample is None:
                self._pending_sample = next(self._sample_iter)
            sample = self._pending_sample
        except StopIteration:
            self._running = False
            self.machine.complete()
            self.stage_var.set("Ensaio concluído")
            self._draw_chart()
            run_id = self._persist_result("concluído")
            self.status_var.set(
                f"Simulação concluída: {len(self.samples)} aquisições disponíveis "
                f"para exportação (intervalo de {self.program.acquisition_interval_mm:g} mm). "
                f"Execução #{run_id} salva no histórico."
            )
            return
        delay_ms = remaining_delay_ms(
            sample.time_s,
            self._started_monotonic,
            time.monotonic(),
        )
        if delay_ms > 0:
            self.after(delay_ms, self._advance)
            return
        self._pending_sample = None
        self.machine.apply_sample(sample)
        if getattr(self.machine.backend, "is_physical", False):
            physical = self.machine.telemetry()
            sample = replace(
                sample,
                force_kn=physical.force_kn,
                position_mm=physical.position_mm,
                displacement_mm=physical.displacement_mm,
                action=(
                    "limite_seguranca"
                    if physical.state == MachineState.ALARM
                    else sample.action
                ),
            )
        if sample.acquired:
            self.samples.append(sample)
            self._draw_chart()
            self._update_current_run_row()
        self._last_force_kn = sample.force_kn
        force_unit = self._selected_force_unit()
        self.force_var.set(format_force(sample.force_kn, force_unit, 2))
        self.position_var.set(f"{sample.position_mm:.2f} mm".replace(".", ","))
        self.displacement_var.set(f"{sample.displacement_mm:.2f} mm".replace(".", ","))
        self.time_var.set(f"{sample.time_s:.2f} s".replace(".", ","))
        if sample.action == "limite_seguranca":
            self._running = False
            self.machine.emergency_stop("Limite de carga excedido")
            self.stage_var.set("PARADA DE SEGURANÇA")
            self.status_var.set(
                f"Ensaio interrompido: carga {format_force(sample.force_kn, force_unit)} "
                f"ultrapassou o limite de "
                f"{format_force(self.program.force_limit_kn, force_unit)}."
            )
            self._draw_chart()
            self._persist_result("alarme")
            return
        self.stage_var.set(
            f"Ciclo {sample.cycle}/{self.program.cycles} · "
            f"{ACTION_LABELS.get(sample.action, 'Início')} · etapa {sample.step}"
        )
        self.after(1, self._advance)

    def _toggle_pause(self) -> None:
        if not self._running:
            return
        self._paused = not self._paused
        now = time.monotonic()
        if self._paused:
            self._pause_started_monotonic = now
            self.machine.pause()
        elif self._pause_started_monotonic is not None:
            self._started_monotonic += now - self._pause_started_monotonic
            self._pause_started_monotonic = None
            self.machine.resume()
        self.pause_button.configure(text="Continuar" if self._paused else "Pausar")
        self._update_current_run_row()
        self.status_var.set("Simulação pausada." if self._paused else "Simulação retomada.")

    def _stop(self) -> None:
        was_running = getattr(self, "_running", False)
        self._running = False
        self._paused = False
        self._sample_iter = None
        self._pending_sample = None
        self._pause_started_monotonic = None
        if hasattr(self, "machine"):
            self.machine.stop()
        if hasattr(self, "stage_var"):
            self.stage_var.set("Parado")
            self.status_var.set("Execução parada pela operação.")
        if was_running and self.samples:
            self._persist_result("interrompido")
        else:
            self._update_current_run_row()

    def _persist_result(self, status: str) -> int:
        if "repository" not in self.__dict__ or self._result_program is None:
            return 0
        mode = self.machine.mode_name
        run_id = self.repository.save_run(
            self._result_program,
            self.samples,
            mode,
            status,
        )
        session_runs = self.__dict__.setdefault("_session_runs", [])
        session_runs.insert(
            0,
            {
                "id": run_id,
                "started_at": (
                    self._current_run_started_at
                    or datetime.now(timezone.utc).isoformat()
                ),
                "program_name": self._result_program.name,
                "mode": mode,
                "status": status,
                "sample_count": len(self.samples),
            },
        )
        self._refresh_run_list()
        return run_id

    @staticmethod
    def _display_run_mode(mode: object) -> str:
        """Resume o modo tecnico para a coluna de conectividade da lista."""
        normalized_mode = str(mode).casefold()
        if "local" in normalized_mode or "offline" in normalized_mode:
            return "Offline"
        return "Online"

    def _active_run_values(self) -> tuple[object, ...]:
        program = self._result_program or self.program
        started_at = self._current_run_started_at or datetime.now(timezone.utc).isoformat()
        status = "pausado" if self._paused else "em execução"
        return (
            "Atual",
            started_at[:19].replace("T", " "),
            program.name,
            self._display_run_mode(self.machine.mode_name),
            status,
            len(self.samples),
        )

    @staticmethod
    def _sync_run_checkboxes(tree: ttk.Treeview) -> None:
        selected = set(tree.selection())
        for item in tree.get_children():
            tree.item(item, text="☑" if item in selected else "☐")

    def _toggle_run_checkbox(self, event: tk.Event) -> str | None:
        tree = event.widget
        if tree.identify_region(event.x, event.y) != "tree":
            return None
        item = tree.identify_row(event.y)
        if not item:
            return "break"
        if item in tree.selection():
            tree.selection_remove(item)
        else:
            tree.selection_add(item)
            tree.focus(item)
        self._sync_run_checkboxes(tree)
        return "break"

    def _enable_run_checkboxes(self, tree: ttk.Treeview) -> None:
        tree.bind("<Button-1>", self._toggle_run_checkbox, add="+")
        tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self._sync_run_checkboxes(tree),
            add="+",
        )

    def _update_current_run_row(self) -> None:
        tree = self.__dict__.get("run_list_tree")
        if tree is None or not tree.winfo_exists():
            return
        current_item = "current-run"
        if getattr(self, "_running", False) and self._result_program is not None:
            values = self._active_run_values()
            if tree.exists(current_item):
                tree.item(current_item, values=values)
            else:
                tree.insert(
                    "",
                    0,
                    iid=current_item,
                    text="☐",
                    values=values,
                    tags=("current",),
                )
            tree.tag_configure("current", background="#fff3cd")
        elif tree.exists(current_item):
            tree.delete(current_item)

    def _refresh_run_list(self) -> None:
        tree = self.__dict__.get("run_list_tree")
        if tree is None or not tree.winfo_exists():
            return
        selected_items = set(tree.selection())
        tree.delete(*tree.get_children())
        if getattr(self, "_running", False) and self._result_program is not None:
            tree.insert(
                "",
                "end",
                iid="current-run",
                text="☐",
                values=self._active_run_values(),
                tags=("current",),
            )
            tree.tag_configure("current", background="#fff3cd")
        for run in self.__dict__.get("_session_runs", []):
            item = tree.insert(
                "",
                "end",
                iid=f"run-{run['id']}",
                text="☐",
                values=(
                    run["id"],
                    str(run["started_at"])[:19].replace("T", " "),
                    run["program_name"],
                    self._display_run_mode(run["mode"]),
                    run["status"],
                    run["sample_count"],
                ),
            )
        restored_items = [
            item for item in selected_items if tree.exists(item)
        ]
        if restored_items:
            tree.selection_set(*restored_items)
        self._sync_run_checkboxes(tree)

    def _delete_selected_run(
        self,
        tree: ttk.Treeview | None = None,
        parent: tk.Misc | None = None,
        *,
        permanent: bool = False,
    ) -> bool:
        target = tree or self.__dict__.get("run_list_tree")
        parent = parent or self
        if target is None:
            return False
        selected_items = tuple(target.selection())
        if not selected_items:
            messagebox.showinfo(
                "Excluir ensaios",
                "Marque um ou mais ensaios na lista.",
                parent=parent,
            )
            return False

        selected_runs: list[tuple[str, tuple[object, ...]]] = []
        for item in selected_items:
            values = target.item(item, "values")
            if values:
                selected_runs.append((item, tuple(values)))
        if not selected_runs:
            return False

        includes_current = any(values[0] == "Atual" for _, values in selected_runs)
        stored_runs = [
            (item, int(values[0]), str(values[2]))
            for item, values in selected_runs
            if values[0] != "Atual"
        ]
        if permanent:
            if len(stored_runs) == 1:
                _, run_id, run_name = stored_runs[0]
                question = f"Excluir definitivamente o ensaio #{run_id} — {run_name}?"
                title = "Excluir ensaio do histórico"
            else:
                question = (
                    f"Excluir definitivamente os {len(stored_runs)} ensaios "
                    "selecionados do histórico?"
                )
                title = "Excluir ensaios do histórico"
        elif includes_current and stored_runs:
            question = (
                "O ensaio atual será parado e seus dados adquiridos serão descartados.\n\n"
                f"Também serão removidos da tela {len(stored_runs)} ensaio(s) "
                "desta sessão. Os ensaios já concluídos continuarão no "
                "histórico permanente. Continuar?"
            )
            title = "Remover ensaios da sessão"
        elif includes_current:
            question = (
                "O ensaio está em execução. Deseja pará-lo e descartar "
                "os dados adquiridos desta execução?"
            )
            title = "Descartar ensaio atual"
        elif len(stored_runs) == 1:
            question = (
                "Remover este ensaio da lista da sessão?\n\n"
                "Ele continuará salvo no histórico permanente."
            )
            title = "Remover da sessão"
        else:
            question = (
                f"Remover os {len(stored_runs)} ensaios selecionados da lista "
                "desta sessão?\n\nEles continuarão salvos no histórico permanente."
            )
            title = "Remover da sessão"
        if not messagebox.askyesno(title, question, parent=parent):
            return False

        deleted_count = 0
        if includes_current:
            try:
                self.machine.stop()
            except (OSError, RuntimeError, TimeoutError) as error:
                messagebox.showerror(
                    "Excluir ensaio atual",
                    f"Não foi possível parar a máquina com segurança:\n{error}",
                    parent=parent,
                )
                return False
            self._running = False
            self._paused = False
            self._sample_iter = None
            self._pending_sample = None
            self._pause_started_monotonic = None
            self.samples = []
            self._result_program = None
            self.stage_var.set("Aguardando")
            self.time_var.set("0,00 s")
            self._draw_chart()
            deleted_count += 1

        missing_ids: list[int] = []
        removed_items: list[str] = []
        removed_ids: set[int] = set()
        for item, run_id, _run_name in stored_runs:
            if not permanent or self.repository.delete_run(run_id):
                deleted_count += 1
                removed_items.append(item)
                removed_ids.add(run_id)
            else:
                missing_ids.append(run_id)

        if removed_ids:
            session_runs = self.__dict__.get("_session_runs", [])
            self._session_runs = [
                run for run in session_runs if int(run["id"]) not in removed_ids
            ]

        main_tree = self.__dict__.get("run_list_tree")
        if target is not main_tree:
            for item in removed_items:
                if target.exists(item):
                    target.delete(item)
            self._sync_run_checkboxes(target)
        self._refresh_run_list()

        if missing_ids:
            missing_text = ", ".join(f"#{run_id}" for run_id in missing_ids)
            messagebox.showerror(
                "Excluir ensaios",
                f"Não foi possível encontrar: {missing_text}.",
                parent=parent,
            )
        if deleted_count:
            if deleted_count == 1:
                status = (
                    "1 ensaio foi excluído do histórico permanente."
                    if permanent
                    else "1 ensaio foi removido da lista desta sessão."
                )
            else:
                status = (
                    f"{deleted_count} ensaios foram excluídos do histórico permanente."
                    if permanent
                    else f"{deleted_count} ensaios foram removidos da lista desta sessão."
                )
            self.status_var.set(status)
        return deleted_count > 0

    def _show_history(self) -> None:
        window = tk.Toplevel(self)
        window.title("Histórico permanente e relatórios de ensaios")
        window.geometry("900x430")
        columns = ("id", "date", "name", "mode", "status", "samples")
        tree = ttk.Treeview(
            window, columns=columns, show="tree headings", selectmode="extended"
        )
        tree.heading("#0", text="Sel.", anchor="center")
        tree.column("#0", width=46, minwidth=46, stretch=False, anchor="center")
        self._enable_run_checkboxes(tree)
        headings = {
            "id": "#",
            "date": "Data UTC",
            "name": "Ensaio",
            "mode": "Modo",
            "status": "Estado",
            "samples": "Aquisições",
        }
        for column in columns:
            tree.heading(column, text=headings[column], anchor="center")
        tree.column("id", width=45, anchor="center")
        tree.column("date", width=175, anchor="center")
        tree.column("name", width=180, anchor="center")
        tree.column("mode", width=150, anchor="center")
        tree.column("status", width=90, anchor="center")
        tree.column("samples", width=85, anchor="center")
        first_item = None
        for run in self.repository.recent_runs():
            item = tree.insert(
                "",
                "end",
                text="☐",
                values=(
                    run["id"],
                    str(run["started_at"])[:19].replace("T", " "),
                    run["program_name"],
                    self._display_run_mode(run["mode"]),
                    run["status"],
                    run["sample_count"],
                ),
            )
            if first_item is None:
                first_item = item
        tree.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        if first_item is not None:
            tree.selection_set(first_item)
            tree.focus(first_item)
            self._sync_run_checkboxes(tree)

        controls = ttk.Frame(window, padding=(10, 4, 10, 10))
        controls.pack(fill="x")
        ttk.Label(
            controls,
            text="Marque vários para excluir; PDF e CSV usam o ensaio em foco.",
            foreground="#555",
        ).pack(side="left")

        def selected_run():
            selection = tree.selection()
            if not selection:
                messagebox.showinfo(
                    "Histórico",
                    "Selecione um ensaio na lista.",
                    parent=window,
                )
                return None
            focused = tree.focus()
            item = focused if focused in selection else selection[0]
            run_id = int(tree.item(item, "values")[0])
            run = self.repository.get_run(run_id)
            if run is None:
                messagebox.showerror(
                    "Histórico",
                    "O ensaio selecionado não foi encontrado.",
                    parent=window,
                )
            return run

        def generate_report() -> None:
            run = selected_run()
            if run is None:
                return
            path = filedialog.asksaveasfilename(
                parent=window,
                title="Salvar relatório do ensaio",
                initialfile=report_filename(run),
                defaultextension=".pdf",
                filetypes=[("Relatório PDF", "*.pdf")],
            )
            if not path:
                return
            try:
                generate_run_report_pdf(path, run)
            except OSError as error:
                messagebox.showerror(
                    "Gerar relatório",
                    f"Não foi possível salvar o relatório:\n{error}",
                    parent=window,
                )
                return
            self.status_var.set(f"Relatório #{run.id} gerado: {Path(path).name}")
            messagebox.showinfo(
                "Relatório gerado",
                f"O relatório do ensaio #{run.id} foi salvo com sucesso.",
                parent=window,
            )

        def export_historical_csv() -> None:
            run = selected_run()
            if run is None:
                return
            path = filedialog.asksaveasfilename(
                parent=window,
                title="Exportar aquisições do ensaio",
                initialfile=f"ensaio-{run.id:04d}.csv",
                defaultextension=".csv",
                filetypes=[("Arquivo CSV", "*.csv")],
            )
            if not path:
                return
            try:
                write_results_csv(path, run.samples, run.program)
            except OSError as error:
                messagebox.showerror(
                    "Exportar CSV",
                    f"Não foi possível salvar o arquivo:\n{error}",
                    parent=window,
                )
                return
            self.status_var.set(f"CSV do ensaio #{run.id} exportado: {Path(path).name}")

        ttk.Button(
            controls,
            text="Gerar relatório PDF",
            command=generate_report,
        ).pack(side="right", padx=(6, 0))
        ttk.Button(
            controls,
            text="Exportar CSV selecionado",
            command=export_historical_csv,
        ).pack(side="right")
        ttk.Button(
            controls,
            text="Excluir definitivamente",
            style="Danger.TButton",
            command=lambda: self._delete_selected_run(
                tree, window, permanent=True
            ),
        ).pack(side="right", padx=(0, 6))
        tree.bind("<Double-1>", lambda _event: generate_report())

    def _draw_chart(self) -> None:
        canvas = self.chart
        canvas.delete("all")
        width = max(canvas.winfo_width(), 200)
        height = max(canvas.winfo_height(), 160)
        left_margin = 62
        right_margin = 62
        top_margin = 34
        bottom_margin = 52
        plot_left = left_margin
        plot_right = width - right_margin
        plot_top = top_margin
        plot_bottom = height - bottom_margin
        plot_width = max(plot_right - plot_left, 1)
        plot_height = max(plot_bottom - plot_top, 1)
        chart_samples = acquired_samples(self.samples)
        if not chart_samples:
            canvas.create_line(plot_left, plot_top, plot_left, plot_bottom, fill="#777")
            canvas.create_line(plot_left, plot_bottom, plot_right, plot_bottom, fill="#777")
            canvas.create_line(plot_right, plot_top, plot_right, plot_bottom, fill="#777")
            canvas.create_text(8, 12, text="Compressão (mm)", anchor="nw", fill="#0066cc")
            canvas.create_text(width - 8, 12, text="Retorno (mm)", anchor="ne", fill="#d76500")
            canvas.create_text(
                (plot_left + plot_right) / 2,
                height - 6,
                text="Tempo (s)",
                anchor="s",
                fill="#555",
            )
            canvas.create_text(
                width / 2,
                height / 2,
                text="Execute uma simulação para visualizar as aquisições",
                fill="#888",
            )
            return
        result_program = self._result_program or self.program
        measured_max_time = max(sample.time_s for sample in chart_samples)
        max_time = measured_max_time or 1.0
        position_min = result_program.position_min_mm
        position_range = result_program.position_max_mm - position_min
        position_max = result_program.position_max_mm

        compression_positions = chart_position_marks(
            chart_samples, {"inicio", "compressao", "manter"}
        )
        return_positions = chart_position_marks(
            chart_samples, {"aguardar_descompressao", "descompressao"}
        )
        grid_positions = spaced_chart_marks(
            chart_position_marks(chart_samples),
            position_min,
            position_max,
            plot_height,
        )
        compression_marks = spaced_chart_marks(
            compression_positions,
            position_min,
            position_max,
            plot_height,
        )
        return_marks = spaced_chart_marks(
            return_positions,
            position_min,
            position_max,
            plot_height,
        )

        def position_y(position: float) -> float:
            return plot_bottom - (position - position_min) / position_range * plot_height

        for position in grid_positions:
            y = position_y(position)
            canvas.create_line(
                plot_left,
                y,
                plot_right,
                y,
                fill="#d8dee8",
                dash=(3, 3),
            )

        for position in compression_marks:
            y = position_y(position)
            canvas.create_line(plot_left - 4, y, plot_left, y, fill="#0066cc")
            canvas.create_text(
                plot_left - 8,
                y,
                text=format_csv_number(position),
                anchor="e",
                fill="#0059b3",
            )

        for position in return_marks:
            y = position_y(position)
            canvas.create_line(plot_right, y, plot_right + 4, y, fill="#d76500")
            canvas.create_text(
                plot_right + 8,
                y,
                text=format_csv_number(position),
                anchor="w",
                fill="#b45100",
            )

        canvas.create_line(plot_left, plot_top, plot_left, plot_bottom, fill="#777")
        canvas.create_line(plot_left, plot_bottom, plot_right, plot_bottom, fill="#777")
        canvas.create_line(plot_right, plot_top, plot_right, plot_bottom, fill="#777")
        canvas.create_text(8, 12, text="Compressão (mm)", anchor="nw", fill="#0066cc")
        canvas.create_text(width - 8, 12, text="Retorno (mm)", anchor="ne", fill="#d76500")
        canvas.create_text(
            (plot_left + plot_right) / 2,
            height - 6,
            text="Tempo (s)",
            anchor="s",
            fill="#555",
        )

        marker_points: list[tuple[float, float, str, Sample]] = []
        previous_point: tuple[float, float] | None = None
        for sample in chart_samples:
            x = plot_left + sample.time_s / max_time * plot_width
            y = position_y(sample.position_mm)
            color = "#d76500" if sample.action in {
                "aguardar_descompressao",
                "descompressao",
            } else "#0066cc"
            if previous_point is not None:
                canvas.create_line(*previous_point, x, y, fill=color, width=2)
            previous_point = (x, y)
            marker_points.append((x, y, color, sample))
        for x, y, color, sample in marker_points:
            canvas.create_oval(
                x - 3,
                y - 3,
                x + 3,
                y + 3,
                fill=color,
                outline="#ffffff",
                width=1,
            )
            is_return = sample.action in {
                "aguardar_descompressao",
                "descompressao",
            }
            prefer_right = is_return
            label_x = x + 8 if prefer_right else x - 8
            label_anchor = "w" if prefer_right else "e"
            if label_x < plot_left + 4:
                label_x = x + 8
                label_anchor = "w"
            elif label_x > plot_right - 4:
                label_x = x - 8
                label_anchor = "e"
            vertical_offset = 10 if is_return else -10
            label_y = min(
                max(y + vertical_offset, plot_top + 8),
                plot_bottom - 8,
            )
            canvas.create_text(
                label_x,
                label_y,
                text=format_chart_force(sample.force_kn, result_program.force_unit),
                anchor=label_anchor,
                fill=color,
                font=("TkDefaultFont", 8),
            )
        canvas.create_text(
            width / 2,
            12,
            text=(
                f"{len(chart_samples)} aquisições · "
                f"intervalo {result_program.acquisition_interval_mm:g} mm"
            ),
            anchor="n",
            fill="#555",
        )
        canvas.create_text(
            plot_right,
            plot_bottom + 6,
            text=format_csv_number(measured_max_time),
            anchor="n",
            fill="#666",
        )

    def _export_csv(self) -> None:
        if not self.samples:
            messagebox.showinfo("Exportar CSV", "Execute uma simulação antes de exportar.")
            return
        if self._running:
            messagebox.showinfo(
                "Exportar CSV", "Aguarde o término ou pare a simulação antes de exportar."
            )
            return
        try:
            current_program = self._collect_program()
        except (ValueError, TypeError) as error:
            messagebox.showerror("Programação inválida", str(error))
            return
        if self._result_program is None or current_program != self._result_program:
            messagebox.showwarning(
                "Resultado desatualizado",
                "A programação foi alterada depois da simulação. "
                "Execute o ensaio novamente para gerar um CSV compatível com os valores atuais.",
            )
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("Arquivo CSV", "*.csv")])
        if not path:
            return
        write_results_csv(path, self.samples, self._result_program)
        self.status_var.set(f"Resultado exportado: {Path(path).name}")


class StepDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, step: TestStep) -> None:
        super().__init__(parent)
        self.title("Configurar etapa")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: TestStep | None = None
        self.action_var = tk.StringVar(value=ACTION_LABELS[step.action])
        self.target_var = tk.StringVar(value=f"{step.target_position_mm:g}")
        self.speed_var = tk.StringVar(value=f"{step.speed_mm_min:g}")
        self.duration_var = tk.StringVar(value=f"{step.duration_s:g}")

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Ação").grid(row=0, column=0, sticky="w")
        action_box = ttk.Combobox(frame, textvariable=self.action_var, values=list(LABEL_ACTIONS), state="readonly", width=26)
        action_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        action_box.bind("<<ComboboxSelected>>", lambda _event: self._update_fields())
        ttk.Label(frame, text="Posição-alvo (mm)").grid(row=2, column=0, sticky="w")
        self.target_entry = ttk.Entry(frame, textvariable=self.target_var)
        self.target_entry.grid(row=3, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))
        ttk.Label(frame, text="Velocidade (mm/min)").grid(row=2, column=1, sticky="w")
        self.speed_entry = ttk.Entry(frame, textvariable=self.speed_var)
        self.speed_entry.grid(row=3, column=1, sticky="ew", pady=(0, 8))
        self.return_note = ttk.Label(frame, text="No retorno, posição inicial e velocidade da compressão são automáticas.", foreground="#555")
        self.return_note.grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="Tempo de manutenção (s)").grid(row=5, column=0, columnspan=2, sticky="w")
        self.duration_entry = ttk.Entry(frame, textvariable=self.duration_var)
        self.duration_entry.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="Aplicar", command=self._accept).pack(side="left")
        self.bind("<Return>", lambda _event: self._accept())
        self.bind("<Escape>", lambda _event: self.destroy())
        self._update_fields()
        self.wait_visibility()
        self.focus_set()

    def _update_fields(self) -> None:
        action = LABEL_ACTIONS[self.action_var.get()]
        self.target_entry.configure(state="normal" if action == "compressao" else "disabled")
        self.speed_entry.configure(state="normal" if action == "compressao" else "disabled")
        self.duration_entry.configure(state="normal" if action == "manter" else "disabled")
        self.return_note.configure(
            foreground="#8a3300" if action == "descompressao" else "#777"
        )

    def _accept(self) -> None:
        try:
            action = LABEL_ACTIONS[self.action_var.get()]
            self.result = TestStep(
                action=action,
                target_position_mm=float(self.target_var.get().replace(",", ".")) if action == "compressao" else 0.0,
                speed_mm_min=float(self.speed_var.get().replace(",", ".")) if action == "compressao" else 0.0,
                duration_s=float(self.duration_var.get().replace(",", ".")) if action == "manter" else 0.0,
            )
            if action == "manter" and self.result.duration_s <= 0:
                raise ValueError("Informe um tempo maior que zero.")
            if action == "compressao" and self.result.speed_mm_min <= 0:
                raise ValueError("Informe uma velocidade maior que zero.")
        except (ValueError, KeyError) as error:
            messagebox.showerror("Valor inválido", str(error), parent=self)
            return
        self.destroy()
