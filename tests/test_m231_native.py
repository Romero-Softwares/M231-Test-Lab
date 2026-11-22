import struct
import unittest

from m231pro.hardware.backend import LocalMachineBackend, MachineState
from m231pro.core.simulation import Sample
from m231pro.hardware.m231 import (
    M231Calibration,
    M231FrameDecoder,
    NativeM231Backend,
    decode_7bit_block,
    encode_command,
    motion_frames,
)
from usb_machine_emulator.protocol import VIRTUAL_POSITION_ZERO_STATUS


def telemetry_frame(
    load=1000,
    position_analog=2000,
    extensometer=3000,
    auxiliary=4000,
    encoder=5000,
    status=(1, 2, 3, 4),
):
    raw = struct.pack(
        "<iiiiiBBBBI",
        load,
        position_analog,
        extensometer,
        auxiliary,
        encoder,
        *status,
        0,
    )
    return encode_command(0x7F, raw)


class FakeTransport:
    def __init__(self, initial=b""):
        self.is_open = False
        self.received = bytearray(initial)
        self.writes = []
        self.virtual_session_resets = 0

    def open(self, index=0):
        self.is_open = True
        self.index = index

    def close(self):
        self.is_open = False

    def write(self, payload):
        self.writes.append(payload)
        return len(payload)

    def reset_virtual_session(self):
        self.virtual_session_resets += 1

    def read_available(self, maximum=65536):
        data = bytes(self.received[:maximum])
        del self.received[:maximum]
        return data


