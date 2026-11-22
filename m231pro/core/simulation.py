from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Callable, Iterator


ACTIONS = ("compressao", "tracao", "descompressao", "manter")
ForceProvider = Callable[[float, float, float], float]
FORCE_UNITS = ("N", "kN", "lbf", "kgf")
FORCE_UNIT_TO_KN = {
    "N": 0.001,
    "kN": 1.0,
    "lbf": 0.0044482216152605,
    "kgf": 0.00980665,
}
SPECIMEN_TYPES = ("compressao", "tracao_pino", "tracao_mola")
TRACTION_STANDARDS = {
    "tracao_pino": "ASTM F606/F606M - referencia para elementos de fixacao",
    "tracao_mola": "Procedimento configuravel do fabricante - mola helicoidal",
}


def motion_target_position(initial_position_mm: float, action: str, target_position_mm: float) -> float:
    """Converte o alvo informado em magnitude para a coordenada física.

    A convenção do ensaio é: compressão desce e tração sobe. Como o encoder
    considera subida positiva, a compressão fica no lado negativo e a tração
    no lado positivo, independentemente do sinal digitado no modelo.
    """
    if action == "tracao":
        return float(initial_position_mm) + abs(
            float(target_position_mm) - float(initial_position_mm)
        )
    return float(target_position_mm)


def oriented_displacement(initial_position_mm: float, position_mm: float, action: str) -> float:
    """Retorna deslocamento positivo na direção principal do ensaio."""
    return float(position_mm) - float(initial_position_mm)


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
        if self.action == "tracao" and self.speed_mm_min <= 0:
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
    specimen_type: str = "compressao"
    standard: str = "Procedimento interno"
    duration_h: float = 0.0
    acquisition_interval_s: float = 60.0
    target_force_kn: float = 0.0
    traction_stiffness_kn_mm: float = 1.0
    load_profile: list[dict[str, float]] = field(default_factory=list)
    auto_return_traction: bool = True

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
        if self.specimen_type not in SPECIMEN_TYPES:
            raise ValueError("Tipo de corpo de prova invalido.")
        if self.specimen_type != "compressao":
            if self.duration_h <= 0:
                raise ValueError("A duracao do ensaio de tracao deve ser maior que zero.")
            if self.acquisition_interval_s <= 0:
                raise ValueError("O intervalo temporal de aquisicao deve ser maior que zero.")
            if self.target_force_kn < 0 or self.target_force_kn > self.force_limit_kn:
                raise ValueError("A carga-alvo deve ficar entre zero e o limite de seguranca.")
            if self.traction_stiffness_kn_mm <= 0:
                raise ValueError("A rigidez equivalente deve ser maior que zero.")
            previous_time = -1.0
            for point in self.load_profile:
                point_time = float(point["time_s"])
                point_force = float(point["force_kn"])
                if point_time < 0 or point_time <= previous_time:
                    raise ValueError("Os tempos do perfil de carga devem ser crescentes.")
                if point_force < 0 or point_force > self.force_limit_kn:
                    raise ValueError("O perfil de carga excede o limite de seguranca.")
                previous_time = point_time
        if not self.steps:
            raise ValueError("Adicione pelo menos uma etapa ao ensaio.")

        has_compression = False
        has_traction = False
        for step in self.steps:
            step.validate(self.position_min_mm, self.position_max_mm)
            if step.action == "compressao":
                if step.target_position_mm == self.initial_position_mm:
                    raise ValueError("A posição de compressão deve diferir da posição inicial.")
                has_compression = True
            elif step.action == "tracao":
                has_traction = True
            elif step.action == "descompressao" and not has_compression:
                raise ValueError("A descompressão precisa vir após uma compressão.")
        if self.specimen_type == "compressao" and not has_compression:
            raise ValueError("Adicione pelo menos uma etapa de compressão.")
        if self.specimen_type != "compressao" and not has_traction:
            raise ValueError("Adicione pelo menos uma etapa de tração.")

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
            specimen_type=str(data.get("specimen_type", "compressao")),
            standard=str(data.get("standard", "Procedimento interno")),
            duration_h=float(data.get("duration_h", 0.0)),
            acquisition_interval_s=float(data.get("acquisition_interval_s", 60.0)),
            target_force_kn=float(data.get("target_force_kn", 0.0)),
            traction_stiffness_kn_mm=float(data.get("traction_stiffness_kn_mm", 1.0)),
            load_profile=[
                {"time_s": float(item["time_s"]), "force_kn": float(item["force_kn"])}
                for item in data.get("load_profile", [])
            ],
            auto_return_traction=bool(data.get("auto_return_traction", True)),
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
        start_elapsed_s: float = 0.0,
    ):
        program.validate()
        if sample_period_s <= 0:
            raise ValueError("O período de amostragem deve ser maior que zero.")
        self.program = program
        self.sample_period_s = sample_period_s
        self.force_provider = force_provider or (lambda _position, _displacement, _time: 0.0)
        if start_elapsed_s < 0:
            raise ValueError("O tempo inicial do ensaio não pode ser negativo.")
        self.start_elapsed_s = float(start_elapsed_s)
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
        displacement = oriented_displacement(
            self.program.initial_position_mm, position, action
        )
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
        elapsed = self.start_elapsed_s
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

                if step.action in ("compressao", "tracao"):
                    target = motion_target_position(
                        self.program.initial_position_mm,
                        step.action,
                        step.target_position_mm,
                    )
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


