from __future__ import annotations

from dataclasses import dataclass
import math
import struct


FRAME_END = 0x0D
TELEMETRY_COMMAND = 0x7F
ENCODER_MM_PER_COUNT = 0.01


def encode_7bit_block(raw: bytes) -> bytes:
    if len(raw) != 4:
        raise ValueError("O bloco M231 deve conter quatro bytes.")
    high_bits = 0x80
    encoded = bytearray(5)
    for index, value in enumerate(raw):
        encoded[index] = (value & 0x7F) | 0x80
        high_bits |= ((value >> 7) & 1) << index
    encoded[4] = high_bits
    return bytes(encoded)


def decode_7bit_block(encoded: bytes) -> bytes:
    if len(encoded) != 5:
        raise ValueError("O bloco M231 codificado deve conter cinco bytes.")
    high_bits = encoded[4]
    return bytes(
        (encoded[index] & 0x7F) | (((high_bits >> index) & 1) << 7)
        for index in range(4)
    )


def encode_frame(command: int, payload: bytes) -> bytes:
    if not 0 <= command <= 0x7F:
        raise ValueError("Comando M231 invalido.")
    if len(payload) % 4:
        raise ValueError("O payload deve ser multiplo de quatro bytes.")
    body = b"".join(
        encode_7bit_block(payload[offset : offset + 4])
        for offset in range(0, len(payload), 4)
    )
    return bytes((command,)) + body + bytes((FRAME_END,))


def telemetry_frame(
    *,
    load_counts: int,
    encoder_counts: int,
    position_analog_counts: int = 0,
    extensometer_counts: int = 0,
    auxiliary_counts: int = 0,
    status_1: int = 0,
    status_2: int = 0,
    control_state: int = 0,
    limit_state: int = 0,
) -> bytes:
    """Cria o quadro 0x7f/37 bytes que o backend fisico ja decodifica."""
    payload = struct.pack(
        "<iiiiiBBBBI",
        load_counts,
        position_analog_counts,
        extensometer_counts,
        auxiliary_counts,
        encoder_counts,
        status_1,
        status_2,
        control_state,
        limit_state,
        0,
    )
    return encode_frame(TELEMETRY_COMMAND, payload)


class CommandDecoder:
    """Separa comandos de velocidade e unidade enviados pela D2XX."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, float]]:
        self._buffer.extend(data)
        commands: list[tuple[str, float]] = []
        while FRAME_END in self._buffer:
            end = self._buffer.index(FRAME_END)
            frame = bytes(self._buffer[:end])
            del self._buffer[: end + 1]
            if len(frame) != 6 or frame[0] not in (ord("0"), ord("X"), ord("U")):
                continue
            raw = decode_7bit_block(frame[1:])
            if frame[0] == ord("U"):
                unit_index = struct.unpack("<I", raw)[0]
                if unit_index < 4:
                    commands.append(("U", float(unit_index)))
                continue
            speed = struct.unpack("<f", raw)[0]
            if math.isfinite(speed):
                commands.append((chr(frame[0]), speed))
        if len(self._buffer) > 4096:
            self._buffer.clear()
        return commands


@dataclass
class MachineModel:
    position_mm: float = 0.0
    force_kn: float = 0.0
    speed_mm_min: float = 0.0
    stiffness_kn_mm: float = 0.05
    contact_position_mm: float = 1.0
    load_counts_per_kn: float = 65_000.0

    def set_speed(self, speed_mm_min: float) -> None:
        if not math.isfinite(speed_mm_min):
            raise ValueError("A velocidade deve ser um numero finito.")
        self.speed_mm_min = speed_mm_min

    def apply_command(self, channel: str, speed_mm_min: float) -> None:
        if channel in ("0", "X"):
            self.set_speed(speed_mm_min)

    def zero_position(self) -> None:
        self.position_mm = 0.0
        self.force_kn = 0.0

    def zero_force(self) -> None:
        self.contact_position_mm = self.position_mm
        self.force_kn = 0.0

    def step(self, delta_s: float) -> None:
        if delta_s < 0:
            raise ValueError("O intervalo nao pode ser negativo.")
        self.position_mm += self.speed_mm_min * delta_s / 60.0
        compression = max(0.0, self.position_mm - self.contact_position_mm)
        self.force_kn = compression * self.stiffness_kn_mm

    @property
    def encoder_counts(self) -> int:
        return round(self.position_mm / ENCODER_MM_PER_COUNT)

    @property
    def load_counts(self) -> int:
        return round(self.force_kn * self.load_counts_per_kn)

    def frame(self) -> bytes:
        return telemetry_frame(
            load_counts=self.load_counts,
            encoder_counts=self.encoder_counts,
        )
