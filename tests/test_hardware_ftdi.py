import struct
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from m231pro.hardware.ftdi import (
    D2xxConnectionSettings,
    D2xxTransport,
    FtdiDevice,
    FtdiProbeResult,
    find_compatible_d2xx,
    pe_bitness,
    probe_ftdi,
    probe_project_ftdi,
)


def write_minimal_pe(path: Path, machine: int) -> None:
    data = bytearray(256)
    data[0:2] = b"MZ"
    data[0x3C:0x40] = struct.pack("<I", 128)
    data[128:132] = b"PE\0\0"
    data[132:134] = struct.pack("<H", machine)
    path.write_bytes(data)


class RecordingFunction:
    def __init__(self, name, calls, callback=None):
        self.name = name
        self.calls = calls
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append((self.name, args))
        if self.callback is not None:
            self.callback(*args)
        return 0


class RecordingD2xxLibrary:
    def __init__(self):
        self.calls = []

        def open_handle(_index, handle_pointer):
            handle_pointer._obj.value = 1

        names = (
            "FT_Close",
            "FT_ResetDevice",
            "FT_SetBaudRate",
            "FT_SetDataCharacteristics",
            "FT_SetFlowControl",
            "FT_SetTimeouts",
            "FT_SetLatencyTimer",
            "FT_SetUSBParameters",
            "FT_SetDeadmanTimeout",
            "FT_Purge",
            "FT_GetQueueStatus",
            "FT_Read",
            "FT_Write",
        )
        self.FT_Open = RecordingFunction("FT_Open", self.calls, open_handle)
        for name in names:
            setattr(self, name, RecordingFunction(name, self.calls))


