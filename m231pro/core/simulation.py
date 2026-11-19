from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Callable, Iterator


ACTIONS = ("compressao", "descompressao", "manter")
ForceProvider = Callable[[float, float, float], float]
FORCE_UNITS = ("N", "kN", "lbf", "kgf")
FORCE_UNIT_TO_KN = {
    "N": 0.001,
    "kN": 1.0,
    "lbf": 0.0044482216152605,
    "kgf": 0.00980665,
}


def force_to_kn(value: float, unit: str) -> float:
    """Converte uma carga informada pelo operador para a unidade interna kN."""
    try:
        return float(value) * FORCE_UNIT_TO_KN[unit]
    except KeyError as error:
        raise ValueError(f"Unidade de carga inválida: {unit}") from error


def force_from_kn(value_kn: float, unit: str) -> float:
    """Converte a carga interna em kN para a unidade escolhida no ensaio."""
    try:
        return float(value_kn) / FORCE_UNIT_TO_KN[unit]
    except KeyError as error:
        raise ValueError(f"Unidade de carga inválida: {unit}") from error


@dataclass
class TestStep:
    action: str
    target_position_mm: float = 0.0
    speed_mm_min: float = 100.0
    duration_s: float = 0.0

    def validate(self, position_min_mm: float, position_max_mm: float) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"Ação inválida: {self.action}")
        if self.action == "compressao":
            if not position_min_mm <= self.target_position_mm <= position_max_mm:
                raise ValueError(
                    "A posição-alvo deve ficar entre "
                    f"{position_min_mm:g} e {position_max_mm:g} mm."
                )
            if self.speed_mm_min <= 0:
                raise ValueError("A velocidade deve ser maior que zero.")
        if self.action == "manter" and self.duration_s <= 0:
            raise ValueError("O tempo de manutenção deve ser maior que zero.")


@dataclass
class TestProgram:
    name: str = "Novo ensaio"
    force_limit_kn: float = 61.0
    force_unit: str = "kN"
    position_min_mm: float = -100.0
    position_max_mm: float = 100.0
    initial_position_mm: float = 0.0
    cycles: int = 1
    steps: list[TestStep] = field(default_factory=list)
    wait_before_decompression_s: float = 0.0
    acquisition_interval_mm: float = 10.0

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("Informe o nome do ensaio.")
        if self.force_limit_kn <= 0:
            raise ValueError("O limite de carga de segurança deve ser maior que zero.")
        if self.force_unit not in FORCE_UNITS:
            raise ValueError(
                "A unidade de carga deve ser N, kN, lbf ou kgf."
            )
        if self.position_min_mm >= self.position_max_mm:
            raise ValueError("O limite mínimo de posição deve ser menor que o máximo.")
        if not self.position_min_mm <= self.initial_position_mm <= self.position_max_mm:
            raise ValueError("A posição inicial deve ficar dentro dos limites configurados.")
        if not 1 <= self.cycles <= 10000:
            raise ValueError("A quantidade de ciclos deve ficar entre 1 e 10000.")
        if self.wait_before_decompression_s < 0:
            raise ValueError("A espera antes da descompressão não pode ser negativa.")
        if self.acquisition_interval_mm <= 0:
            raise ValueError("O intervalo de aquisição deve ser maior que zero.")
        if not self.steps:
            raise ValueError("Adicione pelo menos uma etapa ao ensaio.")

        has_compression = False
        for step in self.steps:
            step.validate(self.position_min_mm, self.position_max_mm)
            if step.action == "compressao":
                if step.target_position_mm == self.initial_position_mm:
                    raise ValueError("A posição de compressão deve diferir da posição inicial.")
                has_compression = True
            elif step.action == "descompressao" and not has_compression:
                raise ValueError("A descompressão precisa vir após uma compressão.")
        if not has_compression:
            raise ValueError("Adicione pelo menos uma etapa de compressão.")

    def save(self, path: str | Path) -> None:
        self.validate()
        Path(path).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "TestProgram":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if "force_limit_n" in data:
            raise ValueError(
                "Este arquivo usa o formato antigo por força-alvo. "
                "Crie um programa novo informando posição em mm e velocidade em mm/min."
            )
        program = cls(
            name=str(data["name"]),
            force_limit_kn=float(data["force_limit_kn"]),
            force_unit=str(data.get("force_unit", "kN")),
            position_min_mm=float(data["position_min_mm"]),
            position_max_mm=float(data["position_max_mm"]),
            initial_position_mm=float(data["initial_position_mm"]),
            cycles=int(data["cycles"]),
            wait_before_decompression_s=float(
                data.get("wait_before_decompression_s", 0.0)
            ),
            acquisition_interval_mm=float(data.get("acquisition_interval_mm", 10.0)),
            steps=[TestStep(**item) for item in data["steps"]],
        )
        program.validate()
        return program


@dataclass(frozen=True)
class Sample:
    time_s: float
    force_kn: float
    position_mm: float
    displacement_mm: float
    cycle: int
    step: int
    action: str
    acquired: bool = False


