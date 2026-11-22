from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import struct

from usb_machine_emulator.driver_transport import DriverStatus, VirtualUsbDriver


MAPPING_NAME = "Local\\M231VirtualD2xx"
MUTEX_NAME = "Local\\M231VirtualD2xxMutex"
MAGIC = 0x3233314D
VERSION = 1
HEADER_SIZE = 64
BUFFER_CAPACITY = 64 * 1024
RX_OFFSET = HEADER_SIZE
TX_OFFSET = RX_OFFSET + BUFFER_CAPACITY
TOTAL_SIZE = HEADER_SIZE + 2 * BUFFER_CAPACITY

_HEADER = struct.Struct("<IIIIIIIIIQ")
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0
_PAGE_READWRITE = 0x04
_FILE_MAP_ALL_ACCESS = 0x000F001F
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _kernel32() -> ctypes.WinDLL:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.MapViewOfFile.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    kernel32.MapViewOfFile.restype = wintypes.LPVOID
    kernel32.UnmapViewOfFile.argtypes = [wintypes.LPCVOID]
    kernel32.UnmapViewOfFile.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    return kernel32


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _WindowsMapping:
    def __init__(self, handle: int, address: int) -> None:
        self._handle = handle
        self._address = address
        self._buffer = (ctypes.c_char * TOTAL_SIZE).from_address(address)

    def read_slice(self, start: int, end: int) -> bytes:
        return bytes(self._buffer[start:end])

    def write_slice(self, start: int, data: bytes) -> None:
        if not data:
            return
        ctypes.memmove(self._address + start, data, len(data))

    def move_slice(self, dst_start: int, src_start: int, length: int) -> None:
        if length <= 0 or dst_start == src_start:
            return
        ctypes.memmove(self._address + dst_start, self._address + src_start, length)

    def zero_all(self) -> None:
        ctypes.memset(self._address, 0, TOTAL_SIZE)

    def close(self) -> None:
        kernel32 = _kernel32()
        if self._address:
            kernel32.UnmapViewOfFile(ctypes.c_void_p(self._address))
            self._address = 0
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = 0


