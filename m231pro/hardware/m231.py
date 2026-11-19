from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import struct
import threading
import time
from typing import Callable, Protocol

from m231pro.core.simulation import Sample
from m231pro.hardware.backend import MachineState, MachineTelemetry
from m231pro.hardware.ftdi import D2xxConnectionSettings, D2xxTransport


FRAME_END = 0x0D
TELEMETRY_COMMAND = 0x7F
ADC_FULL_SCALE = 8_388_592.0


class ByteTransport(Protocol):
    @property
    def is_open(self) -> bool: ...

    def open(self, index: int = 0) -> None: ...

    def close(self) -> None: ...

    def write(self, payload: bytes) -> int: ...

    def read_available(self, maximum: int = 65536) -> bytes: ...


@dataclass(frozen=True)
class M231Calibration:
    """Escalas da maquina instalada, independentes do protocolo USB."""

    load_capacity_kn: float = 100.0
    load_adjustment_positive: float = 1.21269
    load_adjustment_negative: float = 1.21269
    adc_headroom: float = 1.05
    encoder_mm_per_count: float = 0.01
    position_direction: int = 1

    def validate(self) -> None:
        if self.load_capacity_kn <= 0:
            raise ValueError("A capacidade da celula de carga deve ser positiva.")
        if self.load_adjustment_positive <= 0 or self.load_adjustment_negative <= 0:
            raise ValueError("Os fatores de calibracao de carga devem ser positivos.")
        if self.adc_headroom <= 0 or self.encoder_mm_per_count <= 0:
            raise ValueError("As escalas de aquisicao devem ser positivas.")
        if self.position_direction not in (-1, 1):
            raise ValueError("A direcao do encoder deve ser -1 ou 1.")


@dataclass(frozen=True)
class M231RawTelemetry:
    load_counts: int
    position_analog_counts: int
    extensometer_counts: int
    auxiliary_counts: int
    encoder_counts: int
    status_1: int
    status_2: int
    control_state: int
    limit_state: int


def encode_7bit_block(raw: bytes) -> bytes:
    """Codifica quatro bytes no formato de cinco bytes usado pela M231."""
    if len(raw) != 4:
        raise ValueError("Cada bloco M231 deve conter exatamente quatro bytes.")
    high_bits = 0x80
    encoded = bytearray(5)
    for index, value in enumerate(raw):
        encoded[index] = (value & 0x7F) | 0x80
        high_bits |= ((value >> 7) & 1) << index
    encoded[4] = high_bits
    return bytes(encoded)


def decode_7bit_block(encoded: bytes) -> bytes:
    if len(encoded) != 5:
        raise ValueError("Cada bloco M231 codificado deve conter cinco bytes.")
    high_bits = encoded[4]
    return bytes(
        (encoded[index] & 0x7F) | (((high_bits >> index) & 1) << 7)
        for index in range(4)
    )


def encode_command(command: int, payload: bytes) -> bytes:
    if not 0 <= command <= 0x7F:
        raise ValueError("O comando M231 deve caber em sete bits.")
    if len(payload) % 4:
        raise ValueError("O payload M231 deve ser multiplo de quatro bytes.")
    body = b"".join(
        encode_7bit_block(payload[offset : offset + 4])
        for offset in range(0, len(payload), 4)
    )
    return bytes((command,)) + body + bytes((FRAME_END,))


def motion_frames(speed_mm_min: float) -> tuple[bytes, bytes]:
    """Monta os dois canais de velocidade exigidos pela controladora."""
    if not math.isfinite(speed_mm_min):
        raise ValueError("A velocidade deve ser um numero finito.")
    payload = struct.pack("<f", float(speed_mm_min))
    return encode_command(ord("0"), payload), encode_command(ord("X"), payload)


def decode_telemetry_frame(frame: bytes) -> M231RawTelemetry:
    """Decodifica o quadro 0x7F de sete blocos recebido continuamente."""
    if frame.endswith(bytes((FRAME_END,))):
        frame = frame[:-1]
    if len(frame) != 36 or frame[0] != TELEMETRY_COMMAND:
        raise ValueError("Quadro de telemetria M231 invalido.")
    encoded = frame[1:]
    raw = b"".join(
        decode_7bit_block(encoded[offset : offset + 5])
        for offset in range(0, len(encoded), 5)
    )
    values = struct.unpack_from("<iiiii", raw)
    return M231RawTelemetry(
        *values,
        status_1=raw[20],
        status_2=raw[21],
        control_state=raw[22],
        limit_state=raw[23],
    )


