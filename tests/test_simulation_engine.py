"""Testes do motor de programação e simulação de ensaios."""

from dataclasses import asdict
import json
import tempfile
from pathlib import Path
import unittest

from m231pro.core.simulation import (
    SimulationEngine,
    TestProgram,
    TestStep,
    compression_return_template,
    force_from_kn,
    force_to_kn,
)


class TestSimulationEngine(unittest.TestCase):
    def test_force_units_convert_to_internal_kn_and_back(self):
        expected = {
            "N": 1000.0,
            "kN": 1.0,
            "lbf": 224.808943,
            "kgf": 101.971621,
        }
        for unit, displayed_value in expected.items():
            with self.subTest(unit=unit):
                self.assertAlmostEqual(
                    1.0, force_to_kn(displayed_value, unit), places=5
                )
                self.assertAlmostEqual(
                    displayed_value, force_from_kn(1.0, unit), places=5
                )

    def test_program_rejects_unknown_force_unit(self):
        program = compression_return_template()
        program.force_unit = "kg"

        with self.assertRaisesRegex(ValueError, "unidade de carga"):
            program.validate()

    def test_compression_reaches_position_and_return_uses_same_speed(self):
        program = compression_return_template()
        samples = list(SimulationEngine(program, sample_period_s=0.1).samples())

        self.assertEqual(50.0, max(sample.position_mm for sample in samples))
        self.assertEqual(program.initial_position_mm, samples[-1].position_mm)
        compression_time = next(
            sample.time_s for sample in samples if sample.position_mm == 50.0
        )
        final_time = samples[-1].time_s
        self.assertAlmostEqual(30.0, compression_time, places=6)
        self.assertAlmostEqual(62.0, final_time, places=6)

    def test_force_is_only_a_safety_limit_and_stops_motion(self):
        program = compression_return_template()

        def force_sensor(_position, displacement, _time):
            return displacement * 2.0

        engine = SimulationEngine(program, 0.1, force_sensor)
        samples = list(engine.samples())

        self.assertTrue(engine.stopped_by_safety)
        self.assertEqual("limite_seguranca", samples[-1].action)
        self.assertGreater(abs(samples[-1].force_kn), 61.0)
        self.assertLess(samples[-1].position_mm, 50.0)

    def test_default_simulation_does_not_invent_sensor_load(self):
        samples = list(SimulationEngine(compression_return_template()).samples())

        self.assertTrue(all(sample.force_kn == 0.0 for sample in samples))

    def test_waits_configured_time_before_decompression(self):
        program = compression_return_template()
        program.wait_before_decompression_s = 7.5
        samples = list(SimulationEngine(program, sample_period_s=0.1).samples())

        compression_end = next(
            sample.time_s
            for sample in samples
            if sample.action == "compressao" and sample.position_mm == 50.0
        )
        wait_end = max(
            sample.time_s
            for sample in samples
            if sample.action == "aguardar_descompressao"
        )
        decompression_start = next(
            sample.time_s for sample in samples if sample.action == "descompressao"
        )

        self.assertAlmostEqual(7.5, wait_end - compression_end, places=6)
        self.assertGreater(decompression_start, wait_end)
        wait_events = [
            sample
            for sample in samples
            if sample.action == "aguardar_descompressao" and sample.acquired
        ]
        self.assertEqual(1, len(wait_events))
        self.assertAlmostEqual(wait_end, wait_events[0].time_s, places=6)

    def test_export_timeline_for_reported_500_mm_scenario_is_consistent(self):
        program = TestProgram(
            name="Cenario 500 mm",
            force_limit_kn=61,
            position_min_mm=0,
            position_max_mm=500,
            initial_position_mm=0,
            wait_before_decompression_s=20,
            acquisition_interval_mm=100,
            steps=[TestStep("compressao", 500, 100), TestStep("descompressao")],
        )
        samples = [
            sample
            for sample in SimulationEngine(program, sample_period_s=0.1).samples()
            if sample.acquired
        ]

        self.assertEqual(
            [0, 60, 120, 180, 240, 300, 320, 380, 440, 500, 560, 620],
            [round(sample.time_s) for sample in samples],
        )
        self.assertEqual("aguardar_descompressao", samples[6].action)
        self.assertEqual(500, samples[6].position_mm)

    def test_acquisition_points_follow_configured_distance(self):
        program = compression_return_template()
        program.wait_before_decompression_s = 0
        program.acquisition_interval_mm = 10
        samples = list(SimulationEngine(program, sample_period_s=0.7).samples())

        acquired_positions = [
            round(sample.position_mm, 6) for sample in samples if sample.acquired
        ]

        self.assertEqual(
            [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 40.0, 30.0, 20.0, 10.0, 0.0],
            acquired_positions,
        )

    def test_target_is_acquired_when_distance_is_not_multiple_of_interval(self):
        program = compression_return_template()
        program.steps[0].target_position_mm = 25
        program.wait_before_decompression_s = 0
        program.acquisition_interval_mm = 10
        samples = list(SimulationEngine(program, sample_period_s=0.7).samples())

        acquired_positions = [
            round(sample.position_mm, 6) for sample in samples if sample.acquired
        ]

        self.assertEqual([0.0, 10.0, 20.0, 25, 15.0, 5.0, 0.0], acquired_positions)

    def test_cycles_repeat_and_return_to_initial_position(self):
        program = TestProgram(
            name="Ciclos",
            force_limit_kn=61,
            position_min_mm=-10,
            position_max_mm=30,
            initial_position_mm=5,
            cycles=3,
            steps=[
                TestStep("compressao", 20, 60),
                TestStep("descompressao"),
            ],
        )
        samples = list(SimulationEngine(program, 0.5).samples())

        self.assertEqual({1, 2, 3}, {sample.cycle for sample in samples})
        peaks = [sample for sample in samples if sample.position_mm == 20]
        self.assertEqual(3, len(peaks))
        self.assertEqual(5, samples[-1].position_mm)

    def test_rejects_position_outside_configured_limits(self):
        program = TestProgram(
            position_min_mm=0,
            position_max_mm=50,
            steps=[TestStep("compressao", 51, 100)],
        )
        with self.assertRaisesRegex(ValueError, "posição-alvo"):
            program.validate()

    def test_rejects_program_without_compression(self):
        program = TestProgram(steps=[TestStep("manter", duration_s=1)])
        with self.assertRaisesRegex(ValueError, "etapa de compressão"):
            program.validate()

    def test_rejects_invalid_wait_and_acquisition_interval(self):
        program = compression_return_template()
        program.wait_before_decompression_s = -1
        with self.assertRaisesRegex(ValueError, "espera"):
            program.validate()

        program.wait_before_decompression_s = 0
        program.acquisition_interval_mm = 0
        with self.assertRaisesRegex(ValueError, "intervalo de aquisição"):
            program.validate()

    def test_program_round_trip_json(self):
        original = compression_return_template()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ensaio.json"
            original.save(path)
            loaded = TestProgram.load(path)

        self.assertEqual(original, loaded)

    def test_program_without_saved_force_unit_defaults_to_kn(self):
        data = asdict(compression_return_template())
        del data["force_unit"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ensaio_antigo.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            loaded = TestProgram.load(path)

        self.assertEqual("kN", loaded.force_unit)
        self.assertEqual(61.0, loaded.force_limit_kn)


if __name__ == "__main__":
    unittest.main()
