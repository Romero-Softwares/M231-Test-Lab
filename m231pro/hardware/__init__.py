"""Integracoes e diagnosticos de hardware."""

from .ftdi import (
    D2xxConnectionSettings,
    D2xxTransport,
    FtdiDevice,
    FtdiProbeResult,
    find_compatible_d2xx,
    pe_bitness,
    probe_ftdi,
    probe_project_ftdi,
)
from .backend import LocalMachineBackend, MachineController, MachineState, MachineTelemetry
from .m231 import (
    M231Calibration,
    M231FrameDecoder,
    M231RawTelemetry,
    NativeM231Backend,
    decode_telemetry_frame,
    motion_frames,
)

__all__ = [
    "FtdiDevice",
    "FtdiProbeResult",
    "D2xxConnectionSettings",
    "D2xxTransport",
    "find_compatible_d2xx",
    "pe_bitness",
    "probe_ftdi",
    "probe_project_ftdi",
    "LocalMachineBackend",
    "MachineController",
    "MachineState",
    "MachineTelemetry",
    "M231Calibration",
    "M231FrameDecoder",
    "M231RawTelemetry",
    "NativeM231Backend",
    "decode_telemetry_frame",
    "motion_frames",
]
