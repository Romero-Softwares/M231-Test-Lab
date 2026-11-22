from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Protocol

from usb_machine_emulator.driver_transport import DriverStatus
from usb_machine_emulator.protocol import CommandDecoder, MachineModel


class EmulatorDriver(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def plug_in(self) -> None: ...
    def plug_out(self) -> None: ...
    def pull_host_bytes(self, maximum: int = 4096) -> bytes: ...
    def push_machine_bytes(self, payload: bytes) -> None: ...
    def status_info(self) -> DriverStatus: ...


@dataclass(frozen=True)
class PanelSnapshot:
    synchronized: bool
    position_mm: float
    force_kn: float
    speed_mm_min: float
    frame_count: int
    command_source: str
    status_text: str
    compatibility_text: str
    queued_telemetry_bytes: int
    force_unit: str


class MachinePanelController:
    """Estado compartilhado pelo USB virtual e pelo painel manual da maquina."""

    def __init__(
        self,
        driver: EmulatorDriver,
        model: MachineModel | None = None,
        synchronization_timeout_s: float = 3.0,
        allow_manual_when_unsynced: bool = False,
        compatibility_text: str = "M231Pro",
    ) -> None:
        self.driver = driver
        self.model = model or MachineModel()
        self.decoder = CommandDecoder()
        self.synchronization_timeout_s = synchronization_timeout_s
        self.allow_manual_when_unsynced = allow_manual_when_unsynced
        self.compatibility_text = compatibility_text
        self._started = False
        self._previous_time = 0.0
        self._last_host_activity = 0.0
        self._last_host_read_count = 0
        self._last_host_write_count = 0
        self._frame_count = 0
        self._queued_telemetry_bytes = 0
        self._command_source = "AGUARDANDO SOFTWARE"
        self._force_unit = "kN"

    def start(self, now: float | None = None) -> None:
        if self._started:
            return
        # Reabrir o painel no mesmo processo inicia uma maquina virtual limpa.
        self.model.reset_session()
        self.driver.open()
        try:
            self.driver.plug_in()
        except Exception:
            self.driver.close()
            raise
        # Os contadores do driver podem sobreviver a uma sessao anterior.
        # Atividade anterior a este start nao deve sincronizar o painel novo.
        baseline = self.driver.status_info()
        self._last_host_read_count = baseline.host_read_count
        self._last_host_write_count = baseline.host_write_count
        self._last_host_activity = 0.0
        self._previous_time = time.monotonic() if now is None else now
        self._started = True

    def close(self) -> None:
        if not self._started:
            return
        self.model.set_speed(0.0)
        try:
            self.driver.plug_out()
        finally:
            self.driver.close()
            self._started = False

    def _observe_host(
        self,
        status: DriverStatus,
        commands: list[tuple[str, float]],
        now: float,
    ) -> None:
        # Somente uma leitura feita pelo software externo confirma que ele
        # recebeu a telemetria. O contador de escrita pode mudar quando o
        # proprio painel publica um quadro no transporte compartilhado.
        if status.host_read_count != self._last_host_read_count:
            self._last_host_read_count = status.host_read_count
            self._last_host_write_count = status.host_write_count
            self._last_host_activity = now

    def is_synchronized(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        return bool(
            self._started
            and self._last_host_activity
            and moment - self._last_host_activity <= self.synchronization_timeout_s
        )

    def _ensure_manual_allowed(self, now: float | None = None) -> None:
        if not self.allow_manual_when_unsynced and not self.is_synchronized(now):
            raise RuntimeError(
                "Controle bloqueado: abra o M231Pro e aguarde a sincronizacao."
            )

    def tick(self, now: float | None = None) -> PanelSnapshot:
        if not self._started:
            raise RuntimeError("O painel da maquina ainda nao foi iniciado.")
        moment = time.monotonic() if now is None else now
        commands = self.decoder.feed(self.driver.pull_host_bytes())
        status = self.driver.status_info()
        self._queued_telemetry_bytes = status.rx_bytes_queued
        self._observe_host(status, commands, moment)

        for channel, speed in commands:
            if channel == "U":
                self._force_unit = ("N", "kN", "lbf", "kgf")[round(speed)]
            elif channel == "P":
                self.model.zero_position()
            elif channel == "F":
                self.model.zero_force()
            elif channel == "R":
                # FT_ResetDevice da DLL virtual inicia uma sessao nova do
                # emulador; nao basta limpar as filas do transporte porque o
                # estado fisico simulado vive neste processo.
                self.model.reset_session()
            else:
                self.model.apply_command(channel, speed)
        if commands:
            self._command_source = "SOFTWARE"

        if (
            not self.allow_manual_when_unsynced
            and not self.is_synchronized(moment)
            and self.model.speed_mm_min
        ):
            self.model.set_speed(0.0)
            self._command_source = "BLOQUEIO DE SEGURANCA"

        delta_s = max(0.0, moment - self._previous_time)
        self._previous_time = moment
        self.model.step(delta_s)

        frame = self.model.frame()
        if status.rx_bytes_queued < len(frame):
            self.driver.push_machine_bytes(frame)
            updated = self.driver.status_info()
            self._queued_telemetry_bytes = updated.rx_bytes_queued
            self._frame_count += 1

        return self.snapshot(moment)

    def snapshot(self, now: float | None = None) -> PanelSnapshot:
        synchronized = self.is_synchronized(now)
        return PanelSnapshot(
            synchronized=synchronized,
            position_mm=self.model.position_mm,
            force_kn=self.model.force_kn,
            speed_mm_min=self.model.speed_mm_min,
            frame_count=self._frame_count,
            command_source=self._command_source,
            status_text="SINCRONIZADA" if synchronized else "AGUARDANDO SOFTWARE",
            compatibility_text=self.compatibility_text,
            queued_telemetry_bytes=self._queued_telemetry_bytes,
            force_unit=self._force_unit,
        )

    def manual_jog(
        self, direction: int, speed_mm_min: float, now: float | None = None
    ) -> None:
        if direction not in (-1, 1):
            raise ValueError("A direcao manual deve ser -1 ou 1.")
        if speed_mm_min <= 0:
            raise ValueError("A velocidade manual deve ser maior que zero.")
        self._ensure_manual_allowed(now)
        self.model.set_speed(direction * speed_mm_min)
        self._command_source = "PAINEL MANUAL"

    def manual_stop(self, now: float | None = None) -> None:
        self._ensure_manual_allowed(now)
        self.model.set_speed(0.0)
        self._command_source = "PAINEL MANUAL"

    def zero_position(self, now: float | None = None) -> None:
        self._ensure_manual_allowed(now)
        self.model.zero_position()
        self._command_source = "ZERO DE POSICAO"

    def zero_force(self, now: float | None = None) -> None:
        self._ensure_manual_allowed(now)
        self.model.zero_force()
        self._command_source = "ZERO DE CARGA"
