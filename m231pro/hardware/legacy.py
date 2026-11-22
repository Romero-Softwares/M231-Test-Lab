from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from m231pro.core.simulation import Sample
from m231pro.hardware.backend import MachineState, MachineTelemetry


NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def _display_number(value: object) -> float:
    match = NUMBER_PATTERN.search(str(value or ""))
    return float(match.group(0).replace(",", ".")) if match else 0.0


class LegacyBridgeClient:
    """Cliente JSON para o controlador original, executado no runtime x86 exigido."""

    def __init__(self, original_root: str | Path, timeout_s: float = 10.0):
        self.original_root = Path(original_root).resolve()
        self.timeout_s = timeout_s
        self.process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> dict[str, object]:
        if self.is_running:
            return self.request("ping")
        powershell = Path(r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe")
        bridge_script = Path(__file__).with_name("legacy_bridge.ps1")
        if not powershell.is_file():
            raise RuntimeError("PowerShell x86 nao encontrado; o controlador original exige 32 bits.")
        startupinfo = None
        creationflags = 0
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(bridge_script),
                "-OriginalRoot",
                str(self.original_root),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        threading.Thread(target=self._read_responses, daemon=True).start()
        return self.request("ping")

    def _read_responses(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            if line.strip():
                self._responses.put(line)

    def request(self, action: str, **payload: object) -> dict[str, object]:
        with self._lock:
            if not self.is_running or self.process is None or self.process.stdin is None:
                raise RuntimeError("O bridge do controlador original nao esta ativo.")
            message = json.dumps({"action": action, **payload}, ensure_ascii=False)
            self.process.stdin.write(message + "\n")
            self.process.stdin.flush()
            try:
                response_line = self._responses.get(timeout=self.timeout_s)
            except queue.Empty as error:
                raise TimeoutError(f"O controlador original nao respondeu a '{action}'.") from error
            response = json.loads(response_line)
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error", "Falha no controlador original.")))
            return response

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                self.request("shutdown")
            except (RuntimeError, TimeoutError, OSError, json.JSONDecodeError):
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self.process = None


class LegacyOriginalBackend:
    """Backend fisico que reutiliza a logica e os comandos do TESTPro original."""

    def __init__(self, original_root: str | Path):
        self.client = LegacyBridgeClient(original_root)
        self._telemetry = MachineTelemetry(time.time())
        self.position_min_mm = 0.0
        self.position_max_mm = 100.0
        self._return_target_mm: float | None = None
        self._return_direction = 0

    @property
    def mode_name(self) -> str:
        return "Controlador original FTDI"

    def connect(self) -> None:
        self.client.start()
        self.client.request("connect")
        self._telemetry = replace(
            self._telemetry,
            timestamp=time.time(),
            state=MachineState.IDLE,
            hardware_connected=True,
            alarm="",
        )

    def disconnect(self) -> None:
        self.client.close()
        self._telemetry = replace(
            self._telemetry,
            timestamp=time.time(),
            state=MachineState.OFFLINE,
            hardware_connected=False,
        )

    def configure_limits(self, minimum_mm: float, maximum_mm: float) -> None:
        self.position_min_mm = minimum_mm
        self.position_max_mm = maximum_mm

    def _command(self, name: str, state: MachineState, **payload: object) -> None:
        self.client.request("command", name=name, **payload)
        self._telemetry = replace(
            self._telemetry, timestamp=time.time(), state=state, alarm=""
        )

    def telemetry(self) -> MachineTelemetry:
        response = self.client.request("telemetry")
        force = _display_number(response.get("force"))
        position = _display_number(response.get("position"))
        alarm = str(response.get("limit") or "").strip()
        reached_return_target = self._return_target_mm is not None and (
            (self._return_direction > 0 and position >= self._return_target_mm)
            or (self._return_direction < 0 and position <= self._return_target_mm)
        )
        if reached_return_target:
            self.client.request("command", name="stop")
            self._return_target_mm = None
            self._return_direction = 0
        self._telemetry = replace(
            self._telemetry,
            timestamp=time.time(),
            force_kn=force,
            position_mm=position,
            displacement_mm=_display_number(response.get("length")),
            hardware_connected=True,
            alarm=alarm,
            state=MachineState.IDLE if reached_return_target else self._telemetry.state,
        )
        return self._telemetry

    def tick(self, delta_s: float | None = None) -> MachineTelemetry:
        return self.telemetry()

    def jog(self, direction: int, speed_mm_min: float) -> None:
        self._return_target_mm = None
        self._return_direction = 0
        name = "jog_up" if direction > 0 else "jog_down"
        state = MachineState.JOG_UP if direction > 0 else MachineState.JOG_DOWN
        self._command(name, state, speed_mm_min=speed_mm_min)

    def return_position(self, target_position_mm: float, speed_mm_min: float) -> None:
        if speed_mm_min <= 0:
            raise ValueError("A velocidade de retorno deve ser maior que zero.")
        if not self.position_min_mm <= target_position_mm <= self.position_max_mm:
            raise ValueError("A posicao inicial fica fora dos limites configurados.")
        current_position = self._telemetry.position_mm
        if abs(target_position_mm - current_position) <= 1e-6:
            self._return_target_mm = None
            self._return_direction = 0
            self._telemetry = replace(self._telemetry, state=MachineState.IDLE)
            return
        self._return_direction = 1 if target_position_mm > current_position else -1
        self._return_target_mm = target_position_mm
        name = "jog_up" if self._return_direction > 0 else "jog_down"
        self._command(
            name,
            MachineState.RETURNING,
            speed_mm_min=speed_mm_min,
        )

    def zero_position(self) -> None:
        self._command("zero_position", MachineState.IDLE)

    def zero_force(self) -> None:
        self._command("zero_force", MachineState.IDLE)

    def begin_test(self, traction_speed_mm_min: float | None = None) -> None:
        self._command("run", MachineState.RUNNING)

    def apply_sample(self, sample: Sample) -> None:
        return None

    def pause(self) -> None:
        self._command("pause", MachineState.PAUSED)

    def resume(self) -> None:
        self._command("pause", MachineState.RUNNING)

    def complete(self) -> None:
        self._telemetry = replace(self._telemetry, state=MachineState.IDLE)

    def stop(self) -> None:
        self._return_target_mm = None
        self._return_direction = 0
        if self.client.is_running:
            self._command("stop", MachineState.STOPPED)

    def emergency_stop(self, reason: str) -> None:
        self._return_target_mm = None
        self._return_direction = 0
        if self.client.is_running:
            self.client.request("command", name="stop")
        self._telemetry = replace(
            self._telemetry, state=MachineState.ALARM, alarm=reason
        )
