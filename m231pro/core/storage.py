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
                """
            )

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
