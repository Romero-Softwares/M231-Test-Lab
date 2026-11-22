from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from m231pro.core.simulation import Sample, TestProgram, TestStep


class StoredRun:
    """Execução persistida, pronta para exportação e geração de relatório."""

    def __init__(
        self,
        run_id: int,
        started_at: str,
        mode: str,
        status: str,
        program: TestProgram,
        samples: list[Sample],
    ) -> None:
        self.id = run_id
        self.started_at = started_at
        self.mode = mode
        self.status = status
        self.program = program
        self.samples = samples


class ResultsRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    program_name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    program_json TEXT NOT NULL,
                    sample_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    time_s REAL NOT NULL,
                    force_kn REAL NOT NULL,
                    position_mm REAL NOT NULL,
                    displacement_mm REAL NOT NULL,
                    cycle INTEGER NOT NULL,
                    step INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    acquired INTEGER NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS active_run (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    started_at TEXT NOT NULL,
                    checkpoint_at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    program_json TEXT NOT NULL,
                    samples_json TEXT NOT NULL,
                    elapsed_s REAL NOT NULL,
                    machine_json TEXT
                );
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(active_run)")
            }
            if "machine_json" not in columns:
                connection.execute("ALTER TABLE active_run ADD COLUMN machine_json TEXT")
            # Migra checkpoints antigos: estado de carga, posicao, offsets e
            # movimento manual nao pode permanecer armazenado nem ser
            # reutilizado em uma nova abertura do software.
            connection.execute("UPDATE active_run SET machine_json = NULL")

    def save_run(
        self,
        program: TestProgram,
        samples: list[Sample],
        mode: str,
        status: str,
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs
                    (started_at, program_name, mode, status, program_json, sample_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    program.name,
                    mode,
                    status,
                    json.dumps(asdict(program), ensure_ascii=False),
                    len(samples),
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO samples
                    (run_id, sequence, time_s, force_kn, position_mm,
                     displacement_mm, cycle, step, action, acquired)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        sequence,
                        sample.time_s,
                        sample.force_kn,
                        sample.position_mm,
                        sample.displacement_mm,
                        sample.cycle,
                        sample.step,
                        sample.action,
                        int(sample.acquired),
                    )
                    for sequence, sample in enumerate(samples)
                ],
            )
        return run_id

    @staticmethod
    def _program_from_json(program_json: str) -> TestProgram:
        data = json.loads(program_json)
        program = TestProgram(
            name=str(data["name"]), force_limit_kn=float(data["force_limit_kn"]),
            force_unit=str(data.get("force_unit", "kN")),
            position_min_mm=float(data["position_min_mm"]),
            position_max_mm=float(data["position_max_mm"]),
            initial_position_mm=float(data["initial_position_mm"]),
            cycles=int(data["cycles"]),
            wait_before_decompression_s=float(data.get("wait_before_decompression_s", 0.0)),
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

    @staticmethod
    def _samples_from_json(samples_json: str) -> list[Sample]:
        return [Sample(**item) for item in json.loads(samples_json)]

    def save_checkpoint(
        self,
        program: TestProgram,
        samples: list[Sample],
        mode: str,
        started_at: str,
        status: str = "em execução",
        machine_state: dict[str, object] | None = None,
    ) -> None:
        """Persiste o ensaio ativo para sobreviver a queda de energia/processo."""
        checkpoint_at = datetime.now(timezone.utc).isoformat()
        elapsed_s = max((float(sample.time_s) for sample in samples), default=0.0)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO active_run
                    (slot, started_at, checkpoint_at, mode, status,
                     program_json, samples_json, elapsed_s, machine_json)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    started_at=excluded.started_at,
                    checkpoint_at=excluded.checkpoint_at,
                    mode=excluded.mode,
                    status=excluded.status,
                    program_json=excluded.program_json,
                    samples_json=excluded.samples_json,
                    elapsed_s=excluded.elapsed_s,
                    machine_json=excluded.machine_json
                """,
                (
                    started_at,
                    checkpoint_at,
                    mode,
                    status,
                    json.dumps(asdict(program), ensure_ascii=False),
                    json.dumps([asdict(sample) for sample in samples], ensure_ascii=False),
                    elapsed_s,
                    # Estado fisico/manual nunca atravessa uma abertura do
                    # software. O argumento permanece por compatibilidade,
                    # mas a base nao grava carga, posicao ou velocidade.
                    None,
                ),
            )

    def get_checkpoint(self) -> dict[str, object] | None:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM active_run WHERE slot = 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "started_at": str(row["started_at"]),
            "checkpoint_at": str(row["checkpoint_at"]),
            "mode": str(row["mode"]),
            "status": str(row["status"]),
            "program": self._program_from_json(str(row["program_json"])),
            "samples": self._samples_from_json(str(row["samples_json"])),
            "elapsed_s": float(row["elapsed_s"]),
            # Checkpoints antigos tambem nao podem reintroduzir estado fisico.
            "machine_state": None,
        }

    def clear_checkpoint(self) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM active_run WHERE slot = 1")

    def recent_runs(self, limit: int = 50) -> list[dict[str, object]]:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, started_at, program_name, mode, status, sample_count
                FROM runs ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_run(self) -> StoredRun | None:
        """Carrega o ensaio salvo mais recentemente, incluindo as amostras."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return self.get_run(int(row[0]))

    def delete_run(self, run_id: int) -> bool:
        """Remove uma execucao e suas amostras vinculadas."""
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return cursor.rowcount > 0

    def get_run(self, run_id: int) -> StoredRun | None:
        """Carrega configuração e aquisições de uma execução do histórico."""
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            run = connection.execute(
                """
                SELECT id, started_at, mode, status, program_json
                FROM runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                return None
            rows = connection.execute(
                """
                SELECT time_s, force_kn, position_mm, displacement_mm,
                       cycle, step, action, acquired
                FROM samples WHERE run_id = ? ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()

        data = json.loads(str(run["program_json"]))
        program = TestProgram(
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
            acquisition_interval_mm=float(
                data.get("acquisition_interval_mm", 10.0)
            ),
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
        samples = [
            Sample(
                time_s=float(row["time_s"]),
                force_kn=float(row["force_kn"]),
                position_mm=float(row["position_mm"]),
                displacement_mm=float(row["displacement_mm"]),
                cycle=int(row["cycle"]),
                step=int(row["step"]),
                action=str(row["action"]),
                acquired=bool(row["acquired"]),
            )
            for row in rows
        ]
        return StoredRun(
            run_id=int(run["id"]),
            started_at=str(run["started_at"]),
            mode=str(run["mode"]),
            status=str(run["status"]),
            program=program,
            samples=samples,
        )
