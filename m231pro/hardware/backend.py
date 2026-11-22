from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import threading
import time

from m231pro.core.simulation import Sample


class MachineState(str, Enum):
    OFFLINE = "offline"
    IDLE = "idle"
    JOG_UP = "jog_up"
    JOG_DOWN = "jog_down"
    RETURNING = "returning"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ALARM = "alarm"


@dataclass(frozen=True)
class MachineTelemetry:
    timestamp: float
    force_kn: float = 0.0
    position_mm: float = 0.0
    displacement_mm: float = 0.0
    elapsed_s: float = 0.0
    state: MachineState = MachineState.OFFLINE
    hardware_connected: bool = False
    alarm: str = ""


class LocalMachineBackend:
    """Maquina virtual com a mesma API operacional do backend fisico."""

    def __init__(self, position_min_mm: float = -100.0, position_max_mm: float = 100.0):
        self.position_min_mm = position_min_mm
        self.position_max_mm = position_max_mm
        self._position_zero_mm = 0.0
        self._saved_position_zero_mm: float | None = None
        self._force_zero_kn = 0.0
        self._velocity_mm_s = 0.0
        self._return_target_absolute_mm: float | None = None
        self._lock = threading.RLock()
        self._last_tick = time.monotonic()
        self._telemetry = MachineTelemetry(time.time(), state=MachineState.IDLE)

    @property
    def mode_name(self) -> str:
        return "Local / maquina offline"

    def configure_limits(self, minimum_mm: float, maximum_mm: float) -> None:
        if minimum_mm >= maximum_mm:
            raise ValueError("O limite minimo deve ser menor que o limite maximo.")
        with self._lock:
            self.position_min_mm = minimum_mm
            self.position_max_mm = maximum_mm

    def telemetry(self) -> MachineTelemetry:
        self.tick()
        with self._lock:
            return self._telemetry

    def tick(self, delta_s: float | None = None) -> MachineTelemetry:
        with self._lock:
            now = time.monotonic()
            if delta_s is None:
                delta_s = max(0.0, min(now - self._last_tick, 0.25))
            self._last_tick = now
            if self._velocity_mm_s:
                previous_position = self._telemetry.position_mm
                previous_absolute_position = previous_position + self._position_zero_mm
                absolute_position = (
                    previous_position
                    + self._position_zero_mm
                    + self._velocity_mm_s * delta_s
                )
                return_target = self._return_target_absolute_mm
                reached_return_target = return_target is not None and (
                    (self._velocity_mm_s > 0 and absolute_position >= return_target)
                    or (self._velocity_mm_s < 0 and absolute_position <= return_target)
                    or previous_absolute_position == return_target
                )
                if reached_return_target:
                    absolute_position = return_target
                absolute_position = min(
                    max(absolute_position, self.position_min_mm),
                    self.position_max_mm,
                )
                position = absolute_position - self._position_zero_mm
                state = self._telemetry.state
                if reached_return_target:
                    self._velocity_mm_s = 0.0
                    self._return_target_absolute_mm = None
                    state = MachineState.IDLE
                self._telemetry = replace(
                    self._telemetry,
                    timestamp=time.time(),
                    position_mm=position,
                    displacement_mm=position,
                    state=state,
                )
                if (
                    not reached_return_target
                    and absolute_position in (self.position_min_mm, self.position_max_mm)
                ):
                    self.stop()
            return self._telemetry

    def jog(self, direction: int, speed_mm_min: float) -> None:
        if direction not in (-1, 1):
            raise ValueError("A direcao deve ser -1 ou 1.")
        if speed_mm_min <= 0:
            raise ValueError("A velocidade manual deve ser maior que zero.")
        with self._lock:
            self._return_target_absolute_mm = None
            self._velocity_mm_s = direction * speed_mm_min / 60.0
            state = MachineState.JOG_UP if direction > 0 else MachineState.JOG_DOWN
            self._telemetry = replace(
                self._telemetry, timestamp=time.time(), state=state, alarm=""
            )

    def return_position(self, target_position_mm: float, speed_mm_min: float) -> None:
        if speed_mm_min <= 0:
            raise ValueError("A velocidade de retorno deve ser maior que zero.")
        with self._lock:
            target_absolute_position = target_position_mm + self._position_zero_mm
            if not self.position_min_mm <= target_absolute_position <= self.position_max_mm:
                raise ValueError("A posicao inicial fica fora dos limites configurados.")
            current_position = self._telemetry.position_mm
            if abs(target_position_mm - current_position) <= 1e-9:
                self._velocity_mm_s = 0.0
                self._return_target_absolute_mm = None
                state = MachineState.IDLE
            else:
                direction = 1.0 if target_position_mm > current_position else -1.0
                self._velocity_mm_s = direction * speed_mm_min / 60.0
                self._return_target_absolute_mm = target_absolute_position
                state = MachineState.RETURNING
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                state=state,
                alarm="",
            )

    def zero_position(self) -> None:
        with self._lock:
            self._saved_position_zero_mm = self._position_zero_mm
            self._position_zero_mm += self._telemetry.position_mm
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                position_mm=0.0,
                displacement_mm=0.0,
            )

    def zero_force(self) -> None:
        with self._lock:
            self._force_zero_kn += self._telemetry.force_kn
            self._telemetry = replace(
                self._telemetry, timestamp=time.time(), force_kn=0.0
            )

    def begin_test(self, traction_speed_mm_min: float | None = None) -> None:
        with self._lock:
            self._velocity_mm_s = 0.0
            self._return_target_absolute_mm = None
            if traction_speed_mm_min is not None:
                if traction_speed_mm_min <= 0:
                    raise ValueError("A velocidade de tracao deve ser maior que zero.")
                self._velocity_mm_s = traction_speed_mm_min / 60.0
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                elapsed_s=0.0,
                state=MachineState.RUNNING,
                alarm="",
            )

    def checkpoint_state(self) -> dict[str, object]:
        """Guarda a última leitura para uma retomada sem zerar a máquina virtual."""
        with self._lock:
            telemetry = self._telemetry
            return {
                "checkpoint_version": 1,
                "force_kn": telemetry.force_kn,
                "position_mm": telemetry.position_mm,
                "displacement_mm": telemetry.displacement_mm,
                "elapsed_s": telemetry.elapsed_s,
            }

    def restore_checkpoint(self, state: dict[str, object] | None) -> None:
        """Reaplica a última carga e posição salvas pelo sistema na retomada."""
        if not state:
            return
        with self._lock:
            force = state.get("force_kn")
            position = state.get("position_mm")
            displacement = state.get("displacement_mm", position)
            if force is None or position is None:
                return
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                force_kn=float(force),
                position_mm=float(position),
                displacement_mm=float(displacement),
                elapsed_s=float(state.get("elapsed_s", self._telemetry.elapsed_s)),
            )

    def resume_test(self, sample: Sample, speed_mm_min: float | None) -> None:
        """Retoma o movimento sem substituir a última leitura por zeros."""
        with self._lock:
            if speed_mm_min is not None and abs(speed_mm_min) > 0:
                self._velocity_mm_s = speed_mm_min / 60.0
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                state=MachineState.RUNNING,
                alarm="",
            )

    def apply_sample(self, sample: Sample) -> None:
        with self._lock:
            state = (
                MachineState.ALARM
                if sample.action == "limite_seguranca"
                else self._telemetry.state
            )
            alarm = "Limite de carga excedido" if state == MachineState.ALARM else ""
            self._telemetry = MachineTelemetry(
                timestamp=time.time(),
                force_kn=sample.force_kn - self._force_zero_kn,
                position_mm=sample.position_mm,
                displacement_mm=sample.displacement_mm,
                elapsed_s=sample.time_s,
                state=state,
                hardware_connected=False,
                alarm=alarm,
            )

    def pause(self) -> None:
        with self._lock:
            if self._telemetry.state == MachineState.RUNNING:
                self._telemetry = replace(self._telemetry, state=MachineState.PAUSED)

    def resume(self) -> None:
        with self._lock:
            if self._telemetry.state == MachineState.PAUSED:
                self._telemetry = replace(self._telemetry, state=MachineState.RUNNING)

    def complete(self) -> None:
        with self._lock:
            self._velocity_mm_s = 0.0
            self._return_target_absolute_mm = None
            self._telemetry = replace(self._telemetry, state=MachineState.IDLE)

    def stop(self) -> None:
        with self._lock:
            self._velocity_mm_s = 0.0
            self._return_target_absolute_mm = None
            self._telemetry = replace(
                self._telemetry, timestamp=time.time(), state=MachineState.STOPPED
            )

    def emergency_stop(self, reason: str) -> None:
        with self._lock:
            self._velocity_mm_s = 0.0
            self._return_target_absolute_mm = None
            self._telemetry = replace(
                self._telemetry,
                timestamp=time.time(),
                state=MachineState.ALARM,
                alarm=reason,
            )


class MachineController:
    """Fachada unica usada pela UI, independentemente do backend selecionado."""

    def __init__(self, backend: LocalMachineBackend | None = None):
        self.backend = backend or LocalMachineBackend()

    def set_backend(self, backend: object) -> None:
        current = self.backend
        disconnect = getattr(current, "disconnect", None)
        if callable(disconnect):
            disconnect()
        self.backend = backend

    def disconnect(self) -> None:
        disconnect = getattr(self.backend, "disconnect", None)
        if callable(disconnect):
            disconnect()

    @property
    def mode_name(self) -> str:
        return self.backend.mode_name

    def __getattr__(self, name: str):
        return getattr(self.backend, name)
