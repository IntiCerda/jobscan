"""Which postings have already been seen, and which are new since last run.

Without this the report is the same wall of text every morning and you stop
reading it. With it, the daily question becomes "what appeared since
yesterday", which is the only part that needs attention.

Embedding vectors are cached in the same database because they are the slow
part of a run: re-embedding four hundred unchanged postings on every pass wastes
minutes of GPU for an identical answer.

Finished runs are stored whole for the same reason. A scan costs minutes of
network; the web UI opens on the last one instantly rather than making the
reader wait for a fresh answer to a question they asked yesterday.
"""

from __future__ import annotations

import array
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    job_id      TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    best_score  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS vectors (
    job_id      TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    dims        INTEGER NOT NULL,
    data        BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    finished_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);
"""

# Enough history to compare against a run or two back, not enough for a few
# hundred kilobytes of JSON per scan to grow without bound.
KEEP_RUNS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- seen tracking ----------------------------------------------------

    def known_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT job_id FROM seen")}

    def record(self, job_id: str, title: str, total: float) -> None:
        """Upsert. `best_score` keeps the maximum ever assigned so that tuning
        the weights downward does not erase that a posting once looked good."""
        now = _now()
        self.db.execute(
            """
            INSERT INTO seen (job_id, title, first_seen, last_seen, best_score)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                last_seen  = excluded.last_seen,
                title      = excluded.title,
                best_score = MAX(seen.best_score, excluded.best_score)
            """,
            (job_id, title, now, now, total),
        )

    def commit(self) -> None:
        self.db.commit()

    # -- vector cache -----------------------------------------------------

    def get_vector(self, job_id: str, model: str) -> list[float] | None:
        row = self.db.execute(
            "SELECT data FROM vectors WHERE job_id = ? AND model = ?",
            (job_id, model),
        ).fetchone()
        if row is None:
            return None
        buf = array.array("f")
        buf.frombytes(row[0])
        return list(buf)

    def put_vector(self, job_id: str, model: str, vector: list[float]) -> None:
        buf = array.array("f", vector)
        self.db.execute(
            """
            INSERT INTO vectors (job_id, model, dims, data) VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                model = excluded.model,
                dims  = excluded.dims,
                data  = excluded.data
            """,
            (job_id, model, len(vector), buf.tobytes()),
        )

    # -- finished runs ----------------------------------------------------

    def save_run(self, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO runs (finished_at, payload) VALUES (?, ?)",
            (_now(), json.dumps(payload, ensure_ascii=False)),
        )
        self.db.execute(
            "DELETE FROM runs WHERE id NOT IN "
            "(SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
            (KEEP_RUNS,),
        )

    def last_run(self) -> dict | None:
        """The most recent stored run, or None if there is none yet.

        A payload that no longer parses is treated as absent rather than raised:
        an unreadable snapshot should cost the reader a re-scan, not the page.
        """
        row = self.db.execute(
            "SELECT payload FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None
