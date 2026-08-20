"""Entry point: sweep, filter, rank, report.

    python -m jobscan                    # rank everything, mark new ones
    python -m jobscan --new-only         # only postings unseen until now
    python -m jobscan --no-semantic      # skip embeddings entirely
    python -m jobscan --explain          # show the score breakdown per posting
    python -m jobscan -o hoy.md          # write the report to a file
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from . import api, embed
from .scoring import Score, knockouts, score
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "profile.toml"
DEFAULT_DB = ROOT / ".jobscan.sqlite3"


def load_profile(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _age(job: api.Job) -> str:
    days = job.age_days
    if days < 1:
        return "publicada hoy"
    if days < 2:
        return "ayer"
    if days < 60:
        return f"hace {days:.0f} días"
    return f"hace {days / 30:.0f} meses"


def _salary(job: api.Job) -> str:
    if job.min_salary and job.max_salary:
        return f"${job.min_salary:,}–{job.max_salary:,}"
    if job.max_salary or job.min_salary:
        return f"${(job.max_salary or job.min_salary):,}"
    return "—"


def render(
    ranked: list[tuple[api.Job, Score]],
    *,
    new_ids: set[str],
    dropped: list[tuple[api.Job, list[str]]],
    seniority_names: dict[str, str],
    explain: bool,
    semantic_on: bool,
) -> str:
    now = datetime.now(timezone.utc).astimezone()
    lines = [
        f"# Vacantes — {now:%Y-%m-%d %H:%M}",
        "",
        f"{len(ranked)} pasaron el filtro · {len(dropped)} descartadas · "
        f"{len(new_ids)} nuevas desde la última corrida"
        + ("" if semantic_on else " · capa semántica apagada"),
        "",
    ]

    if not ranked:
        lines += ["Nada pasó el filtro esta vez.", ""]

    for i, (job, sc) in enumerate(ranked, 1):
        flag = " 🆕" if job.id in new_ids else ""
        level = seniority_names.get(str(job.seniority_id), "—")
        lines += [
            f"## {i}. [{job.title}]({job.url}){flag}",
            "",
            f"**{sc.total}** · {level} · {_salary(job)} USD · "
            f"{job.applications_count} postulaciones · {_age(job)}",
            "",
        ]
        if sc.matched:
            lines += ["Coincide en: " + ", ".join(sc.matched), ""]
        if sc.penalized:
            lines += ["⚠️ También menciona: " + ", ".join(sc.penalized), ""]
        if explain:
            detail = " · ".join(f"{k} {v}" for k, v in sc.parts.items())
            sim = "—" if sc.semantic is None else f"{sc.semantic:.3f}"
            lines += [f"<sub>{detail} · coseno {sim}</sub>", ""]

    if dropped:
        lines += ["---", "", f"<details><summary>Descartadas ({len(dropped)})</summary>", ""]
        for job, reasons in dropped[:80]:
            lines.append(f"- **{job.title}** — {reasons[0]}")
        if len(dropped) > 80:
            lines.append(f"- …y {len(dropped) - 80} más")
        lines += ["", "</details>", ""]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jobscan", description="Rank Get on Board postings against a profile.")
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("-o", "--output", type=Path, help="write the report here instead of stdout")
    p.add_argument("--new-only", action="store_true", help="only postings never seen before")
    p.add_argument("--no-semantic", action="store_true", help="skip embeddings")
    p.add_argument("--explain", action="store_true", help="show the score breakdown")
    p.add_argument("--limit", type=int, default=25, help="how many to report (0 = all)")
    p.add_argument("--min-score", type=float, default=0.0)
    args = p.parse_args(argv)

    profile = load_profile(args.profile)
    search_conf = profile.get("search", {})

    def progress(query: str, new: int, err: str | None) -> None:
        note = f" [{err}]" if err else ""
        print(f"  {query:<24} +{new}{note}", file=sys.stderr)

    print("Barriendo Get on Board…", file=sys.stderr)
    jobs = api.sweep(
        search_conf.get("queries", []),
        category=search_conf.get("category"),
        remote_only=search_conf.get("remote_only", True),
        max_pages=int(search_conf.get("max_pages_per_query", 3)),
        on_progress=progress,
    )
    print(f"{len(jobs)} avisos únicos\n", file=sys.stderr)

    kept: list[api.Job] = []
    dropped: list[tuple[api.Job, list[str]]] = []
    for job in jobs:
        reasons = knockouts(job, profile)
        if reasons:
            dropped.append((job, reasons))
        else:
            kept.append(job)
    print(f"{len(kept)} pasan el filtro, {len(dropped)} descartados\n", file=sys.stderr)

    embedder: embed.Embedder = embed.NullEmbedder()
    if not args.no_semantic:
        embedder = embed.resolve()
        if isinstance(embedder, embed.NullEmbedder):
            print("Ollama no disponible — solo keywords\n", file=sys.stderr)

    semantic_on = not isinstance(embedder, embed.NullEmbedder)
    model = embed.DEFAULT_MODEL

    with Store(args.db) as store:
        known = store.known_ids()

        profile_vector = (
            embedder.embed(profile.get("identity", {}).get("summary", ""))
            if semantic_on
            else None
        )
        if semantic_on and profile_vector is None:
            # The profile is what everything is compared against; without it the
            # semantic stage has no reference and is simply off.
            print("No se pudo embeber el perfil — solo keywords\n", file=sys.stderr)
            semantic_on = False

        ranked: list[tuple[api.Job, Score]] = []
        for n, job in enumerate(kept, 1):
            vector = None
            if semantic_on:
                vector = store.get_vector(job.id, model)
                if vector is None:
                    vector = embedder.embed(job.text)
                    if vector is not None:
                        store.put_vector(job.id, model, vector)
                if n % 25 == 0:
                    print(f"  embebidos {n}/{len(kept)}", file=sys.stderr)

            sc = score(job, profile, job_vector=vector, profile_vector=profile_vector)
            ranked.append((job, sc))

        ranked.sort(key=lambda pair: pair[1].total, reverse=True)
        new_ids = {job.id for job, _ in ranked if job.id not in known}

        # Recorded before slicing so the store reflects everything actually
        # seen, not just what fit in the report.
        for job, sc in ranked:
            store.record(job.id, job.title, sc.total)
        store.commit()

    shown = [(j, s) for j, s in ranked if s.total >= args.min_score]
    if args.new_only:
        shown = [(j, s) for j, s in shown if j.id in new_ids]
    if args.limit:
        shown = shown[: args.limit]

    report = render(
        shown,
        new_ids=new_ids,
        dropped=dropped,
        seniority_names=_seniority_names(),
        explain=args.explain,
        semantic_on=semantic_on,
    )

    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"\nEscrito en {args.output}", file=sys.stderr)
    else:
        print(report)
    return 0


def _seniority_names() -> dict[str, str]:
    try:
        return api.lookup("seniorities")
    except api.ApiError:
        return {}


if __name__ == "__main__":
    raise SystemExit(main())