def _public_security_attributes() -> tuple[_SecurityAttributes, int]:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = wintypes.LPVOID()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    if not convert("D:(A;;GA;;;WD)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    return attributes, int(descriptor.value)


class SharedD2xxTransport:
    """Canal D2XX virtual em modo usuario via memoria compartilhada."""

    def __init__(self) -> None:
        self._mapping: _WindowsMapping | None = None
        self._mutex: int | None = None

    def open(self) -> None:
        if os.name != "nt":
            raise OSError("O transporte D2XX virtual requer Windows.")
        if self._mapping is not None:
            return
        kernel32 = _kernel32()
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateFileMappingW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateFileMappingW.restype = wintypes.HANDLE
        attributes, descriptor = _public_security_attributes()
        try:
            mutex = kernel32.CreateMutexW(ctypes.byref(attributes), False, MUTEX_NAME)
            if not mutex:
                raise ctypes.WinError(ctypes.get_last_error())
            mapping_handle = kernel32.CreateFileMappingW(
                _INVALID_HANDLE_VALUE,
                ctypes.byref(attributes),
                _PAGE_READWRITE,
                0,
                TOTAL_SIZE,
                MAPPING_NAME,
            )
            if not mapping_handle:
                kernel32.CloseHandle(mutex)
                raise ctypes.WinError(ctypes.get_last_error())
            address = kernel32.MapViewOfFile(
                mapping_handle, _FILE_MAP_ALL_ACCESS, 0, 0, TOTAL_SIZE
            )
            if not address:
                kernel32.CloseHandle(mapping_handle)
                kernel32.CloseHandle(mutex)
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.LocalFree(descriptor)

        mapping = _WindowsMapping(int(mapping_handle), int(address))
        self._mutex = int(mutex)
        self._mapping = mapping
        with self._locked():
            mapping.zero_all()
            self._write_header(
                MAGIC,
                VERSION,
                os.getpid(),
                0,
                1,  # plugged_in ativo ao abrir o canal
                0,
                0,
                0,
                0,
                0,
            )

    def close(self) -> None:
        mapping = self._mapping
        mutex = self._mutex
        if mapping is not None:
            try:
                with self._locked():
                    values = list(self._read_header())
                    values[0] = 0
                    values[4] = 0
                    self._write_header(*values)
            finally:
                mapping.close()
                self._mapping = None
        if mutex is not None:
            _kernel32().CloseHandle(mutex)
            self._mutex = None

    def __enter__(self) -> "SharedD2xxTransport":
        self.open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    class _MutexScope:
        def __init__(self, owner: "SharedD2xxTransport") -> None:
            self.owner = owner

        def __enter__(self) -> None:
            if self.owner._mutex is None:
                raise RuntimeError("O mutex D2XX virtual nao esta aberto.")
            kernel32 = _kernel32()
            result = kernel32.WaitForSingleObject(self.owner._mutex, _INFINITE)
            if result != _WAIT_OBJECT_0:
                raise ctypes.WinError(ctypes.get_last_error())

        def __exit__(self, *_args: object) -> None:
            if self.owner._mutex is not None:
                _kernel32().ReleaseMutex(self.owner._mutex)

    def _locked(self) -> "SharedD2xxTransport._MutexScope":
        return self._MutexScope(self)

    def _require_mapping(self) -> _WindowsMapping:
        if self._mapping is None:
            raise RuntimeError("O canal D2XX virtual nao esta aberto.")
        return self._mapping

    def _read_header(self) -> tuple[int, ...]:
        mapping = self._require_mapping()
        return _HEADER.unpack_from(mapping._buffer, 0)

    def _write_header(self, *values: int) -> None:
        mapping = self._require_mapping()
        _HEADER.pack_into(mapping._buffer, 0, *values)

    def plug_in(self) -> None:
        with self._locked():
            values = list(self._read_header())
            values[4] = 1
            self._write_header(*values)

    def plug_out(self) -> None:
        with self._locked():
            values = list(self._read_header())
            values[4] = 0
            values[5] = 0
            values[6] = 0
            self._write_header(*values)

    def pull_host_bytes(self, maximum: int = 4096) -> bytes:
        if maximum <= 0:
            return b""
        with self._locked():
            mapping = self._require_mapping()
            values = list(self._read_header())
            queued = min(values[6], BUFFER_CAPACITY)
            count = min(maximum, queued)
            if count <= 0:
                return b""
            payload = mapping.read_slice(TX_OFFSET, TX_OFFSET + count)
            remaining = queued - count
            if remaining > 0:
                mapping.move_slice(TX_OFFSET, TX_OFFSET + count, remaining)
            values[6] = remaining
            values[7] = (values[7] + 1) & 0xFFFFFFFF  # host_read_count
            self._write_header(*values)
            return payload

    def push_machine_bytes(self, payload: bytes) -> None:
        if not payload:
            return
        with self._locked():
            mapping = self._require_mapping()
            values = list(self._read_header())
            current = min(values[5], BUFFER_CAPACITY)
            if len(payload) > BUFFER_CAPACITY:
                payload = payload[-BUFFER_CAPACITY:]
            overflow = max(0, current + len(payload) - BUFFER_CAPACITY)
            if overflow > 0:
                remaining = current - overflow
                if remaining > 0:
                    mapping.move_slice(RX_OFFSET, RX_OFFSET + overflow, remaining)
                current = remaining
            mapping.write_slice(RX_OFFSET + current, payload)
            values[5] = current + len(payload)
            self._write_header(*values)

    def status_info(self) -> DriverStatus:
        with self._locked():
            values = self._read_header()
            return DriverStatus(
                version=4,
                plugged_in=bool(values[4]),
                rx_bytes_queued=values[5],
                tx_bytes_queued=values[6],
                host_read_count=values[7],
                host_write_count=values[8],
            )


class PhysicalAndSharedTransport:
    """Mantem o canal D2XX local e conecta o FT232R no UdeCx quando disponivel."""

    def __init__(self, enable_physical: bool | None = None) -> None:
        self._shared = SharedD2xxTransport()
        self._physical = VirtualUsbDriver()
        self._physical_available = False
        # O caminho UdeCx/KMDF precisa ser tentado por padrao: e ele que faz o
        # Windows enumerar o conversor FTDI no Gerenciador de Dispositivos.
        # O canal compartilhado continua sendo o fallback quando o driver
        # ainda nao foi instalado ou nao pode ser aberto.
        if enable_physical is None:
            enable_physical = os.environ.get("M231_ENABLE_PHYSICAL_USB", "1") == "1"
        self._physical_enabled = bool(enable_physical)

    def open(self) -> None:
        self._shared.open()
        self._shared.plug_in()
        if not self._physical_enabled:
            return
        try:
            self._physical.open()
            self._physical_available = True
            # O processo pode ter terminado sem executar close(). Nesse caso
            # o UdeCx continua plugado e conserva telemetria/comandos antigos.
            # Forcar o ciclo plug-out/plug-in cria uma sessao USB limpa antes
            # de o novo modelo publicar o primeiro quadro.
            self._physical.plug_out()
            self._physical.plug_in()
        except Exception:
            try:
                if self._physical_available:
                    self._physical.plug_out()
            finally:
                try:
                    self._physical.close()
                finally:
                    self._physical_available = False

    def close(self) -> None:
        try:
            self._shared.close()
        finally:
            try:
                if self._physical_available:
                    self._physical.plug_out()
            finally:
                try:
                    self._physical.close()
                finally:
                    self._physical_available = False

    def plug_in(self) -> None:
        self._shared.plug_in()
        if self._physical_available:
            self._physical.plug_in()

    def plug_out(self) -> None:
        try:
            self._shared.plug_out()
        finally:
            if self._physical_available:
                self._physical.plug_out()

    def pull_host_bytes(self, maximum: int = 4096) -> bytes:
        shared = self._shared.pull_host_bytes(maximum)
        physical = (
            self._physical.pull_host_bytes(maximum) if self._physical_available else b""
        )
        return shared + physical

    def push_machine_bytes(self, payload: bytes) -> None:
        frame_len = len(payload)
        if frame_len == 0:
            return
        shared_queued = self._shared.status_info().rx_bytes_queued
        if shared_queued == 0 or shared_queued < frame_len:
            self._shared.push_machine_bytes(payload)
        if self._physical_available:
            phys_queued = self._physical.status_info().rx_bytes_queued
            if phys_queued == 0 or phys_queued < frame_len:
                self._physical.push_machine_bytes(payload)

    def status_info(self) -> DriverStatus:
        shared = self._shared.status_info()
        physical = (
            self._physical.status_info()
            if self._physical_available
            else DriverStatus(
                version=0,
                plugged_in=False,
                rx_bytes_queued=0,
                tx_bytes_queued=0,
            )
        )
        return DriverStatus(
            version=max(shared.version, physical.version),
            plugged_in=bool(shared.plugged_in or physical.plugged_in),
            rx_bytes_queued=(
                min(shared.rx_bytes_queued, physical.rx_bytes_queued)
                if self._physical_available
                else shared.rx_bytes_queued
            ),
            tx_bytes_queued=shared.tx_bytes_queued + physical.tx_bytes_queued,
            host_read_count=shared.host_read_count + physical.host_read_count,
            host_write_count=shared.host_write_count + physical.host_write_count,
        )
