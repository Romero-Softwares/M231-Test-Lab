from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Protocol


FT_OK = 0
FT_PURGE_RX = 1
FT_PURGE_TX = 2
PE32_MACHINE = 0x014C
PE64_MACHINES = {0x8664, 0xAA64}


@dataclass(frozen=True)
class FtdiDevice:
    index: int
    serial_number: str
    description: str
    device_type: int
    device_id: int
    location_id: int
    flags: int


@dataclass(frozen=True)
class FtdiProbeResult:
    available: bool
    devices: tuple[FtdiDevice, ...]
    message: str
    dll_path: Path | None = None


@dataclass(frozen=True)
class D2xxConnectionSettings:
    # Valores extraidos do IL do AnyTest Pro 3.8.17.44 (bo+a::v).
    # O baud e carregado como 0x000D0408 == 853000; nao substituir pelo
    # valor UART comum de 921600 nem ajustar estes parametros por tentativa.
    baud_rate: int = 853000
    word_length: int = 8
    stop_bits: int = 0
    parity: int = 1
    flow_control: int = 0
    xon_character: int = 0
    xoff_character: int = 0
    read_timeout_ms: int = 2_147_483_647
    write_timeout_ms: int = 2_147_483_647
    latency_timer_ms: int = 1
    usb_transfer_size: int = 65536
    usb_out_transfer_size: int = 512
    deadman_timeout_ms: int = 500


class D2xxLibrary(Protocol):
    FT_CreateDeviceInfoList: object
    FT_GetDeviceInfoDetail: object


def pe_bitness(path: str | Path) -> int | None:
    """Retorna 32/64 para uma DLL PE ou None quando o arquivo nao e PE valido."""
    try:
        with Path(path).open("rb") as handle:
            if handle.read(2) != b"MZ":
                return None
            handle.seek(0x3C)
            pe_offset_data = handle.read(4)
            if len(pe_offset_data) != 4:
                return None
            pe_offset = struct.unpack("<I", pe_offset_data)[0]
            handle.seek(pe_offset)
            if handle.read(4) != b"PE\0\0":
                return None
            machine_data = handle.read(2)
    except (OSError, ValueError):
        return None

    if len(machine_data) != 2:
        return None
    machine = struct.unpack("<H", machine_data)[0]
    if machine == PE32_MACHINE:
        return 32
    if machine in PE64_MACHINES:
        return 64
    return None


def find_compatible_d2xx(root: str | Path) -> Path | None:
    """Localiza a D2XX da mesma arquitetura do processo Python."""
    base = Path(root)
    process_bits = struct.calcsize("P") * 8
    architecture_candidates = (
        [base / "help" / "amd64" / "ftd2xx64.dll"]
        if process_bits == 64
        else [base / "help" / "i386" / "ftd2xx.dll"]
    )
    packaged_candidates = (
        [base / "ftd2xx64.dll"]
        if process_bits == 64
        else [base / "ftd2xx.dll"]
    )
    candidates = [base / "FTD2XX.DLL", *packaged_candidates, *architecture_candidates]
    for candidate in candidates:
        if candidate.is_file() and pe_bitness(candidate) == process_bits:
            return candidate.resolve()
    return None


def _decode_c_string(buffer: ctypes.Array[ctypes.c_char]) -> str:
    return buffer.value.decode("ascii", errors="replace").strip()


def enumerate_d2xx_devices(library: D2xxLibrary) -> tuple[FtdiDevice, ...]:
    """Lista dispositivos D2XX sem abrir handles e sem transmitir dados."""
    create_list = library.FT_CreateDeviceInfoList
    get_detail = library.FT_GetDeviceInfoDetail
    create_list.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    create_list.restype = ctypes.c_uint32
    get_detail.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_detail.restype = ctypes.c_uint32

    count = ctypes.c_uint32()
    status = int(create_list(ctypes.byref(count)))
    if status != FT_OK:
        raise RuntimeError(f"FT_CreateDeviceInfoList retornou FT_STATUS {status}.")

    devices: list[FtdiDevice] = []
    for index in range(count.value):
        flags = ctypes.c_uint32()
        device_type = ctypes.c_uint32()
        device_id = ctypes.c_uint32()
        location_id = ctypes.c_uint32()
        serial = ctypes.create_string_buffer(16)
        description = ctypes.create_string_buffer(64)
        handle = ctypes.c_void_p()
        status = int(
            get_detail(
                index,
                ctypes.byref(flags),
                ctypes.byref(device_type),
                ctypes.byref(device_id),
                ctypes.byref(location_id),
                serial,
                description,
                ctypes.byref(handle),
            )
        )
        if status != FT_OK:
            raise RuntimeError(
                f"FT_GetDeviceInfoDetail({index}) retornou FT_STATUS {status}."
            )
        devices.append(
            FtdiDevice(
                index=index,
                serial_number=_decode_c_string(serial),
                description=_decode_c_string(description),
                device_type=device_type.value,
                device_id=device_id.value,
                location_id=location_id.value,
                flags=flags.value,
            )
        )
    return tuple(devices)


