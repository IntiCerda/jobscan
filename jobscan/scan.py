"""The pipeline: sweep, filter, rank.

Extracted from cli.py so the markdown report and the web UI consume the same
result instead of each re-running the orchestration.

Rows are plain dicts rather than `Job` objects because a result is stored as
JSON between runs. A scan costs minutes of network and embedding; the web UI
has to open on yesterday's answer instantly rather than making the reader wait
for a fresh one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import tomllib

from . import api, embed
from .scoring import knockouts, score
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "profile.toml"
DEFAULT_DB = ROOT / ".jobscan.sqlite3"


def load_profile(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


@dataclass
class Result:
    """One complete scan, in the shape both renderers read and SQLite stores."""

    finished_at: str = ""
    semantic_on: bool = False
    swept: int = 0
    jobs: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    seniority_names: dict[str, str] = field(default_factory=dict)

    @property
    def new_count(self) -> int:
        return sum(1 for j in self.jobs if j["is_new"])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Result":
        # Only known fields are taken: a snapshot written by an older version
        # must still open instead of crashing the page it was meant to fill.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def age_days(published_at: str | None) -> float:
    """Days since publication, recomputed at render time.

    The timestamp is stored rather than a precomputed age so a snapshot read
    tomorrow does not report yesterday's numbers. An unknown date reads as
    brand new, matching `Job.age_days`, so a missing timestamp never buries an
    otherwise good posting.
    """
    if not published_at:
        return 0.0
    try:
        when = datetime.fromisoformat(published_at)
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - when).total_seconds() / 86400.0, 0.0)


def _row(job: api.Job, sc, *, is_new: bool) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "url": job.url,
        "company_id": job.company_id,
        "category": job.category,
        "countries": list(job.countries),
        "seniority_id": job.seniority_id,
        "min_salary": job.min_salary,
        "max_salary": job.max_salary,
        "published_at": job.published_at.isoformat() if job.published_at else None,
        "applications": job.applications_count,
        "is_new": is_new,
        "score": sc.total,
        "parts": sc.parts,
        "matched": sc.matched,
        "penalized": sc.penalized,
        "blocked": sc.blocked,
        "openness": sc.openness,
        "semantic": sc.semantic,
    }


def identity_text(profile: dict, cv: str = "") -> str:
    """What the semantic layer compares every posting against.

    The CV is appended to the summary rather than replacing it: the summary
    says what the person wants, the CV says what they have done, and a posting
    should resonate with both. The embedder truncates long input itself.
    """
    summary = profile.get("identity", {}).get("summary", "").strip()
    cv = cv.strip()
    return f"{summary}\n\n{cv}".strip() if cv else summary


def run(
    *,
    profile: dict,
    db: Path,
    cv: str = "",
    no_semantic: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> Result:
    """Sweep, filter, rank, persist. Returns the whole run.

    `on_progress` takes one already-formatted line so both the terminal and the
    web log can show the same thing without either owning the wording.
    """

    def say(line: str) -> None:
        if on_progress:
            on_progress(line)

    search_conf = profile.get("search", {})
    say("Barriendo Get on Board…")

    jobs = api.sweep(
        search_conf.get("queries", []),
        category=search_conf.get("category"),
        remote_only=search_conf.get("remote_only", True),
        max_pages=int(search_conf.get("max_pages_per_query", 3)),
        on_progress=lambda q, new, err: say(f"  {q:<24} +{new}" + (f" [{err}]" if err else "")),
    )
    say(f"{len(jobs)} avisos únicos")

    kept: list[api.Job] = []
    dropped: list[dict] = []
    for job in jobs:
        reasons = knockouts(job, profile)
        if reasons:
            dropped.append(
                {"id": job.id, "title": job.title, "url": job.url, "reasons": reasons}
            )
        else:
            kept.append(job)
    say(f"{len(kept)} pasan el filtro, {len(dropped)} descartados")

    embedder: embed.Embedder = embed.NullEmbedder()
    if not no_semantic:
        embedder = embed.resolve()
        if isinstance(embedder, embed.NullEmbedder):
            say("Ollama no disponible — solo keywords")

    semantic_on = not isinstance(embedder, embed.NullEmbedder)
    model = embed.DEFAULT_MODEL

    with Store(db) as store:
        known = store.known_ids()

        profile_vector = (
            embedder.embed(identity_text(profile, cv)) if semantic_on else None
        )
        if semantic_on and profile_vector is None:
            # The profile is what everything is compared against; without it the
            # semantic stage has no reference and is simply off.
            say("No se pudo embeber el perfil — solo keywords")
            semantic_on = False

        rows: list[dict] = []
        for n, job in enumerate(kept, 1):
            vector = None
            if semantic_on:
                vector = store.get_vector(job.id, model)
                if vector is None:
                    vector = embedder.embed(job.text)
                    if vector is not None:
                        store.put_vector(job.id, model, vector)
                if n % 25 == 0:
                    say(f"  embebidos {n}/{len(kept)}")

            sc = score(job, profile, job_vector=vector, profile_vector=profile_vector)
            rows.append(_row(job, sc, is_new=job.id not in known))

        rows.sort(key=lambda r: r["score"], reverse=True)

        # Recorded for every posting actually seen, not just the ones a report
        # ends up showing, so "new since last run" stays honest under --limit.
        for r in rows:
            store.record(r["id"], r["title"], r["score"])

        result = Result(
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            semantic_on=semantic_on,
            swept=len(jobs),
            jobs=rows,
            dropped=dropped,
            seniority_names=_seniority_names(),
        )
        store.save_run(result.to_dict())
        store.commit()

    return result


def last(db: Path) -> Result | None:
    """The most recent stored scan, or None if this database has never run."""
    with Store(db) as store:
        data = store.last_run()
    return Result.from_dict(data) if data else None


# The three answers a posting can get. Anything else is not a state the rest of
# the program knows how to render, so it never reaches the database.
MARK_STATES = ("applied", "saved", "discarded")


def marks(db: Path) -> dict[str, str]:
    """What the reader has already decided, by posting id."""
    with Store(db) as store:
        return store.marks()


def mark(db: Path, job_id: str, state: str) -> None:
    """Record one decision. An unknown state clears the mark rather than storing it."""
    with Store(db) as store:
        store.mark(job_id, state if state in MARK_STATES else "")
        store.commit()


def _seniority_names() -> dict[str, str]:
    try:
        return api.lookup("seniorities")
    except api.ApiError:
        return {}
