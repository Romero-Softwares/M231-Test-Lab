from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
import struct


DEVICE_PATH = r"\\.\M231VirtualUsb"
FILE_DEVICE_UNKNOWN = 0x22
METHOD_BUFFERED = 0
FILE_READ_ACCESS = 0x0001
FILE_WRITE_ACCESS = 0x0002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def _ctl_code(function: int) -> int:
    return (
        (FILE_DEVICE_UNKNOWN << 16)
        | ((FILE_READ_ACCESS | FILE_WRITE_ACCESS) << 14)
        | (function << 2)
        | METHOD_BUFFERED
    )


IOCTL_M231_PLUG_IN = _ctl_code(0x800)
IOCTL_M231_PLUG_OUT = _ctl_code(0x801)
IOCTL_M231_PUSH_RX = _ctl_code(0x802)
IOCTL_M231_PULL_TX = _ctl_code(0x803)
IOCTL_M231_GET_STATUS = _ctl_code(0x804)


@dataclass(frozen=True)
class DriverStatus:
    version: int
    plugged_in: bool
    rx_bytes_queued: int
    tx_bytes_queued: int
    host_read_count: int = 0
    host_write_count: int = 0


def decode_driver_status(payload: bytes) -> DriverStatus:
    """Decodifica os contratos v1, v2 e v3 sem quebrar drivers instalados."""
    if len(payload) < 16:
        raise ValueError("Status incompleto retornado pelo driver M231.")
    version, plugged_in, rx_queued, tx_queued = struct.unpack_from("<IIII", payload)
    host_read_count = struct.unpack_from("<I", payload, 16)[0] if len(payload) >= 20 else 0
    host_write_count = struct.unpack_from("<I", payload, 20)[0] if len(payload) >= 24 else 0
    return DriverStatus(
        version=version,
        plugged_in=bool(plugged_in),
        rx_bytes_queued=rx_queued,
        tx_bytes_queued=tx_queued,
        host_read_count=host_read_count,
        host_write_count=host_write_count,
    )


class VirtualUsbDriver:
    def __init__(self, path: str = DEVICE_PATH) -> None:
        self.path = path
        self._handle: int | None = None

    def open(self) -> None:
        if os.name != "nt":
            raise OSError("O driver UdeCx so pode ser aberto no Windows.")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            self.path,
            0xC0000000,
            0x00000003,
            None,
            3,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = int(handle)

    def close(self) -> None:
        if self._handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "VirtualUsbDriver":
        self.open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _ioctl(self, code: int, payload: bytes = b"", output_size: int = 0) -> bytes:
        if self._handle is None:
            raise RuntimeError("O canal do driver nao esta aberto.")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        input_buffer = ctypes.create_string_buffer(payload) if payload else None
        output_buffer = ctypes.create_string_buffer(output_size) if output_size else None
        returned = wintypes.DWORD()
        ok = kernel32.DeviceIoControl(
            self._handle,
            code,
            input_buffer,
            len(payload),
            output_buffer,
            output_size,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        if output_buffer is None:
            return b""
        return output_buffer.raw[: returned.value]

    def plug_in(self) -> None:
        self._ioctl(IOCTL_M231_PLUG_IN)

    def plug_out(self) -> None:
        self._ioctl(IOCTL_M231_PLUG_OUT)

    def push_machine_bytes(self, payload: bytes) -> None:
        self._ioctl(IOCTL_M231_PUSH_RX, payload)

    def pull_host_bytes(self, maximum: int = 4096) -> bytes:
        return self._ioctl(IOCTL_M231_PULL_TX, output_size=maximum)

    def status(self) -> bytes:
        return self._ioctl(IOCTL_M231_GET_STATUS, output_size=32)

    def status_info(self) -> DriverStatus:
        return decode_driver_status(self.status())