def probe_ftdi(dll_path: str | Path) -> FtdiProbeResult:
    """Verifica DLL e enumera FTDI; deliberadamente nao chama FT_Open/FT_Write."""
    path = Path(dll_path).resolve()
    if not path.is_file():
        return FtdiProbeResult(
            False,
            (),
            f"Driver FTDI nao encontrado em {path}.",
            path,
        )

    dll_bits = pe_bitness(path)
    python_bits = struct.calcsize("P") * 8
    if dll_bits is None:
        return FtdiProbeResult(False, (), "A DLL FTDI nao e um arquivo PE valido.", path)
    if dll_bits != python_bits:
        return FtdiProbeResult(
            False,
            (),
            f"DLL FTDI {dll_bits} bits incompativel com Python {python_bits} bits.",
            path,
        )

    try:
        loader = getattr(ctypes, "WinDLL")
        library = loader(str(path))
        devices = enumerate_d2xx_devices(library)
    except (AttributeError, OSError, RuntimeError) as error:
        return FtdiProbeResult(False, (), f"Falha no diagnostico FTDI: {error}", path)

    if not devices:
        return FtdiProbeResult(True, (), "Driver FTDI carregado; nenhum dispositivo conectado.", path)
    if len(devices) == 1:
        device = devices[0]
        identity = device.description or "FTDI sem descricao"
        if device.serial_number:
            identity += f" (serie {device.serial_number})"
        return FtdiProbeResult(True, devices, f"1 dispositivo detectado: {identity}.", path)
    return FtdiProbeResult(
        True,
        devices,
        f"{len(devices)} dispositivos FTDI detectados; selecao ainda bloqueada.",
        path,
    )


def probe_project_ftdi(driver_root: str | Path) -> FtdiProbeResult:
    """Localiza a D2XX empacotada com o sistema e executa a sondagem."""
    root = Path(driver_root).resolve()
    dll_path = find_compatible_d2xx(root)
    if dll_path is None:
        process_bits = struct.calcsize("P") * 8
        return FtdiProbeResult(
            False,
            (),
            f"Nenhuma DLL FTDI de {process_bits} bits encontrada em {root}.",
        )
    return probe_ftdi(dll_path)


