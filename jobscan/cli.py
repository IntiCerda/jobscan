"""Entry point: sweep, filter, rank, report.

    python -m jobscan                    # rank everything, mark new ones
    python -m jobscan --new-only         # only postings unseen until now
    python -m jobscan --no-semantic      # skip embeddings entirely
    python -m jobscan --explain          # show the score breakdown per posting
    python -m jobscan -o hoy.md          # write the report to a file
    python -m jobscan --serve            # the same ranking in the browser

The pipeline itself lives in scan.py; this file only turns a result into
markdown so the web UI can turn the same result into HTML.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .profiles import load_cv
from .scan import DEFAULT_DB, DEFAULT_PROFILE, Result, age_days, load_profile, run


def _age(row: dict) -> str:
    if not row["published_at"]:
        return "sin fecha"
    days = age_days(row["published_at"])
    if days < 1:
        return "publicada hoy"
    if days < 2:
        return "ayer"
    if days < 60:
        return f"hace {days:.0f} días"
    return f"hace {days / 30:.0f} meses"


def _salary(row: dict) -> str:
    lo, hi = row["min_salary"], row["max_salary"]
    if lo and hi:
        return f"${lo:,}–{hi:,}"
    if lo or hi:
        return f"${(hi or lo):,}"
    return "—"


def render(result: Result, shown: list[dict], *, explain: bool) -> str:
    now = datetime.now().astimezone()
    lines = [
        f"# Vacantes — {now:%Y-%m-%d %H:%M}",
        "",
        f"{len(result.jobs)} pasaron el filtro · {len(result.dropped)} descartadas · "
        f"{result.new_count} nuevas desde la última corrida"
        + ("" if result.semantic_on else " · capa semántica apagada"),
        "",
    ]

    if not shown:
        lines += ["Nada pasó el filtro esta vez.", ""]

    for i, row in enumerate(shown, 1):
        flag = " 🆕" if row["is_new"] else ""
        level = result.seniority_names.get(str(row["seniority_id"]), "—")
        lines += [
            f"## {i}. [{row['title']}]({row['url']}){flag}",
            "",
            f"**{row['score']}** · {level} · {_salary(row)} USD · "
            f"{row['applications']} postulaciones · {_age(row)}",
            "",
        ]
        if row["matched"]:
            lines += ["Coincide en: " + ", ".join(row["matched"]), ""]
        if row["penalized"]:
            lines += ["⚠️ También menciona: " + ", ".join(row["penalized"]), ""]
        if explain:
            detail = " · ".join(f"{k} {v}" for k, v in row["parts"].items())
            sim = "—" if row["semantic"] is None else f"{row['semantic']:.3f}"
            lines += [f"<sub>{detail} · coseno {sim}</sub>", ""]

    if result.dropped:
        lines += [
            "---",
            "",
            f"<details><summary>Descartadas ({len(result.dropped)})</summary>",
            "",
        ]
        for d in result.dropped[:80]:
            lines.append(f"- **{d['title']}** — {d['reasons'][0]}")
        if len(result.dropped) > 80:
            lines.append(f"- …y {len(result.dropped) - 80} más")
        lines += ["", "</details>", ""]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="jobscan", description="Rank Get on Board postings against a profile."
    )
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("-o", "--output", type=Path, help="write the report here instead of stdout")
    p.add_argument("--new-only", action="store_true", help="only postings never seen before")
    p.add_argument("--no-semantic", action="store_true", help="skip embeddings")
    p.add_argument("--explain", action="store_true", help="show the score breakdown")
    p.add_argument("--limit", type=int, default=25, help="how many to report (0 = all)")
    p.add_argument("--min-score", type=float, default=0.0)
    p.add_argument("--serve", action="store_true", help="open the web UI instead of printing")
    p.add_argument("--host", default="127.0.0.1", help="interface for --serve")
    p.add_argument("--port", type=int, default=8787, help="port for --serve")
    p.add_argument("--no-open", action="store_true", help="do not launch a browser with --serve")
    args = p.parse_args(argv)

    if args.serve:
        # Imported here so a plain CLI run never pays for the http machinery.
        from . import web

        return web.serve(
            profile_path=args.profile,
            db=args.db,
            host=args.host,
            port=args.port,
            no_semantic=args.no_semantic,
            open_browser=not args.no_open,
        )

    result = run(
        profile=load_profile(args.profile),
        db=args.db,
        cv=load_cv(args.profile),
        no_semantic=args.no_semantic,
        on_progress=lambda line: print(line, file=sys.stderr),
    )

    shown = [r for r in result.jobs if r["score"] >= args.min_score]
    if args.new_only:
        shown = [r for r in shown if r["is_new"]]
    if args.limit:
        shown = shown[: args.limit]

    report = render(result, shown, explain=args.explain)

    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"\nEscrito en {args.output}", file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