class SimulationEngine:
    """Simula posição; a carga medida é usada somente como intertravamento."""

    def __init__(
        self,
        program: TestProgram,
        sample_period_s: float = 0.05,
        force_provider: ForceProvider | None = None,
    ):
        program.validate()
        if sample_period_s <= 0:
            raise ValueError("O período de amostragem deve ser maior que zero.")
        self.program = program
        self.sample_period_s = sample_period_s
        self.force_provider = force_provider or (lambda _position, _displacement, _time: 0.0)
        self.stopped_by_safety = False

    def _sample(
        self,
        elapsed: float,
        position: float,
        cycle: int,
        step: int,
        action: str,
        acquired: bool = False,
    ) -> Sample:
        displacement = position - self.program.initial_position_mm
        force = float(self.force_provider(position, displacement, elapsed))
        return Sample(elapsed, force, position, displacement, cycle, step, action, acquired)

    def _checked_sample(
        self,
        elapsed: float,
        position: float,
        cycle: int,
        step: int,
        action: str,
        acquired: bool = False,
    ) -> Sample:
        sample = self._sample(elapsed, position, cycle, step, action, acquired)
        if abs(sample.force_kn) > self.program.force_limit_kn:
            self.stopped_by_safety = True
            return Sample(
                sample.time_s,
                sample.force_kn,
                sample.position_mm,
                sample.displacement_mm,
                sample.cycle,
                sample.step,
                "limite_seguranca",
                True,
            )
        return sample

    def samples(self) -> Iterator[Sample]:
        elapsed = 0.0
        position = self.program.initial_position_mm
        last_compression_speed = 0.0
        initial = self._checked_sample(elapsed, position, 1, 0, "inicio", True)
        yield initial
        if initial.action == "limite_seguranca":
            return

        for cycle_index in range(1, self.program.cycles + 1):
            for step_index, step in enumerate(self.program.steps, start=1):
                if step.action == "manter":
                    remaining = step.duration_s
                    while remaining > 1e-9:
                        dt = min(self.sample_period_s, remaining)
                        elapsed += dt
                        remaining -= dt
                        sample = self._checked_sample(
                            elapsed, position, cycle_index, step_index, step.action
                        )
                        yield sample
                        if sample.action == "limite_seguranca":
                            return
                    continue

                if (
                    step.action == "descompressao"
                    and self.program.wait_before_decompression_s > 0
                ):
                    remaining = self.program.wait_before_decompression_s
                    while remaining > 1e-9:
                        dt = min(self.sample_period_s, remaining)
                        elapsed += dt
                        remaining -= dt
                        sample = self._checked_sample(
                            elapsed,
                            position,
                            cycle_index,
                            step_index,
                            "aguardar_descompressao",
                            acquired=remaining <= 1e-9,
                        )
                        yield sample
                        if sample.action == "limite_seguranca":
                            return

                if step.action == "compressao":
                    target = step.target_position_mm
                    speed_mm_min = step.speed_mm_min
                    last_compression_speed = speed_mm_min
                else:
                    target = self.program.initial_position_mm
                    speed_mm_min = last_compression_speed

                speed_mm_s = speed_mm_min / 60.0
                direction = 1.0 if target > position else -1.0
                distance_since_acquisition = 0.0
                while abs(target - position) > 1e-9:
                    remaining_distance = abs(target - position)
                    distance_to_acquisition = (
                        self.program.acquisition_interval_mm
                        - distance_since_acquisition
                    )
                    change = min(
                        speed_mm_s * self.sample_period_s,
                        remaining_distance,
                        distance_to_acquisition,
                    )
                    next_position = position + direction * change
                    if remaining_distance <= change + 1e-9:
                        next_position = target
                    actual_dt = abs(next_position - position) / speed_mm_s
                    elapsed += actual_dt
                    position = next_position
                    distance_since_acquisition += change
                    acquired = (
                        distance_since_acquisition
                        >= self.program.acquisition_interval_mm - 1e-9
                        or position == target
                    )
                    if acquired:
                        distance_since_acquisition = 0.0
                    sample = self._checked_sample(
                        elapsed,
                        position,
                        cycle_index,
                        step_index,
                        step.action,
                        acquired,
                    )
                    yield sample
                    if sample.action == "limite_seguranca":
                        return


def compression_return_template() -> TestProgram:
    return TestProgram(
        name="Compressão com retorno",
        force_limit_kn=61.0,
        position_min_mm=-100.0,
        position_max_mm=100.0,
        initial_position_mm=0.0,
        cycles=1,
        wait_before_decompression_s=2.0,
        acquisition_interval_mm=10.0,
        steps=[
            TestStep("compressao", target_position_mm=50.0, speed_mm_min=100.0),
            TestStep("descompressao"),
        ],
    )


def cyclic_template() -> TestProgram:
    program = compression_return_template()
    program.name = "Compressão cíclica"
    program.cycles = 5
    program.steps[0].target_position_mm = 25.0
    program.wait_before_decompression_s = 1.0
    return program