class D2xxTransport:
    """Canal D2XX completo; o codec de protocolo fica separado deste transporte."""

    def __init__(
        self,
        dll_path: str | Path,
        settings: D2xxConnectionSettings | None = None,
    ):
        self.dll_path = Path(dll_path).resolve()
        self.settings = settings or D2xxConnectionSettings()
        self._library = None
        self._handle = ctypes.c_void_p()

    @property
    def is_open(self) -> bool:
        return bool(self._handle.value)

    @staticmethod
    def _check(status: int, operation: str) -> None:
        if int(status) != FT_OK:
            raise RuntimeError(f"{operation} retornou FT_STATUS {int(status)}.")

    def _load(self):
        if not self.dll_path.is_file():
            raise FileNotFoundError(f"DLL FTDI nao encontrada: {self.dll_path}")
        if pe_bitness(self.dll_path) != struct.calcsize("P") * 8:
            raise RuntimeError("A arquitetura da DLL FTDI nao corresponde ao Python.")
        try:
            library = getattr(ctypes, "WinDLL")(str(self.dll_path))
        except (AttributeError, OSError) as error:
            raise RuntimeError(f"Nao foi possivel carregar a D2XX: {error}") from error
        library.FT_Open.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        library.FT_Open.restype = ctypes.c_uint32
        library.FT_Close.argtypes = [ctypes.c_void_p]
        library.FT_Close.restype = ctypes.c_uint32
        library.FT_ResetDevice.argtypes = [ctypes.c_void_p]
        library.FT_ResetDevice.restype = ctypes.c_uint32
        library.FT_SetBaudRate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        library.FT_SetBaudRate.restype = ctypes.c_uint32
        library.FT_SetDataCharacteristics.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
        ]
        library.FT_SetDataCharacteristics.restype = ctypes.c_uint32
        library.FT_SetFlowControl.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ushort,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
        ]
        library.FT_SetFlowControl.restype = ctypes.c_uint32
        library.FT_SetTimeouts.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        library.FT_SetTimeouts.restype = ctypes.c_uint32
        library.FT_SetLatencyTimer.argtypes = [ctypes.c_void_p, ctypes.c_ubyte]
        library.FT_SetLatencyTimer.restype = ctypes.c_uint32
        library.FT_SetUSBParameters.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        library.FT_SetUSBParameters.restype = ctypes.c_uint32
        if hasattr(library, "FT_SetDeadmanTimeout"):
            library.FT_SetDeadmanTimeout.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            library.FT_SetDeadmanTimeout.restype = ctypes.c_uint32
        library.FT_Purge.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        library.FT_Purge.restype = ctypes.c_uint32
        library.FT_GetQueueStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        library.FT_GetQueueStatus.restype = ctypes.c_uint32
        library.FT_Read.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        library.FT_Read.restype = ctypes.c_uint32
        library.FT_Write.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        library.FT_Write.restype = ctypes.c_uint32
        return library

    def open(self, index: int = 0) -> None:
        if self.is_open:
            return
        self._library = self._load()
        self._check(self._library.FT_Open(index, ctypes.byref(self._handle)), "FT_Open")
        try:
            # Preserve a mesma ordem de inicializacao de bo+a::v no legado.
            self._check(self._library.FT_ResetDevice(self._handle), "FT_ResetDevice")
            self.purge()
            self._check(
                self._library.FT_SetLatencyTimer(
                    self._handle, self.settings.latency_timer_ms
                ),
                "FT_SetLatencyTimer",
            )
            self._check(
                self._library.FT_SetBaudRate(self._handle, self.settings.baud_rate),
                "FT_SetBaudRate",
            )
            self._check(
                self._library.FT_SetDataCharacteristics(
                    self._handle,
                    self.settings.word_length,
                    self.settings.stop_bits,
                    self.settings.parity,
                ),
                "FT_SetDataCharacteristics",
            )
            self._check(
                self._library.FT_SetFlowControl(
                    self._handle,
                    self.settings.flow_control,
                    self.settings.xon_character,
                    self.settings.xoff_character,
                ),
                "FT_SetFlowControl",
            )
            self._check(
                self._library.FT_SetTimeouts(
                    self._handle,
                    self.settings.read_timeout_ms,
                    self.settings.write_timeout_ms,
                ),
                "FT_SetTimeouts",
            )
            if hasattr(self._library, "FT_SetDeadmanTimeout"):
                self._check(
                    self._library.FT_SetDeadmanTimeout(
                        self._handle, self.settings.deadman_timeout_ms
                    ),
                    "FT_SetDeadmanTimeout",
                )
            self._check(
                self._library.FT_SetUSBParameters(
                    self._handle,
                    self.settings.usb_transfer_size,
                    self.settings.usb_out_transfer_size,
                ),
                "FT_SetUSBParameters",
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._library is not None and self.is_open:
            self._library.FT_Close(self._handle)
        self._handle = ctypes.c_void_p()
        self._library = None

    def purge(self) -> None:
        if not self.is_open:
            raise RuntimeError("O canal FTDI nao esta aberto.")
        self._check(
            self._library.FT_Purge(self._handle, FT_PURGE_RX | FT_PURGE_TX),
            "FT_Purge",
        )

    def write(self, payload: bytes) -> int:
        if not self.is_open:
            raise RuntimeError("O canal FTDI nao esta aberto.")
        if not payload:
            return 0
        buffer = ctypes.create_string_buffer(payload, len(payload))
        written = ctypes.c_uint32()
        self._check(
            self._library.FT_Write(
                self._handle, buffer, len(payload), ctypes.byref(written)
            ),
            "FT_Write",
        )
        if written.value != len(payload):
            raise RuntimeError(
                f"FT_Write transmitiu {written.value} de {len(payload)} bytes."
            )
        return int(written.value)

    def read_available(self, maximum: int = 65536) -> bytes:
        if not self.is_open:
            raise RuntimeError("O canal FTDI nao esta aberto.")
        queued = ctypes.c_uint32()
        self._check(
            self._library.FT_GetQueueStatus(self._handle, ctypes.byref(queued)),
            "FT_GetQueueStatus",
        )
        size = min(int(queued.value), maximum)
        if size <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        received = ctypes.c_uint32()
        self._check(
            self._library.FT_Read(
                self._handle, buffer, size, ctypes.byref(received)
            ),
            "FT_Read",
        )
        return bytes(buffer.raw[: received.value])

    def __enter__(self) -> "D2xxTransport":
        self.open()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()
