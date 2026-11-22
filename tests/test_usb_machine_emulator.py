import struct
import unittest
from unittest.mock import patch

from m231pro.hardware.m231 import decode_telemetry_frame, motion_frames
from usb_machine_emulator.driver_transport import (
    DriverStatus,
    IOCTL_M231_GET_STATUS,
    IOCTL_M231_PLUG_IN,
    IOCTL_M231_PLUG_OUT,
    IOCTL_M231_PULL_TX,
    IOCTL_M231_PUSH_RX,
    decode_driver_status,
)
from usb_machine_emulator.control import MachinePanelController
from usb_machine_emulator.protocol import CommandDecoder, MachineModel, telemetry_frame
from usb_machine_emulator.shared_transport import PhysicalAndSharedTransport


class UsbMachineProtocolTests(unittest.TestCase):
    def test_physical_usb_enumeration_is_enabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            transport = PhysicalAndSharedTransport()

        self.assertTrue(transport._physical_enabled)

    def test_driver_status_accepts_v1_and_v2_contracts(self):
        v1 = decode_driver_status(struct.pack("<IIII", 1, 1, 37, 0))
        v2 = decode_driver_status(struct.pack("<IIIII", 2, 1, 0, 6, 12))
        v3 = decode_driver_status(struct.pack("<IIIIII", 3, 1, 0, 6, 12, 7))

        self.assertTrue(v1.plugged_in)
        self.assertEqual(v1.host_read_count, 0)
        self.assertEqual(v2.host_read_count, 12)
        self.assertEqual(v2.host_write_count, 0)
        self.assertEqual(v3.host_write_count, 7)

    def test_virtual_telemetry_is_consumed_by_physical_backend_codec(self):
        frame = telemetry_frame(load_counts=123456, encoder_counts=-4321)
        decoded = decode_telemetry_frame(frame)
        self.assertEqual(decoded.load_counts, 123456)
        self.assertEqual(decoded.encoder_counts, -4321)
        self.assertEqual(len(frame), 37)

    def test_decoder_accepts_fragmented_physical_speed_commands(self):
        first, second = motion_frames(12.5)
        decoder = CommandDecoder()
        self.assertEqual(decoder.feed(first[:3]), [])
        commands = decoder.feed(first[3:] + second)
        self.assertEqual([name for name, _speed in commands], ["0", "X"])
        self.assertAlmostEqual(commands[0][1], 12.5)
        self.assertAlmostEqual(commands[1][1], 12.5)

    def test_machine_model_moves_and_generates_load_after_contact(self):
        model = MachineModel(contact_position_mm=0.5, stiffness_kn_mm=2.0)
        model.apply_command("X", 60.0)
        model.step(1.0)
        self.assertAlmostEqual(model.position_mm, 1.0)
        self.assertAlmostEqual(model.force_kn, 1.0)
        decoded = decode_telemetry_frame(model.frame())
        self.assertEqual(decoded.encoder_counts, 100)
        self.assertEqual(decoded.load_counts, 65000)

    def test_driver_ioctl_contract_is_stable_and_unique(self):
        values = {
            IOCTL_M231_PLUG_IN,
            IOCTL_M231_PLUG_OUT,
            IOCTL_M231_PUSH_RX,
            IOCTL_M231_PULL_TX,
            IOCTL_M231_GET_STATUS,
        }
        self.assertEqual(len(values), 5)
        self.assertEqual(IOCTL_M231_PLUG_IN, 0x22E000)
        self.assertEqual(struct.calcsize("P"), 8)


class FakePanelDriver:
    def __init__(self) -> None:
        self.opened = False
        self.plugged_in = False
        self.host_bytes = bytearray()
        self.frames: list[bytes] = []
        self.host_read_count = 0
        self.host_write_count = 0

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def plug_in(self) -> None:
        self.plugged_in = True

    def plug_out(self) -> None:
        self.plugged_in = False

    def pull_host_bytes(self, maximum: int = 4096) -> bytes:
        result = bytes(self.host_bytes[:maximum])
        del self.host_bytes[:maximum]
        return result

    def push_machine_bytes(self, payload: bytes) -> None:
        self.frames.append(payload)

    def status_info(self) -> DriverStatus:
        return DriverStatus(
            3,
            self.plugged_in,
            sum(len(frame) for frame in self.frames),
            len(self.host_bytes),
            self.host_read_count,
            self.host_write_count,
        )

    def host_reads_telemetry(self) -> bytes:
        frame = self.frames.pop(0)
        self.host_read_count += 1
        return frame

    def host_writes(self, payload: bytes) -> None:
        self.host_bytes.extend(payload)
        self.host_write_count += 1