class TestFtdiProbe(unittest.TestCase):
    def test_m231_uses_the_exact_legacy_connection_parameters(self):
        # Extraidos de bo+a::v no IL do AnyTest Pro 3.8.17.44.
        settings = D2xxConnectionSettings()

        self.assertEqual(853000, settings.baud_rate)
        self.assertEqual((8, 0, 1), (settings.word_length, settings.stop_bits, settings.parity))
        self.assertEqual(
            (0, 0, 0),
            (settings.flow_control, settings.xon_character, settings.xoff_character),
        )
        self.assertEqual(2_147_483_647, settings.read_timeout_ms)
        self.assertEqual(2_147_483_647, settings.write_timeout_ms)
        self.assertEqual(1, settings.latency_timer_ms)
        self.assertEqual(500, settings.deadman_timeout_ms)
        self.assertEqual(65536, settings.usb_transfer_size)
        self.assertEqual(512, settings.usb_out_transfer_size)

    def test_transport_initializes_d2xx_in_the_legacy_order(self):
        library = RecordingD2xxLibrary()
        transport = D2xxTransport("unused.dll")
        transport._load = lambda: library

        transport.open()

        self.assertEqual(
            [
                "FT_Open",
                "FT_ResetDevice",
                "FT_Purge",
                "FT_SetLatencyTimer",
                "FT_SetBaudRate",
                "FT_SetDataCharacteristics",
                "FT_SetFlowControl",
                "FT_SetTimeouts",
                "FT_SetDeadmanTimeout",
                "FT_SetUSBParameters",
            ],
            [name for name, _args in library.calls],
        )

    def test_reads_32_bit_pe_architecture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "FTD2XX.DLL"
            write_minimal_pe(path, 0x014C)

            self.assertEqual(32, pe_bitness(path))

    def test_reads_64_bit_pe_architecture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "FTD2XX.DLL"
            write_minimal_pe(path, 0x8664)

            self.assertEqual(64, pe_bitness(path))

    def test_missing_dll_is_reported_without_loading_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            result = probe_ftdi(Path(directory) / "missing.dll")

        self.assertFalse(result.available)
        self.assertEqual((), result.devices)
        self.assertIn("nao encontrado", result.message)

    def test_architecture_mismatch_is_reported_before_loading_driver(self):
        other_machine = 0x014C if struct.calcsize("P") * 8 == 64 else 0x8664
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "FTD2XX.DLL"
            write_minimal_pe(path, other_machine)

            result = probe_ftdi(path)

        self.assertFalse(result.available)
        self.assertIn("incompativel", result.message)

    def test_finds_matching_library_in_original_program_driver_folders(self):
        process_bits = struct.calcsize("P") * 8
        machine = 0x8664 if process_bits == 64 else 0x014C
        relative = (
            Path("help/amd64/ftd2xx64.dll")
            if process_bits == 64
            else Path("help/i386/ftd2xx.dll")
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / relative
            path.parent.mkdir(parents=True)
            write_minimal_pe(path, machine)

            selected = find_compatible_d2xx(directory)

        self.assertIsNotNone(selected)
        self.assertEqual(path.name, selected.name)

    def test_virtual_dll_is_selected_before_physical_probe_when_emulator_is_active(self):
        process_bits = struct.calcsize("P") * 8
        machine = 0x8664 if process_bits == 64 else 0x014C
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            physical = root / "ftd2xx64.dll" if process_bits == 64 else root / "ftd2xx.dll"
            virtual = root / "virtual_d2xx64.dll" if process_bits == 64 else root / "virtual_d2xx.dll"
            write_minimal_pe(physical, machine)
            write_minimal_pe(virtual, machine)
            device = FtdiDevice(0, "REAL-M231", "M231 FTDI", 0, 0, 0, 0)

            probed = []

            def fake_probe(path):
                probed.append(Path(path).name)
                if Path(path).name == physical.name:
                    return FtdiProbeResult(True, (device,), "maquina real", Path(path))
                return FtdiProbeResult(True, (device,), "emulador", Path(path))

            with patch.dict("os.environ", {"M231PRO_HARDWARE_MODE": "virtual"}), patch(
                "m231pro.hardware.ftdi._virtual_d2xx_is_active", return_value=True
            ), patch(
                "m231pro.hardware.ftdi.probe_ftdi", side_effect=fake_probe
            ):
                result = probe_project_ftdi(root)

        self.assertEqual(virtual.name, result.dll_path.name)
        self.assertEqual((device,), result.devices)
        self.assertEqual([virtual.name], probed)

    def test_emulator_is_selected_automatically_without_environment_variable(self):
        process_bits = struct.calcsize("P") * 8
        machine = 0x8664 if process_bits == 64 else 0x014C
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            physical = root / "ftd2xx64.dll" if process_bits == 64 else root / "ftd2xx.dll"
            virtual = root / "virtual_d2xx64.dll" if process_bits == 64 else root / "virtual_d2xx.dll"
            write_minimal_pe(physical, machine)
            write_minimal_pe(virtual, machine)
            device = FtdiDevice(0, "A501GR6Y", "M231 virtual", 5, 0, 0, 0)

            def fake_probe(path):
                if Path(path).name == virtual.name:
                    return FtdiProbeResult(True, (device,), "emulador", Path(path))
                raise AssertionError("a DLL fisica nao deve ser sondada com o emulador ativo")

            with patch.dict("os.environ", {}, clear=True), patch(
                "m231pro.hardware.ftdi._virtual_d2xx_is_active", return_value=True
            ), patch("m231pro.hardware.ftdi.probe_ftdi", side_effect=fake_probe):
                result = probe_project_ftdi(root)

        self.assertEqual(virtual.name, result.dll_path.name)
        self.assertEqual((device,), result.devices)

    def test_virtual_dll_wins_when_physical_dll_has_no_device(self):
        process_bits = struct.calcsize("P") * 8
        machine = 0x8664 if process_bits == 64 else 0x014C
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            physical = root / "ftd2xx64.dll" if process_bits == 64 else root / "ftd2xx.dll"
            virtual = root / "virtual_d2xx64.dll" if process_bits == 64 else root / "virtual_d2xx.dll"
            write_minimal_pe(physical, machine)
            write_minimal_pe(virtual, machine)
            device = FtdiDevice(0, "A501GR6Y", "FT232R USB UART", 5, 0, 0, 0)

            def fake_probe(path):
                if Path(path).name == physical.name:
                    return FtdiProbeResult(True, (), "fisica sem dispositivo", Path(path))
                return FtdiProbeResult(True, (device,), "emulador ativo", Path(path))

            with patch.dict("os.environ", {"M231PRO_HARDWARE_MODE": "virtual"}), patch(
                "m231pro.hardware.ftdi._virtual_d2xx_is_active", return_value=True
            ), patch(
                "m231pro.hardware.ftdi.probe_ftdi", side_effect=fake_probe
            ):
                result = probe_project_ftdi(root)

        self.assertEqual(virtual.name, result.dll_path.name)
        self.assertEqual((device,), result.devices)

    def test_virtual_dll_wins_when_physical_dll_only_sees_anonymous_usb_host(self):
        process_bits = struct.calcsize("P") * 8
        machine = 0x8664 if process_bits == 64 else 0x014C
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            physical = root / "ftd2xx64.dll" if process_bits == 64 else root / "ftd2xx.dll"
            virtual = root / "virtual_d2xx64.dll" if process_bits == 64 else root / "virtual_d2xx.dll"
            write_minimal_pe(physical, machine)
            write_minimal_pe(virtual, machine)
            anonymous = FtdiDevice(0, "", "", 3, 0, 0, 1)
            device = FtdiDevice(0, "A501GR6Y", "FT232R USB UART", 5, 0, 0, 0)

            def fake_probe(path):
                if Path(path).name == physical.name:
                    return FtdiProbeResult(True, (anonymous,), "USB virtual anonimo", Path(path))
                return FtdiProbeResult(True, (device,), "emulador ativo", Path(path))

            with patch.dict("os.environ", {"M231PRO_HARDWARE_MODE": "virtual"}), patch(
                "m231pro.hardware.ftdi._virtual_d2xx_is_active", return_value=True
            ), patch(
                "m231pro.hardware.ftdi.probe_ftdi", side_effect=fake_probe
            ):
                result = probe_project_ftdi(root)

        self.assertEqual(virtual.name, result.dll_path.name)
        self.assertEqual((device,), result.devices)


if __name__ == "__main__":
    unittest.main()