class TestM231Protocol(unittest.TestCase):
    def test_virtual_checkpoint_rejects_zeroed_resume_values(self):
        backend = LocalMachineBackend()
        saved = Sample(12.0, 0.032, 18.5, 18.5, 1, 1, "tracao", True)
        backend.apply_sample(saved)
        checkpoint = backend.checkpoint_state()

        backend.apply_sample(Sample(12.0, 0.0, 0.0, 0.0, 1, 1, "tracao", True))
        backend.restore_checkpoint(checkpoint)

        telemetry = backend.telemetry()
        self.assertAlmostEqual(0.032, telemetry.force_kn)
        self.assertAlmostEqual(18.5, telemetry.position_mm)
        backend.resume_test(saved, 1.3)
        resumed = backend.telemetry()
        self.assertAlmostEqual(0.032, resumed.force_kn)
        self.assertAlmostEqual(18.5, resumed.position_mm, places=5)

    def test_7bit_codec_preserves_binary_values(self):
        raw = bytes((0x00, 0x7F, 0x80, 0xFF))
        frame = encode_command(ord("0"), raw)

        self.assertEqual(7, len(frame))
        self.assertEqual(raw, decode_7bit_block(frame[1:6]))

    def test_motion_uses_both_controller_channels_and_signed_float(self):
        up, mirror = motion_frames(-75.5)

        self.assertEqual(ord("0"), up[0])
        self.assertEqual(ord("X"), mirror[0])
        self.assertEqual(up[1:], mirror[1:])
        decoded = decode_7bit_block(up[1:6])
        self.assertAlmostEqual(-75.5, struct.unpack("<f", decoded)[0])

    def test_virtual_backend_zero_commands_are_sent_only_to_virtual_dll(self):
        transport = FakeTransport(telemetry_frame(load=1000, encoder=5000))
        backend = NativeM231Backend(
            "virtual_d2xx64.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )
        backend.connect()
        backend.zero_position()
        backend.zero_force()

        self.assertEqual([ord("P"), ord("F")], [payload[0] for payload in transport.writes[-2:]])

    def test_virtual_backend_starts_a_new_emulator_session_on_connect(self):
        transport = FakeTransport(telemetry_frame(load=1000, encoder=5000))
        backend = NativeM231Backend(
            "virtual_d2xx64.dll",
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )

        backend.connect()

        self.assertEqual(1, transport.virtual_session_resets)

    def test_decodes_fragmented_telemetry(self):
        frame = telemetry_frame(10, 20, 30, 40, 50)
        decoder = M231FrameDecoder()

        self.assertEqual([], decoder.feed(frame[:9]))
        decoded = decoder.feed(frame[9:])

        self.assertEqual(10, decoded[0].load_counts)
        self.assertEqual(50, decoded[0].encoder_counts)
        self.assertEqual(4, decoded[0].limit_state)

    def test_connection_timeout_reports_when_controller_sends_no_bytes(self):
        transport = FakeTransport()
        backend = NativeM231Backend(
            "unused.dll",
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.01,
        )

        with self.assertRaisesRegex(TimeoutError, "Nenhum byte foi recebido"):
            backend.connect()

    def test_connection_timeout_reports_unexpected_bytes(self):
        transport = FakeTransport(b"bad-frame\r")
        backend = NativeM231Backend(
            "unused.dll",
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.01,
        )

        with self.assertRaisesRegex(TimeoutError, "62 61 64 2d 66 72 61 6d 65 0d"):
            backend.connect()

    def test_native_backend_connects_reads_and_controls_without_executable(self):
        transport = FakeTransport(telemetry_frame(load=1000, encoder=5000))
        backend = NativeM231Backend(
            "unused.dll",
            device_index=2,
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )

        backend.connect()
        backend.jog(1, 60.0)

        self.assertEqual(2, transport.index)
        self.assertEqual(MachineState.JOG_UP, backend.telemetry().state)
        self.assertEqual(2, len(transport.writes))
        self.assertEqual((ord("0"), ord("X")), tuple(item[0] for item in transport.writes))

    def test_reopen_autozeroes_first_frame_without_reusing_manual_state(self):
        transport = FakeTransport(telemetry_frame(load=2114, encoder=3200))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )

        backend.connect()
        telemetry = backend.telemetry()

        self.assertAlmostEqual(0.0, telemetry.force_kn, places=3)
        self.assertAlmostEqual(0.0, telemetry.position_mm, places=5)

    def test_checkpoint_blocks_resume_when_real_load_or_position_changed(self):
        transport = FakeTransport(telemetry_frame(load=1000, encoder=5000))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )

        backend.connect()
        checkpoint = backend.checkpoint_state()
        self.assertIn("force_kn", checkpoint)
        self.assertIn("position_mm", checkpoint)
        transport.received.extend(telemetry_frame(load=100000, encoder=5000))

        with self.assertRaisesRegex(RuntimeError, "Retomada bloqueada"):
            backend.restore_checkpoint(checkpoint)

        self.assertEqual(MachineState.PAUSED, backend.telemetry().state)

    def test_checkpoint_rebases_offsets_when_controller_counters_reset(self):
        transport = FakeTransport(telemetry_frame(load=0, encoder=0))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )

        backend.connect()
        transport.received.extend(telemetry_frame(load=2_000_000, encoder=3200))
        saved = backend.checkpoint_state()
        saved_force = float(saved["force_kn"])
        saved_position = float(saved["position_mm"])

        transport.received.extend(telemetry_frame(load=0, encoder=0))
        backend.restore_checkpoint(saved)

        telemetry = backend.telemetry()
        self.assertAlmostEqual(saved_force, telemetry.force_kn, places=3)
        self.assertAlmostEqual(saved_position, telemetry.position_mm, places=3)

    def test_checkpoint_rebases_32_newtons_even_below_force_tolerance(self):
        transport = FakeTransport(telemetry_frame(load=0, encoder=0))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )

        backend.connect()
        transport.received.extend(telemetry_frame(load=2_114, encoder=0))
        saved = backend.checkpoint_state()
        self.assertAlmostEqual(0.032, float(saved["force_kn"]), places=3)

        transport.received.extend(telemetry_frame(load=0, encoder=0))
        backend.restore_checkpoint(saved)

        telemetry = backend.telemetry()
        self.assertAlmostEqual(float(saved["force_kn"]), telemetry.force_kn, places=3)
        self.assertAlmostEqual(float(saved["position_mm"]), telemetry.position_mm, places=3)

    def test_resume_at_saved_target_holds_zero_speed_after_counter_reset(self):
        transport = FakeTransport(telemetry_frame(load=0, encoder=0))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )

        backend.connect()
        transport.received.extend(telemetry_frame(load=2_000_000, encoder=3200))
        saved = backend.checkpoint_state()
        backend.configure_target_force(float(saved["force_kn"]))
        transport.received.extend(telemetry_frame(load=0, encoder=0))

        backend.restore_checkpoint(saved)
        backend.resume_test(
            Sample(12.0, float(saved["force_kn"]), float(saved["position_mm"]),
                   float(saved["position_mm"]), 1, 1, "tracao", True),
            25.0,
        )

        payload = decode_7bit_block(transport.writes[-2][1:6])
        self.assertAlmostEqual(0.0, struct.unpack("<f", payload)[0])

    def test_begin_traction_commands_speed_before_first_sample(self):
        transport = FakeTransport(telemetry_frame(load=0, encoder=0))
        backend = NativeM231Backend(
            "unused.dll",
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )

        backend.connect()
        backend.begin_test(30.0)
        backend.apply_sample(Sample(0.0, 0.0, 0.0, 0.0, 1, 1, "tracao", True))

        self.assertEqual(2, len(transport.writes))
        payload = decode_7bit_block(transport.writes[-2][1:6])
        self.assertAlmostEqual(30.0, struct.unpack("<f", payload)[0])

    def test_traction_keeps_program_speed_between_sparse_samples(self):
        transport = FakeTransport(telemetry_frame(load=0, encoder=0))
        backend = NativeM231Backend(
            "unused.dll",
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )

        backend.connect()
        backend.begin_test(1.3)
        backend.apply_sample(Sample(0.0, 0.0, 0.0, 0.0, 1, 1, "tracao", True))
        backend.apply_sample(Sample(60.0, 1.0, 1.0, 1.0, 1, 1, "tracao", True))

        # O segundo ponto foi adquirido 60 s depois, mas isso nao pode
        # substituir a velocidade configurada no programa.
        self.assertEqual(2, len(transport.writes))
        payload = decode_7bit_block(transport.writes[-2][1:6])
        self.assertAlmostEqual(1.3, struct.unpack("<f", payload)[0], places=5)

    def test_target_force_stops_traction_without_alarm(self):
        transport = FakeTransport(telemetry_frame(load=0, encoder=0))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(load_capacity_kn=100.0),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )

        backend.connect()
        backend.configure_target_force(1.0)
        backend.begin_test(30.0)
        transport.received.extend(telemetry_frame(load=100000, encoder=10))

        telemetry = backend.telemetry()

        self.assertEqual(MachineState.RUNNING, telemetry.state)
        self.assertIn("Carga-alvo", telemetry.alarm)
        self.assertAlmostEqual(
            0.0,
            struct.unpack("<f", decode_7bit_block(transport.writes[-2][1:6]))[0],
        )

    def test_return_stops_after_crossing_target(self):
        transport = FakeTransport(telemetry_frame(encoder=1000))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )
        backend.connect()
        backend.zero_position()
        backend.configure_limits(-20, 20)
        backend.return_position(2.0, 60.0)
        transport.received.extend(telemetry_frame(encoder=1200))

        telemetry = backend.telemetry()

        self.assertEqual(2.0, telemetry.position_mm)
        self.assertEqual(MachineState.IDLE, telemetry.state)
        stop_payload = decode_7bit_block(transport.writes[-2][1:6])
        self.assertEqual(0.0, struct.unpack("<f", stop_payload)[0])

    def test_zero_position_stops_active_motion_and_keeps_display_zero(self):
        transport = FakeTransport(telemetry_frame(encoder=1000))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )
        backend.connect()
        backend.jog(1, 60.0)
        backend.zero_position()
        transport.received.extend(telemetry_frame(encoder=1000))

        telemetry = backend.telemetry()

        self.assertEqual(0.0, telemetry.position_mm)
        self.assertEqual(MachineState.STOPPED, telemetry.state)
        self.assertEqual(0.0, backend._commanded_speed)

    def test_virtual_panel_zero_rebases_host_position_reference(self):
        transport = FakeTransport(telemetry_frame(encoder=-820))
        backend = NativeM231Backend(
            "virtual_d2xx64.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )
        backend.connect()
        self.assertAlmostEqual(0.0, backend.telemetry().position_mm)

        transport.received.extend(
            telemetry_frame(
                encoder=-820,
                status=(VIRTUAL_POSITION_ZERO_STATUS, 0, 0, 0),
            )
        )
        self.assertEqual(0.0, backend.telemetry().position_mm)

        transport.received.extend(telemetry_frame(encoder=-720))
        self.assertAlmostEqual(1.0, backend.telemetry().position_mm)

    def test_force_limit_stops_machine_in_backend(self):
        transport = FakeTransport(telemetry_frame(load=0, encoder=0))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(
                load_capacity_kn=100.0,
                load_adjustment_positive=1.0,
                load_adjustment_negative=1.0,
            ),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )
        backend.connect()
        backend.configure_safety(1.0)
        backend.jog(1, 10.0)
        transport.received.extend(telemetry_frame(load=100000, encoder=1))

        telemetry = backend.telemetry()

        self.assertEqual(MachineState.ALARM, telemetry.state)
        self.assertIn("carga", telemetry.alarm)
        writes_after_alarm = len(transport.writes)
        with self.assertRaises(RuntimeError):
            backend.jog(1, 10.0)
        self.assertEqual(writes_after_alarm, len(transport.writes))

    def test_course_limit_stops_motion_and_updates_telemetry_state(self):
        transport = FakeTransport(telemetry_frame(encoder=0))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )
        backend.connect()
        backend.configure_limits(-100.0, 100.0)
        backend.jog(1, 60.0)
        transport.received.extend(telemetry_frame(encoder=10000))

        telemetry = backend.telemetry()

        self.assertEqual(MachineState.STOPPED, telemetry.state)
        self.assertIn("superior", telemetry.alarm)
        self.assertIn("100 mm", telemetry.alarm)
        stop_payload = decode_7bit_block(transport.writes[-2][1:6])
        self.assertEqual(0.0, struct.unpack("<f", stop_payload)[0])
        with self.assertRaises(ValueError):
            backend.jog(1, 60.0)


if __name__ == "__main__":
    unittest.main()