class MachinePanelControllerTests(unittest.TestCase):
    def test_panel_telemetry_does_not_sync_itself(self):
        class SelfReportingPanelDriver(FakePanelDriver):
            def push_machine_bytes(self, payload: bytes) -> None:
                super().push_machine_bytes(payload)
                self.host_write_count += 1

        driver = SelfReportingPanelDriver()
        controller = MachinePanelController(driver)
        controller.start(now=8.0)

        waiting = controller.tick(now=8.1)

        self.assertFalse(waiting.synchronized)

    def test_stale_host_counters_do_not_sync_a_new_panel_session(self):
        driver = FakePanelDriver()
        driver.host_read_count = 12
        driver.host_write_count = 7
        controller = MachinePanelController(driver, synchronization_timeout_s=1.0)

        controller.start(now=9.0)
        waiting = controller.tick(now=9.1)

        self.assertFalse(waiting.synchronized)

    def test_values_and_manual_control_stay_locked_until_host_reads_telemetry(self):
        driver = FakePanelDriver()
        controller = MachinePanelController(driver, synchronization_timeout_s=1.0)
        controller.start(now=10.0)

        waiting = controller.tick(now=10.1)
        self.assertFalse(waiting.synchronized)
        with self.assertRaisesRegex(RuntimeError, "Controle bloqueado"):
            controller.manual_jog(1, 60.0, now=10.1)

        driver.host_reads_telemetry()
        synchronized = controller.tick(now=10.2)
        self.assertTrue(synchronized.synchronized)
        controller.manual_jog(1, 60.0, now=10.2)
        moving = controller.tick(now=10.7)
        self.assertAlmostEqual(moving.position_mm, 0.5)
        self.assertEqual(moving.command_source, "PAINEL MANUAL")

    def test_lost_synchronization_stops_manual_motion_and_locks_panel(self):
        driver = FakePanelDriver()
        controller = MachinePanelController(driver, synchronization_timeout_s=0.5)
        controller.start(now=20.0)
        controller.tick(now=20.1)
        driver.host_reads_telemetry()
        controller.tick(now=20.2)
        controller.manual_jog(-1, 120.0, now=20.2)

        stale = controller.tick(now=20.8)

        self.assertFalse(stale.synchronized)
        self.assertEqual(stale.speed_mm_min, 0.0)
        self.assertEqual(stale.command_source, "BLOQUEIO DE SEGURANCA")

    def test_software_command_alone_cannot_synchronize_the_emulator(self):
        driver = FakePanelDriver()
        controller = MachinePanelController(driver)
        controller.start(now=30.0)
        driver.host_writes(b"".join(motion_frames(25.0)))

        snapshot = controller.tick(now=30.1)

        self.assertFalse(snapshot.synchronized)
        self.assertEqual(snapshot.speed_mm_min, 0.0)
        self.assertEqual(snapshot.command_source, "BLOQUEIO DE SEGURANCA")

    def test_waiting_for_either_software_does_not_build_a_stale_frame_backlog(self):
        driver = FakePanelDriver()
        controller = MachinePanelController(driver)
        controller.start(now=40.0)

        for index in range(100):
            controller.tick(now=40.0 + index * 0.02)

        self.assertEqual(len(driver.frames), 1)
        self.assertEqual(driver.status_info().rx_bytes_queued, 37)

    def test_same_emulator_accepts_successive_m231pro_sessions(self):
        driver = FakePanelDriver()
        controller = MachinePanelController(driver, synchronization_timeout_s=0.5)
        controller.start(now=50.0)
        controller.tick(now=50.1)

        # Primeira sessao: um cliente consome telemetria.
        driver.host_reads_telemetry()
        self.assertTrue(controller.tick(now=50.2).synchronized)
        self.assertFalse(controller.tick(now=50.8).synchronized)

        # Segunda sessao: o M231Pro abre depois e envia os dois canais 0/X.
        driver.host_reads_telemetry()
        driver.host_writes(b"".join(motion_frames(15.0)))
        snapshot = controller.tick(now=50.9)

        self.assertTrue(snapshot.synchronized)
        self.assertEqual(snapshot.speed_mm_min, 15.0)
        self.assertEqual(snapshot.compatibility_text, "M231Pro")

    def test_dual_transport_refills_only_the_interface_consumed_by_its_client(self):
        shared = FakePanelDriver()
        physical = FakePanelDriver()
        shared.open()
        physical.open()
        shared.plug_in()
        physical.plug_in()
        shared.frames.append(b"shared-old")
        physical.frames.append(b"physical-old")
        transport = PhysicalAndSharedTransport.__new__(PhysicalAndSharedTransport)
        transport._shared = shared
        transport._physical = physical
        transport._physical_available = True

        shared.frames.clear()
        status = transport.status_info()
        self.assertEqual(status.rx_bytes_queued, 0)
        transport.push_machine_bytes(b"latest")

        self.assertEqual(shared.frames, [b"latest"])
        self.assertEqual(physical.frames, [b"physical-old"])


if __name__ == "__main__":
    unittest.main()
