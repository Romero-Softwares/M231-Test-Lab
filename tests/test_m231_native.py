import struct
import unittest

from m231pro.hardware.backend import MachineState
from m231pro.hardware.m231 import (
    M231Calibration,
    M231FrameDecoder,
    NativeM231Backend,
    decode_7bit_block,
    encode_command,
    motion_frames,
)


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

    def open(self, index=0):
        self.is_open = True
        self.index = index

    def close(self):
        self.is_open = False

    def write(self, payload):
        self.writes.append(payload)
        return len(payload)

    def read_available(self, maximum=65536):
        data = bytes(self.received[:maximum])
        del self.received[:maximum]
        return data


class TestM231Protocol(unittest.TestCase):
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

    def test_return_stops_after_crossing_target(self):
        transport = FakeTransport(telemetry_frame(encoder=1000))
        backend = NativeM231Backend(
            "unused.dll",
            calibration=M231Calibration(encoder_mm_per_count=0.01),
            transport_factory=lambda _path, _settings: transport,
            connect_timeout_s=0.1,
        )
        backend.connect()
        backend.configure_limits(-20, 20)
        backend.return_position(2.0, 60.0)
        transport.received.extend(telemetry_frame(encoder=1200))

        telemetry = backend.telemetry()

        self.assertEqual(2.0, telemetry.position_mm)
        self.assertEqual(MachineState.IDLE, telemetry.state)
        stop_payload = decode_7bit_block(transport.writes[-2][1:6])
        self.assertEqual(0.0, struct.unpack("<f", stop_payload)[0])

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