def traction_pin_template() -> TestProgram:
    return TestProgram(
        name="Tracao de pino - F606/F606M",
        force_limit_kn=61.0, position_min_mm=-100.0, position_max_mm=100.0,
        initial_position_mm=0.0, cycles=1,
        steps=[TestStep("tracao", target_position_mm=1.0, speed_mm_min=1.0)],
        specimen_type="tracao_pino", standard=TRACTION_STANDARDS["tracao_pino"],
        duration_h=200.0, acquisition_interval_s=60.0,
        target_force_kn=10.0, traction_stiffness_kn_mm=10.0,
    )


def traction_spring_template() -> TestProgram:
    return TestProgram(
        name="Tracao de mola - procedimento configuravel",
        force_limit_kn=10.0, position_min_mm=-100.0, position_max_mm=100.0,
        initial_position_mm=0.0, cycles=1,
        steps=[TestStep("tracao", target_position_mm=1.0, speed_mm_min=1.0)],
        specimen_type="tracao_mola", standard=TRACTION_STANDARDS["tracao_mola"],
        duration_h=200.0, acquisition_interval_s=60.0,
        target_force_kn=2.0, traction_stiffness_kn_mm=0.5,
    )


class TractionEngine:
    """Motor temporal: uma aquisição por intervalo, viável para ensaios de 200 h."""

    def __init__(
        self,
        program: TestProgram,
        force_provider: ForceProvider | None = None,
        start_elapsed_s: float = 0.0,
        start_position_mm: float | None = None,
    ):
        program.validate()
        if program.specimen_type == "compressao":
            raise ValueError("O TractionEngine requer um corpo de prova de tracao.")
        self.program = program
        self.force_provider = force_provider
        if start_elapsed_s < 0:
            raise ValueError("O tempo inicial do ensaio não pode ser negativo.")
        self.start_elapsed_s = float(start_elapsed_s)
        self.start_position_mm = (
            None if start_position_mm is None else float(start_position_mm)
        )
        self.stopped_by_safety = False

    def _profile_force(self, elapsed: float) -> float:
        profile = self.program.load_profile
        if not profile:
            return self.program.target_force_kn
        if elapsed <= profile[0]["time_s"]:
            return profile[0]["force_kn"]
        for left, right in zip(profile, profile[1:]):
            if elapsed <= right["time_s"]:
                fraction = (elapsed - left["time_s"]) / (right["time_s"] - left["time_s"])
                return left["force_kn"] + fraction * (right["force_kn"] - left["force_kn"])
        return profile[-1]["force_kn"]

    def samples(self) -> Iterator[Sample]:
        duration_s = self.program.duration_h * 3600.0
        elapsed = self.start_elapsed_s
        traction_step = next(
            step for step in self.program.steps if step.action == "tracao"
        )
        return_speed_mm_s = traction_step.speed_mm_min / 60.0
        return_position = self.program.initial_position_mm
        return_step = next(
            (index for index, step in enumerate(self.program.steps, start=1)
             if step.action == "tracao"),
            1,
        ) + 1
        resumed_return = elapsed > duration_s + 1e-9
        force = (
            float(self.force_provider(0.0, 0.0, elapsed))
            if self.force_provider is not None else self._profile_force(elapsed)
        )
        displacement = force / self.program.traction_stiffness_kn_mm
        if resumed_return and self.start_position_mm is not None:
            position = self.start_position_mm
            displacement = position - self.program.initial_position_mm
        while not resumed_return and elapsed <= duration_s + 1e-9:
            force = (
                float(self.force_provider(0.0, 0.0, elapsed))
                if self.force_provider is not None else self._profile_force(elapsed)
            )
            displacement = force / self.program.traction_stiffness_kn_mm
            if abs(force) > self.program.force_limit_kn:
                self.stopped_by_safety = True
                yield Sample(elapsed, force, displacement, displacement, 1, 0, "limite_seguranca", True)
                return
            yield Sample(
                elapsed,
                force,
                self.program.initial_position_mm + displacement,
                displacement,
                1,
                return_step - 1,
                "tracao",
                True,
            )
            if elapsed >= duration_s - 1e-9:
                break
            elapsed = min(
                duration_s,
                elapsed + self.program.acquisition_interval_s,
            )

        if not self.program.auto_return_traction:
            return

        # O retorno começa imediatamente após o fim da duração configurada.
        # O relógio desta etapa é calculado exclusivamente pela distância e
        # pela velocidade da tração; o intervalo temporal serve apenas para
        # espaçar as amostras exibidas.
        return_started_at = (
            duration_s if self.start_elapsed_s <= duration_s else self.start_elapsed_s
        )
        position = (
            self.start_position_mm
            if resumed_return and self.start_position_mm is not None
            else self.program.initial_position_mm + displacement
        )
        return_elapsed = return_started_at
        while abs(position - return_position) > 1e-9:
            distance = abs(position - return_position)
            step_distance = min(
                return_speed_mm_s * self.program.acquisition_interval_s,
                distance,
            )
            return_elapsed += step_distance / return_speed_mm_s
            direction = -1.0 if position > return_position else 1.0
            position += direction * step_distance
            if abs(position - return_position) <= 1e-9:
                position = return_position
            displacement = position - self.program.initial_position_mm
            yield Sample(
                return_elapsed,
                0.0,
                position,
                displacement,
                1,
                return_step,
                "descompressao",
                True,
            )
