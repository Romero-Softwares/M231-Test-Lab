import tempfile
from pathlib import Path
import unittest

from m231pro.core.reporting import generate_run_report_pdf, report_filename
from m231pro.core.simulation import Sample, compression_return_template
from m231pro.core.storage import ResultsRepository
from m231pro.hardware.backend import LocalMachineBackend, MachineState


class TestIntegrationComponents(unittest.TestCase):
    def test_return_position_moves_to_test_start_at_configured_speed(self):
        machine = LocalMachineBackend(-100.0, 100.0)
        machine.jog(1, 60.0)
        telemetry = machine.tick(5.0)

        self.assertEqual(5.0, telemetry.position_mm)
        machine.stop()
        machine.return_position(-2.0, 120.0)

        telemetry = machine.tick(2.0)
        self.assertEqual(1.0, telemetry.position_mm)
        self.assertEqual(MachineState.RETURNING, telemetry.state)
        telemetry = machine.tick(2.0)

        self.assertEqual(-2.0, telemetry.position_mm)
        self.assertEqual(-2.0, telemetry.displacement_mm)
        self.assertEqual(MachineState.IDLE, telemetry.state)

    def test_return_position_can_move_up_to_positive_test_start(self):
        machine = LocalMachineBackend(-100.0, 100.0)
        machine.jog(-1, 60.0)
        machine.tick(5.0)
        machine.stop()

        machine.return_position(2.0, 60.0)
        telemetry = machine.tick(10.0)

        self.assertEqual(2.0, telemetry.position_mm)
        self.assertEqual(MachineState.IDLE, telemetry.state)

    def test_manual_descent_from_zero_produces_negative_signed_readings(self):
        machine = LocalMachineBackend()

        machine.jog(-1, 60.0)
        telemetry = machine.tick(5.0)

        self.assertEqual(-5.0, telemetry.position_mm)
        self.assertEqual(-5.0, telemetry.displacement_mm)
        self.assertEqual(MachineState.JOG_DOWN, telemetry.state)

    def test_default_program_allows_negative_positions(self):
        program = compression_return_template()

        self.assertLess(program.position_min_mm, 0.0)

    def test_results_repository_persists_run_and_samples(self):
        program = compression_return_template()
        samples = [Sample(0.0, 0.0, 0.0, 0.0, 1, 0, "inicio", True)]
        with tempfile.TemporaryDirectory() as directory:
            repository = ResultsRepository(Path(directory) / "results.sqlite3")
            run_id = repository.save_run(program, samples, "local", "concluido")
            recent = repository.recent_runs()

        self.assertEqual(run_id, recent[0]["id"])
        self.assertEqual(program.name, recent[0]["program_name"])
        self.assertEqual(1, recent[0]["sample_count"])

    def test_results_repository_reopens_complete_run_for_reporting(self):
        program = compression_return_template()
        program.force_unit = "N"
        samples = [
            Sample(0.0, 0.0, 0.0, 0.0, 1, 0, "inicio", True),
            Sample(6.0, 1.25, 10.0, 10.0, 1, 1, "compressao", True),
        ]
        with tempfile.TemporaryDirectory() as directory:
            repository = ResultsRepository(Path(directory) / "results.sqlite3")
            run_id = repository.save_run(program, samples, "máquina física", "concluído")
            run = repository.get_run(run_id)

        self.assertIsNotNone(run)
        self.assertEqual(run_id, run.id)
        self.assertEqual("N", run.program.force_unit)
        self.assertEqual(program.steps, run.program.steps)
        self.assertEqual(samples, run.samples)
        self.assertEqual("máquina física", run.mode)

    def test_results_repository_deletes_run_and_its_samples(self):
        program = compression_return_template()
        samples = [Sample(0.0, 0.0, 0.0, 0.0, 1, 0, "inicio", True)]
        with tempfile.TemporaryDirectory() as directory:
            repository = ResultsRepository(Path(directory) / "results.sqlite3")
            deleted_id = repository.save_run(program, samples, "local", "concluído")
            kept_id = repository.save_run(program, samples, "local", "concluído")

            deleted = repository.delete_run(deleted_id)

            self.assertTrue(deleted)
            self.assertIsNone(repository.get_run(deleted_id))
            self.assertIsNotNone(repository.get_run(kept_id))
            self.assertFalse(repository.delete_run(deleted_id))

    def test_generates_standalone_pdf_report_from_historical_run(self):
        program = compression_return_template()
        samples = [
            Sample(0.0, 0.0, 0.0, 0.0, 1, 0, "inicio", True),
            Sample(6.0, 1.25, 10.0, 10.0, 1, 1, "compressao", True),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = ResultsRepository(root / "results.sqlite3")
            run_id = repository.save_run(program, samples, "local", "concluído")
            run = repository.get_run(run_id)
            report = root / report_filename(run)

            generate_run_report_pdf(report, run)
            content = report.read_bytes()

        self.assertTrue(content.startswith(b"%PDF-1.4"))
        self.assertIn(b"/Type /Catalog", content)
        self.assertIn(b"/Count 2", content)
        self.assertTrue(content.endswith(b"%%EOF\n"))
        self.assertGreater(len(content), 3000)


if __name__ == "__main__":
    unittest.main()