class M231FrameDecoder:
    """Separa quadros mesmo quando a D2XX entrega dados fragmentados."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[M231RawTelemetry]:
        self._buffer.extend(data)
        telemetry: list[M231RawTelemetry] = []
        while FRAME_END in self._buffer:
            end = self._buffer.index(FRAME_END)
            frame = bytes(self._buffer[:end])
            del self._buffer[: end + 1]
            if not frame or frame[0] != TELEMETRY_COMMAND:
                continue
            try:
                telemetry.append(decode_telemetry_frame(frame))
            except ValueError:
                continue
        if len(self._buffer) > 4096:
            self._buffer.clear()
        return telemetry


TransportFactory = Callable[[str | Path, D2xxConnectionSettings], ByteTransport]


class NativeM231Backend:
    """Controle direto da M231 por D2XX, sem processo ou assembly do TESTPro."""

    is_physical = True

    def __init__(
        self,
        dll_path: str | Path,
        device_index: int = 0,
        calibration: M231Calibration | None = None,
        transport_factory: TransportFactory | None = None,
        connect_timeout_s: float = 2.0,
    ):
        self.dll_path = Path(dll_path)
        self.device_index = device_index
        self.calibration = calibration or M231Calibration()
        self.calibration.validate()
        self.connect_timeout_s = connect_timeout_s
        factory = transport_factory or (
            lambda path, settings: D2xxTransport(path, settings)
        )
        self.transport = factory(self.dll_path, D2xxConnectionSettings())
        self.position_min_mm = -100.0
        self.position_max_mm = 100.0
        self.force_limit_kn = math.inf
        self._decoder = M231FrameDecoder()
        self._lock = threading.RLock()
        self._raw: M231RawTelemetry | None = None
        self._load_zero_counts: int | None = None
        self._position_zero_counts: int | None = None
        self._return_target_mm: float | None = None
        self._return_direction = 0
        self._commanded_speed = 0.0
        self._paused_speed = 0.0
        self._last_plan_sample: Sample | None = None
        self._last_frame_monotonic = 0.0
        self._received_byte_count = 0
        self._received_preview = bytearray()
        self._alarm_latched = False
        self._telemetry = MachineTelemetry(time.time())

    @property
    def mode_name(self) -> str:
        return "Controlador M231 FTDI nativo (Python)"

    def connect(self) -> None:
        self._decoder = M231FrameDecoder()
        self._received_byte_count = 0
        self._received_preview.clear()
        with self._lock:
            self.transport.open(self.device_index)
        deadline = time.monotonic() + self.connect_timeout_s
        while time.monotonic() < deadline:
            if self._read_hardware():
                with self._lock:
                    self._alarm_latched = False
                    self._telemetry = replace(
                        self._telemetry,
                        timestamp=time.time(),
                        state=MachineState.IDLE,
                        hardware_connected=True,
                        alarm="",
                    )
                return
            time.sleep(0.01)
        self.transport.close()
        if self._received_byte_count:
            preview = self._received_preview.hex(" ")
            detail = (
                f"Foram recebidos {self._received_byte_count} bytes em formato inesperado; "
                f"amostra hexadecimal: {preview}."
            )
        else:
            detail = "Nenhum byte foi recebido da controladora."
        raise TimeoutError(
            "A FTDI abriu, mas a controladora M231 nao enviou telemetria valida. "
            + detail
        )

    def disconnect(self) -> None:
        with self._lock:
            if self.transport.is_open:
                try:
                    self._write_speed(0.0)
                finally:
                    self.transport.close()
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                state=MachineState.OFFLINE,
                hardware_connected=False,
            )

    def configure_limits(self, minimum_mm: float, maximum_mm: float) -> None:
        if minimum_mm >= maximum_mm:
            raise ValueError("O limite minimo deve ser menor que o limite maximo.")
        with self._lock:
            self.position_min_mm = minimum_mm
            self.position_max_mm = maximum_mm

    def configure_safety(self, force_limit_kn: float) -> None:
        if force_limit_kn <= 0:
            raise ValueError("O limite de carga deve ser maior que zero.")
        with self._lock:
            self.force_limit_kn = force_limit_kn

    def _write_speed(self, speed_mm_min: float) -> None:
        if not self.transport.is_open:
            raise RuntimeError("A controladora M231 nao esta conectada.")
        first, second = motion_frames(speed_mm_min)
        self.transport.write(first)
        self.transport.write(second)
        self._commanded_speed = float(speed_mm_min)

    def _engineering_values(
        self, raw: M231RawTelemetry
    ) -> tuple[float, float, float]:
        if self._load_zero_counts is None:
            self._load_zero_counts = raw.load_counts
        if self._position_zero_counts is None:
            self._position_zero_counts = raw.encoder_counts
        load_delta = raw.load_counts - self._load_zero_counts
        load_adjustment = (
            self.calibration.load_adjustment_positive
            if load_delta >= 0
            else self.calibration.load_adjustment_negative
        )
        force_kn = (
            load_delta
            / ADC_FULL_SCALE
            * self.calibration.adc_headroom
            * self.calibration.load_capacity_kn
            * load_adjustment
        )
        position_mm = (
            (raw.encoder_counts - self._position_zero_counts)
            * self.calibration.encoder_mm_per_count
            * self.calibration.position_direction
        )
        return force_kn, position_mm, position_mm

    def _read_hardware(self) -> bool:
        with self._lock:
            if not self.transport.is_open:
                return False
            chunk = self.transport.read_available()
            if chunk:
                self._received_byte_count += len(chunk)
                preview_space = 64 - len(self._received_preview)
                if preview_space > 0:
                    self._received_preview.extend(chunk[:preview_space])
            frames = self._decoder.feed(chunk) if chunk else []
            for raw in frames:
                self._raw = raw
                self._last_frame_monotonic = time.monotonic()
                force, position, displacement = self._engineering_values(raw)
                self._telemetry = replace(
                    self._telemetry,
                    timestamp=time.time(),
                    force_kn=force,
                    position_mm=position,
                    displacement_mm=displacement,
                    hardware_connected=True,
                )
            return bool(frames)

    def telemetry(self) -> MachineTelemetry:
        self._read_hardware()
        with self._lock:
            telemetry = self._telemetry
            if (
                self._commanded_speed
                and self._last_frame_monotonic
                and time.monotonic() - self._last_frame_monotonic > 0.75
            ):
                self.emergency_stop("Telemetria da M231 interrompida durante o movimento")
                return self._telemetry
            if abs(telemetry.force_kn) > self.force_limit_kn:
                self.emergency_stop(
                    f"Limite de carga excedido: {telemetry.force_kn:.3f} kN"
                )
                return self._telemetry
            upper_limit_reached = (
                self._commanded_speed > 0
                and telemetry.position_mm >= self.position_max_mm
            )
            lower_limit_reached = (
                self._commanded_speed < 0
                and telemetry.position_mm <= self.position_min_mm
            )
            if upper_limit_reached or lower_limit_reached:
                limit_name = "superior" if upper_limit_reached else "inferior"
                limit_value = (
                    self.position_max_mm if upper_limit_reached else self.position_min_mm
                )
                self._write_speed(0.0)
                self._return_target_mm = None
                self._return_direction = 0
                self._telemetry = replace(
                    self._telemetry,
                    timestamp=time.time(),
                    state=MachineState.STOPPED,
                    alarm=(
                        f"Limite {limit_name} de curso atingido: "
                        f"{limit_value:g} mm"
                    ),
                )
                return self._telemetry
            target = self._return_target_mm
            reached = target is not None and (
                (self._return_direction > 0 and telemetry.position_mm >= target)
                or (self._return_direction < 0 and telemetry.position_mm <= target)
            )
            if reached:
                self._write_speed(0.0)
                self._return_target_mm = None
                self._return_direction = 0
                self._telemetry = replace(
                    self._telemetry, timestamp=time.time(), state=MachineState.IDLE
                )
            return self._telemetry

    def tick(self, delta_s: float | None = None) -> MachineTelemetry:
        return self.telemetry()

    def jog(self, direction: int, speed_mm_min: float) -> None:
        if direction not in (-1, 1):
            raise ValueError("A direcao deve ser -1 ou 1.")
        if speed_mm_min <= 0:
            raise ValueError("A velocidade manual deve ser maior que zero.")
        with self._lock:
            if self._alarm_latched:
                raise RuntimeError("Alarme travado; reconecte a M231 apos verificar a maquina.")
            current_position = self._telemetry.position_mm
            if direction > 0 and current_position >= self.position_max_mm:
                raise ValueError("A maquina ja esta no limite superior de curso.")
            if direction < 0 and current_position <= self.position_min_mm:
                raise ValueError("A maquina ja esta no limite inferior de curso.")
            self._return_target_mm = None
            self._return_direction = 0
            self._write_speed(direction * speed_mm_min)
            state = MachineState.JOG_UP if direction > 0 else MachineState.JOG_DOWN
            self._telemetry = replace(
                self._telemetry, timestamp=time.time(), state=state, alarm=""
            )

    def return_position(self, target_position_mm: float, speed_mm_min: float) -> None:
        if speed_mm_min <= 0:
            raise ValueError("A velocidade de retorno deve ser maior que zero.")
        if not self.position_min_mm <= target_position_mm <= self.position_max_mm:
            raise ValueError("A posicao inicial fica fora dos limites configurados.")
        current = self.telemetry().position_mm
        with self._lock:
            if self._alarm_latched:
                raise RuntimeError("Alarme travado; reconecte a M231 apos verificar a maquina.")
            if abs(target_position_mm - current) <= 1e-6:
                self._write_speed(0.0)
                self._telemetry = replace(self._telemetry, state=MachineState.IDLE)
                return
            self._return_target_mm = target_position_mm
            self._return_direction = 1 if target_position_mm > current else -1
            self._write_speed(self._return_direction * speed_mm_min)
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                state=MachineState.RETURNING,
                alarm="",
            )

    def zero_position(self) -> None:
        self._read_hardware()
        with self._lock:
            if self._raw is None:
                raise RuntimeError("Nao ha leitura da M231 para zerar a posicao.")
            self._position_zero_counts = self._raw.encoder_counts
            self._telemetry = replace(
                self._telemetry, position_mm=0.0, displacement_mm=0.0
            )

    def zero_force(self) -> None:
        self._read_hardware()
        with self._lock:
            if self._raw is None:
                raise RuntimeError("Nao ha leitura da M231 para zerar a carga.")
            self._load_zero_counts = self._raw.load_counts
            self._telemetry = replace(self._telemetry, force_kn=0.0)

    def begin_test(self) -> None:
        with self._lock:
            if self._alarm_latched:
                raise RuntimeError("Alarme travado; reconecte a M231 apos verificar a maquina.")
            self._last_plan_sample = None
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                elapsed_s=0.0,
                state=MachineState.RUNNING,
                alarm="",
            )

    def apply_sample(self, sample: Sample) -> None:
        """Converte a trajetoria validada da UI em velocidade fisica assinada."""
        with self._lock:
            if self._alarm_latched:
                return
            previous = self._last_plan_sample
            self._last_plan_sample = sample
            desired_speed = 0.0
            if previous is not None and sample.action in ("compressao", "descompressao"):
                delta_time = sample.time_s - previous.time_s
                if delta_time > 1e-9:
                    desired_speed = (
                        (sample.position_mm - previous.position_mm) / delta_time * 60.0
                    )
            if abs(desired_speed - self._commanded_speed) > 1e-3:
                self._write_speed(desired_speed)
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                elapsed_s=sample.time_s,
                state=MachineState.RUNNING,
            )

    def pause(self) -> None:
        with self._lock:
            if self._alarm_latched:
                return
            self._paused_speed = self._commanded_speed
            self._write_speed(0.0)
            self._telemetry = replace(self._telemetry, state=MachineState.PAUSED)

    def resume(self) -> None:
        with self._lock:
            if self._alarm_latched:
                return
            self._write_speed(self._paused_speed)
            self._paused_speed = 0.0
            self._telemetry = replace(self._telemetry, state=MachineState.RUNNING)

    def complete(self) -> None:
        with self._lock:
            self._write_speed(0.0)
            self._telemetry = replace(self._telemetry, state=MachineState.IDLE)

    def stop(self) -> None:
        with self._lock:
            if self.transport.is_open:
                self._write_speed(0.0)
            self._return_target_mm = None
            self._return_direction = 0
            self._telemetry = replace(
                self._telemetry, timestamp=time.time(), state=MachineState.STOPPED
            )

    def emergency_stop(self, reason: str) -> None:
        with self._lock:
            if self.transport.is_open:
                self._write_speed(0.0)
            self._alarm_latched = True
            self._return_target_mm = None
            self._return_direction = 0
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                state=MachineState.ALARM,
                alarm=reason,
            )
