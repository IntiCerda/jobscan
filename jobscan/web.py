"""The local front end: ranking, profile and CV, served from the stdlib.

Design direction — a signals room. The product is a radar that sweeps a job
board and reports signal strength, and the interface commits to that world:
graphite surfaces, a single phosphor-green accent that only ever means signal
(a strong match, a new posting), amber only ever meaning caution, and every
piece of data — scores, salaries, counts, the sweep log — set in monospace
like telemetry, while prose stays in the text face. Depth is borders only, a
few percent of white or black; hierarchy is done with weight and color before
size. One accent, one depth strategy, one density.

Why a browser and not a terminal UI: the browser is already the accessible
surface — screen readers, zoom, keyboard, forced colors all work without this
file asking. Why no JavaScript: filtering is a GET form and progress is a page
that refreshes itself, which every assistive technology understands without a
single aria-live region hoping to be announced.

    python -m jobscan --serve
"""

from __future__ import annotations

import html
import json
import threading
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import embed, profiles, scan

# Orderings the reader can ask for. Freshness and competition are offered
# because they answer questions the total score blurs together: "what appeared
# today" and "where am I not the four hundredth CV in the pile".
SORTS = {
    "score": ("puntaje", lambda r: -r["score"]),
    "new": ("recién publicadas", lambda r: scan.age_days(r["published_at"])),
    "quiet": ("menos postulaciones", lambda r: r["applications"]),
}

PART_LABELS = {
    "stack": "Stack",
    "semantic": "Semántica",
    "competition": "Competencia",
    "freshness": "Frescura",
    "salary": "Sueldo",
    "seniority": "Seniority",
}

# Postings kept open when the page loads, counting from the top group down.
# Sixty-four rows is a long scroll; one collapsed page that hides the best
# posting of the day is worse. Groups open until roughly a screenful of
# ranking is visible and the rest arrive folded.
OPEN_UNTIL = 15


def _by_band(row, _names) -> tuple[int, str]:
    total = row["score"]
    if total >= 25:
        return 0, "señal fuerte · 25+"
    if total >= 20:
        return 1, "señal buena · 20–25"
    if total >= 15:
        return 2, "señal media · 15–20"
    return 3, "señal débil · <15"


def _by_category(row, _names) -> tuple[int, str]:
    return 0, row["category"] or "sin categoría"


def _by_seniority(row, names) -> tuple[int, str]:
    sid = row.get("seniority_id")
    return (sid if sid is not None else 99), names.get(str(sid), "sin nivel")


def _by_freshness(row, _names) -> tuple[int, str]:
    days = scan.age_days(row["published_at"])
    if days < 1:
        return 0, "publicadas hoy"
    if days < 7:
        return 1, "esta semana"
    if days < 30:
        return 2, "este mes"
    return 3, "más viejas"


GROUPS = {
    "band": ("banda de puntaje", _by_band),
    "category": ("categoría", _by_category),
    "seniority": ("seniority", _by_seniority),
    "fresh": ("frescura", _by_freshness),
}


def group_rows(rows: list[dict], axis: str, names: dict) -> list[tuple[str, list[dict]]] | None:
    """Fold rows along one axis, best group first. None means "do not group".

    Groups are ordered by their explicit rank and then by the best posting
    inside them, so folding never buries the top of the ranking under a group
    that happens to sort first alphabetically.
    """
    entry = GROUPS.get(axis)
    if entry is None:
        return None
    _, key_of = entry

    buckets: dict[tuple[int, str], list[dict]] = {}
    for row in rows:
        buckets.setdefault(key_of(row, names), []).append(row)

    return [
        (label, items)
        for (_, label), items in sorted(
            buckets.items(),
            key=lambda kv: (kv[0][0], -max(r["score"] for r in kv[1]), kv[0][1]),
        )
    ]


def group_axes(params: dict) -> tuple[str, str]:
    """The two folding axes in effect, defaulted and de-duplicated.

    Grouping twice along the same axis would produce one subgroup per group
    holding everything — noise dressed as structure — so the second axis
    drops out when it repeats the first.
    """
    axis = params.get("group", "band")
    sub = params.get("sub", "category")
    if axis not in GROUPS:
        axis = "" if "group" in params else "band"
    if sub == axis or sub not in GROUPS:
        sub = ""
    return axis, sub


ICONS_JSON = json.dumps(profiles.ICON_SLUGS, ensure_ascii=False)


def _icon(term: str) -> str:
    """A small monochrome brand icon for a known term, or nothing.

    Served from the Simple Icons CDN in a neutral gray so logos read as quiet
    marks, not a rainbow. Offline or unknown slugs remove themselves.
    """
    slug = profiles.ICON_SLUGS.get(term.lower())
    if not slug:
        return ""
    return (
        f'<img class="ticon" src="https://cdn.simpleicons.org/{slug}/8a94a0" '
        f'alt="" loading="lazy" onerror="this.remove()">'
    )


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def e(value) -> str:
    """Escape for HTML text and attributes.

    Every string on the page comes from a third-party API or a form. A posting
    titled `Dev <script>` has to render as that title, not run as one.
    """
    return html.escape(str(value), quote=True)


def fmt_age(published_at: str | None) -> str:
    if not published_at:
        return "sin fecha"
    days = scan.age_days(published_at)
    if days < 1:
        return "hoy"
    if days < 2:
        return "ayer"
    if days < 60:
        return f"hace {days:.0f} días"
    return f"hace {days / 30:.0f} meses"


def fmt_salary(row: dict) -> str:
    lo, hi = row.get("min_salary"), row.get("max_salary")
    if lo and hi:
        return f"${lo:,}–{hi:,}"
    if lo or hi:
        return f"${(hi or lo):,}"
    return "sin sueldo"


def fmt_when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m %H:%M")
    except (ValueError, TypeError):
        return "—"


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------


def apply_filters(rows: list[dict], params: dict) -> list[dict]:
    """Narrow and order the ranked rows from the query string.

    Every filter is optional and absent means "do not narrow", so a bookmarked
    or hand-edited URL degrades to the full list rather than to an empty page.
    """
    out = list(rows)

    query = (params.get("q") or "").strip().lower()
    if query:
        out = [
            r
            for r in out
            if query in r["title"].lower()
            or any(query in m.lower() for m in r.get("matched", []))
        ]

    raw_min = (params.get("min") or "").strip()
    if raw_min:
        try:
            floor = float(raw_min)
        except ValueError:
            # A junk value in a hand-edited URL should show everything, not
            # nothing, and not a stack trace.
            floor = 0.0
        out = [r for r in out if r["score"] >= floor]

    if params.get("new"):
        out = [r for r in out if r["is_new"]]

    _, key = SORTS.get(params.get("sort") or "score", SORTS["score"])
    out.sort(key=key)
    return out


# --------------------------------------------------------------------------
# the stylesheet
# --------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f5f6f7;
  --surface: #ffffff;
  --surface-2: #eef0f2;
  --ink: #17191d;
  --ink-2: #55606b;
  --ink-3: #68717d;
  --line: rgba(15, 23, 32, 0.13);
  --line-2: rgba(15, 23, 32, 0.07);
  --signal: #0b7a4b;
  --signal-ink: #f2fbf6;
  --signal-soft: #e2f3ea;
  --amber: #7d4a10;
  --amber-bg: #f8ecda;
  --focus: #0b7a4b;
  --lift: 0 1px 2px rgba(15, 23, 32, 0.05), 0 10px 28px -20px rgba(15, 23, 32, 0.35);
  --glow: rgba(255, 255, 255, 0);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0e1114;
    --surface: #14181d;
    --surface-2: #0b0e11;
    --ink: #e7ebee;
    --ink-2: #98a2ad;
    --ink-3: #7d8894;
    --line: rgba(255, 255, 255, 0.13);
    --line-2: rgba(255, 255, 255, 0.07);
    --signal: #3ecf8e;
    --signal-ink: #07130d;
    --signal-soft: #10281c;
    --amber: #e3b077;
    --amber-bg: #2e2210;
    --focus: #3ecf8e;
    --lift: 0 1px 0 rgba(255, 255, 255, 0.03) inset;
    --glow: rgba(255, 255, 255, 0.022);
  }
}

* { box-sizing: border-box; }
html { -webkit-font-smoothing: antialiased; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 400 15px/1.6 "Segoe UI", system-ui, -apple-system, sans-serif;
}
code, .mono, .meta, .score, .rank, .count, .chip, .log, .badge, .stat {
  font-family: "Cascadia Code", "Cascadia Mono", ui-monospace, "SF Mono",
    "JetBrains Mono", Consolas, monospace;
}
a { color: inherit; }
h1, h2, h3 { text-wrap: balance; }
p { text-wrap: pretty; }
:is(a, button, input, select, textarea, summary):focus-visible {
  outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 4px;
}
::selection { background: var(--signal-soft); }

.skip {
  position: absolute; left: -9999px; top: 0; z-index: 99;
  background: var(--surface); color: var(--ink); padding: 8px 14px;
  border: 1px solid var(--line); border-radius: 6px;
}
.skip:focus { left: 8px; top: 8px; }

/* -- app shell ---------------------------------------------------------- */

.app { display: grid; grid-template-columns: 240px minmax(0, 1fr); min-height: 100vh; }
.side {
  border-right: 1px solid var(--line);
  padding: 24px 18px;
  display: flex; flex-direction: column; gap: 4px;
  position: sticky; top: 0; height: 100vh;
}
.brand {
  font-family: "Cascadia Code", ui-monospace, Consolas, monospace;
  font-size: 17px; font-weight: 600; letter-spacing: -0.01em;
  margin: 0 10px 24px; display: flex; align-items: center; gap: 9px;
}
.brand::after {
  content: ""; width: 8px; height: 8px; border-radius: 50%;
  background: var(--signal);
}
.nav-item {
  display: block; padding: 9px 12px; border-radius: 7px;
  color: var(--ink-2); text-decoration: none; font-weight: 500; font-size: 15px;
  transition: background 120ms ease-out, color 120ms ease-out;
}
.nav-item:hover { color: var(--ink); background: var(--line-2); }
.nav-item[aria-current="page"] { color: var(--ink); background: var(--surface-2); font-weight: 600; }
.side-foot { margin-top: auto; padding: 14px 10px 0; border-top: 1px solid var(--line-2); }
.side-foot .stat { font-size: 12px; color: var(--ink-3); margin: 0 0 12px; line-height: 1.7; }
.side-foot .stat strong { color: var(--ink-2); font-weight: 500; }
.sweeping {
  display: flex; align-items: center; gap: 9px;
  font-size: 13.5px; color: var(--signal); text-decoration: none; font-weight: 500;
}
.sweeping::before {
  content: ""; width: 8px; height: 8px; border-radius: 50%;
  background: var(--signal); animation: pulse 1.4s ease-in-out infinite;
}
@keyframes pulse { 50% { opacity: 0.25; } }

/* Centered in the free space beside the sidebar, so a 16:9 monitor gets a
   balanced column instead of content hugging the left edge and a desert on
   the right. */
main { padding: 36px 48px 88px; max-width: 1120px; margin-inline: auto; width: 100%; }

/* -- controls ----------------------------------------------------------- */

button, .btn {
  font: 500 14.5px/1 inherit; font-family: inherit; cursor: pointer;
  padding: 10px 16px; border-radius: 7px;
  border: 1px solid var(--signal); background: var(--signal); color: var(--signal-ink);
  transition: transform 120ms cubic-bezier(0.23, 1, 0.32, 1);
}
button:hover, .btn:hover { filter: brightness(1.07); }
button:active { transform: scale(0.97); }
button.ghost { background: transparent; color: var(--ink-2); border-color: var(--line); }
button.ghost:hover { color: var(--ink); border-color: var(--ink-3); }

input[type=text], input[type=number], select, textarea {
  font: inherit; font-size: 14.5px; color: var(--ink);
  background: var(--surface-2); border: 1px solid var(--line); border-radius: 7px;
  padding: 9px 11px; width: 100%;
}
textarea { line-height: 1.6; resize: vertical; }
input::placeholder, textarea::placeholder { color: var(--ink-3); }

.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field > label, .field > .label {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 11.5px; font-weight: 600; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--ink-3);
}
.check { flex-direction: row; align-items: center; gap: 8px; }
.check label {
  font-family: inherit; font-size: 14.5px; font-weight: 400;
  letter-spacing: 0; text-transform: none; color: var(--ink);
}
.check input { width: 16px; height: 16px; accent-color: var(--signal); }

/* -- radar page --------------------------------------------------------- */

.page-head { margin: 0 0 28px; }
.page-head h1 { font-size: 26px; font-weight: 650; letter-spacing: -0.02em; margin: 0 0 8px; }
.page-head h1::after {
  content: ""; display: block; width: 30px; height: 4px; border-radius: 2px;
  background: var(--signal); margin-top: 10px;
  animation: growx 480ms cubic-bezier(0.23, 1, 0.32, 1) backwards; transform-origin: left;
}
.statline { font-size: 13.5px; color: var(--ink-2); margin: 0; }
.statline b { color: var(--ink); font-weight: 600; }

.toolbar {
  display: grid; grid-template-columns: minmax(220px, 1.6fr) 96px 160px 176px 176px;
  gap: 12px; align-items: end;
  padding: 16px; margin: 0 0 10px;
  background: linear-gradient(180deg, var(--glow), transparent 72px), var(--surface);
  border: 1px solid var(--line); border-radius: 10px;
  box-shadow: var(--lift);
}
.toolbar-foot {
  grid-column: 1 / -1; display: flex; align-items: center; gap: 10px;
  padding-top: 14px; margin-top: 4px; border-top: 1px solid var(--line-2);
}
.toolbar-foot .check { margin-right: auto; }
.btn.ghost {
  background: transparent; color: var(--ink-2); border: 1px solid var(--line);
  padding: 9px 15px; border-radius: 7px; font-size: 14.5px; font-weight: 500;
  text-decoration: none; transition: color 120ms ease-out, border-color 120ms ease-out;
}
.btn.ghost:hover { color: var(--ink); border-color: var(--ink-3); filter: none; }
.showing { font-size: 13px; color: var(--ink-3); margin: 12px 2px 24px; }

details.grp { margin: 0 0 22px; }
details.grp > summary {
  list-style: none; cursor: pointer; user-select: none;
  display: flex; align-items: baseline; gap: 10px;
  padding: 8px 4px 10px;
}
details.grp > summary::-webkit-details-marker { display: none; }
details.grp > summary::before {
  content: "▸"; font-size: 12px; color: var(--ink-3);
  transition: transform 150ms cubic-bezier(0.23, 1, 0.32, 1);
  align-self: center;
}
details.grp[open] > summary::before { transform: rotate(90deg); }
details.grp[open] > ol.jobs, details.grp[open] > details.sub,
details.sub[open] > ol.jobs {
  animation: reveal 220ms cubic-bezier(0.23, 1, 0.32, 1);
}
@keyframes reveal { from { opacity: 0; transform: translateY(-4px); } }
details.grp > summary { border-radius: 7px; transition: color 120ms ease-out; }
details.grp > summary:hover .grp-name { color: var(--signal); }
.grp-name {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 13px; font-weight: 650; letter-spacing: 0.07em;
  text-transform: uppercase; color: var(--ink);
}
details.grp > summary .count { font-size: 13px; color: var(--ink-3); margin-left: auto; }

details.sub { margin: 0 0 12px; }
details.sub > summary {
  list-style: none; cursor: pointer;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px; font-weight: 600; color: var(--ink-2); padding: 8px 4px 6px 2px;
  display: flex; align-items: center; gap: 10px;
  transition: color 120ms ease-out;
}
details.sub > summary::after { content: ""; flex: 1; border-top: 1px solid var(--line-2); }
details.sub > summary:hover { color: var(--ink); }
details.sub > summary::-webkit-details-marker { display: none; }

/* the ledger — rows live on a visible surface so the structure reads at a
   glance instead of floating on the page background */
ol.jobs {
  list-style: none; margin: 0; padding: 0;
  background: linear-gradient(180deg, var(--glow), transparent 72px), var(--surface);
  border: 1px solid var(--line); border-radius: 10px;
  box-shadow: var(--lift);
}
li.job {
  display: grid; grid-template-columns: 40px minmax(0, 1fr) auto;
  gap: 4px 16px; align-items: start;
  padding: 16px 18px 16px 10px;
  border-bottom: 1px solid var(--line-2);
}
li.job:last-child { border-bottom: none; }
li.job:hover { background: var(--line-2); box-shadow: inset 2px 0 0 var(--signal); }
li.job { animation: rise 260ms cubic-bezier(0.23, 1, 0.32, 1) backwards; }
li.job:nth-child(1) { animation-delay: 30ms; }
li.job:nth-child(2) { animation-delay: 60ms; }
li.job:nth-child(3) { animation-delay: 90ms; }
li.job:nth-child(4) { animation-delay: 120ms; }
li.job:nth-child(5) { animation-delay: 150ms; }
li.job:nth-child(6) { animation-delay: 180ms; }
@keyframes rise { from { opacity: 0; transform: translateY(5px); } }
li.job:first-child { border-radius: 10px 10px 0 0; }
li.job:last-child { border-radius: 0 0 10px 10px; }
.rank { font-size: 13px; color: var(--ink-3); padding-top: 5px; text-align: right; }
.job-main { min-width: 0; }
.job-main h2 { font-size: 16px; font-weight: 600; margin: 0; line-height: 1.45; }
.job-main h2 a { text-decoration: none; }
.job-main h2 a:hover { text-decoration: underline; text-underline-offset: 3px; }
.badge {
  display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
  padding: 2.5px 7px; border-radius: 4px; vertical-align: 2px; margin-left: 8px;
  background: var(--signal); color: var(--signal-ink);
}
.chips { margin: 7px 0 0; display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  font-size: 12px; color: var(--ink-2); background: var(--surface-2);
  border: 1px solid var(--line-2); border-radius: 5px; padding: 2px 8px;
}
.chip.hit { color: var(--ink-2); }
.chip.warn { color: var(--amber); background: var(--amber-bg); border-color: transparent; }
.none { font-size: 13px; color: var(--ink-3); }
.job-data { text-align: right; }
.signal-row { display: flex; align-items: center; gap: 9px; justify-content: flex-end; }
.score { font-size: 23px; font-weight: 650; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.score.strong { color: var(--signal); }
.bars { display: inline-flex; gap: 3px; align-items: flex-end; }
.bars i { width: 5px; border-radius: 1.5px; background: var(--line); }
.bars i:nth-child(1) { height: 6px; }  .bars i:nth-child(2) { height: 10px; }
.bars i:nth-child(3) { height: 14px; } .bars i:nth-child(4) { height: 18px; }
.bars i.on { background: var(--signal); }
li.job .meta { font-size: 12.5px; color: var(--ink-3); margin: 5px 0 0; font-variant-numeric: tabular-nums; }
li.job .meta b { color: var(--ink-2); font-weight: 500; }

.empty {
  border: 1px dashed var(--line); border-radius: 10px;
  padding: 56px 24px; text-align: center; color: var(--ink-2); font-size: 15px;
}
.empty h2 { font-size: 18px; color: var(--ink); margin: 0 0 10px; }
.steps { list-style: none; counter-reset: n; padding: 0; margin: 24px auto 28px; max-width: 420px; text-align: left; }
.steps li { counter-increment: n; display: flex; gap: 14px; padding: 9px 0; align-items: baseline; }
.steps li::before {
  content: counter(n, decimal-leading-zero);
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px; color: var(--signal); font-weight: 600;
}

/* -- detail page -------------------------------------------------------- */

.crumb { font-size: 13.5px; margin: 0 0 20px; }
.crumb a { color: var(--ink-2); text-decoration: none; }
.crumb a:hover { color: var(--ink); }
.detail-head h1 { font-size: 23px; font-weight: 650; letter-spacing: -0.015em; margin: 0 0 12px; max-width: 42rem; }
.detail-stats { display: flex; flex-wrap: wrap; gap: 10px 28px; margin: 16px 0 8px; }
.detail-stats .stat { font-size: 12.5px; color: var(--ink-3); }
.detail-stats .stat b { display: block; font-size: 17px; font-weight: 600; color: var(--ink); margin-top: 3px; }
.apply { margin: 20px 0 36px; }
.detail-head + section.panel, .detail-head ~ section.panel { max-width: 800px; }
.btn { text-decoration: none; display: inline-block; }
.panel {
  background: linear-gradient(180deg, var(--glow), transparent 72px), var(--surface);
  border: 1px solid var(--line); border-radius: 10px;
  padding: 24px; margin: 0 0 18px;
  box-shadow: var(--lift);
}
.panel h2 { font-size: 13px; font-weight: 650; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--ink-2); margin: 0 0 16px; font-family: ui-monospace, Consolas, monospace; }
table.parts { border-collapse: collapse; width: 100%; }
table.parts td, table.parts th {
  text-align: left; font-weight: 400; font-size: 14.5px;
  padding: 9px 0; border-bottom: 1px solid var(--line-2);
}
table.parts td.num { text-align: right; font-variant-numeric: tabular-nums; width: 72px; }
table.parts .bar { width: 42%; }
table.parts .bar i {
  display: block; height: 5px; border-radius: 2.5px; background: var(--signal);
  animation: growx 420ms cubic-bezier(0.23, 1, 0.32, 1) backwards; transform-origin: left;
}
@keyframes growx { from { transform: scaleX(0); } }
table.parts tfoot th, table.parts tfoot td { font-weight: 650; border-bottom: none; padding-top: 12px; }

/* -- forms (perfil, cv) ------------------------------------------------- */

.form-grid { display: grid; gap: 18px; max-width: 860px; }
.form-grid > * { animation: rise 300ms cubic-bezier(0.23, 1, 0.32, 1) backwards; }
.form-grid > *:nth-child(1) { animation-delay: 40ms; }
.form-grid > *:nth-child(2) { animation-delay: 90ms; }
.form-grid > *:nth-child(3) { animation-delay: 140ms; }
.form-grid > *:nth-child(4) { animation-delay: 190ms; }
.form-grid > *:nth-child(5) { animation-delay: 240ms; }
.form-grid > *:nth-child(6) { animation-delay: 290ms; }
.form-grid .panel { margin: 0; display: grid; gap: 16px; }
.cols-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.cols-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
.hint { font-size: 13px; color: var(--ink-3); margin: -6px 0 0; }
.checks { display: flex; flex-wrap: wrap; gap: 8px 20px; }
.savebar { display: flex; gap: 14px; align-items: center; margin-top: 22px; }
.saved { font-size: 14.5px; color: var(--signal); font-weight: 500; }
.errors {
  border: 1px solid var(--amber); background: var(--amber-bg); color: var(--amber);
  border-radius: 8px; padding: 14px 18px; margin: 0 0 18px; font-size: 14.5px;
}
.errors ul { margin: 6px 0 0; padding-left: 18px; }

/* -- chips, presets, advanced ------------------------------------------- */

.js-hidden { display: none; }
.chips-box {
  display: flex; flex-wrap: wrap; gap: 7px; align-items: center;
  background: var(--surface-2); border: 1px solid var(--line); border-radius: 8px;
  padding: 9px 10px; min-height: 46px;
}
.chips-box:focus-within { outline: 2px solid var(--focus); outline-offset: 2px; }
.chip-item {
  display: inline-flex; align-items: center; gap: 6px;
  font-family: "Cascadia Code", ui-monospace, Consolas, monospace; font-size: 12.5px;
  background: var(--surface); border: 1px solid var(--line); border-radius: 999px;
  padding: 4px 6px 4px 12px; color: var(--ink);
  animation: rise 180ms cubic-bezier(0.23, 1, 0.32, 1) backwards;
}
.chip-item.key { border-color: var(--signal); color: var(--signal); }
.chip-star, .chip-x {
  border: none; background: transparent; cursor: pointer; padding: 2px 5px;
  font-size: 13px; line-height: 1; color: var(--ink-3); border-radius: 999px;
  transition: color 120ms ease-out, background 120ms ease-out;
}
.chip-star:hover, .chip-x:hover { color: var(--ink); background: var(--line-2); }
.chip-item.key .chip-star { color: var(--signal); }
.chip-x:hover { color: var(--amber); }
.chip-entry {
  flex: 1; min-width: 150px; border: none !important; background: transparent !important;
  padding: 6px 4px !important; font-size: 14px !important; outline: none !important;
}
.chip-add {
  font-size: 12.5px; font-weight: 600; padding: 6px 12px;
  background: transparent; color: var(--ink-2); border: 1px solid var(--line);
}
.chip-add:hover { color: var(--ink); border-color: var(--ink-3); filter: none; }

.presets { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
.preset-card {
  display: grid; gap: 4px; cursor: pointer;
  border: 1px solid var(--line); border-radius: 9px; padding: 13px 14px;
  transition: border-color 120ms ease-out, background 120ms ease-out;
}
.preset-card:hover { border-color: var(--ink-3); }
.preset-card:active { transform: scale(0.98); }
.preset-card { will-change: transform; transition: border-color 120ms ease-out, background 120ms ease-out, transform 120ms cubic-bezier(0.23, 1, 0.32, 1); }
.preset-card strong { font-size: 14.5px; font-weight: 600; }
.preset-card span { font-size: 12.5px; color: var(--ink-3); line-height: 1.45; }
.preset-card input { position: absolute; opacity: 0; pointer-events: none; }
.preset-card:has(input:checked) {
  border-color: var(--signal); background: var(--signal-soft);
}
.preset-card:has(input:checked) strong { color: var(--signal); }
.preset-card:has(input:focus-visible) { outline: 2px solid var(--focus); outline-offset: 2px; }

details.adv { margin: 6px 0 0; max-width: 860px; }
details.adv > summary {
  cursor: pointer; font-size: 14px; font-weight: 500; color: var(--ink-2);
  padding: 12px 16px; border: 1px dashed var(--line); border-radius: 9px;
  list-style: none; transition: color 120ms ease-out, border-color 120ms ease-out;
}
details.adv > summary::-webkit-details-marker { display: none; }
details.adv > summary::before { content: "▸ "; color: var(--ink-3); }
details.adv[open] > summary::before { content: "▾ "; }
details.adv > summary:hover { color: var(--ink); border-color: var(--ink-3); }
details.adv > .panel { margin-top: 14px; }
details.adv[open] > .panel { animation: reveal 220ms cubic-bezier(0.23, 1, 0.32, 1); }

/* -- suggestions & icons ------------------------------------------------ */

.ticon { width: 14px; height: 14px; vertical-align: -2.5px; margin-right: 7px; opacity: 0.9; }
.chip .ticon { width: 12px; height: 12px; margin-right: 5px; vertical-align: -2px; }
.chip-item .ticon { margin-right: 2px; }
.sugs { display: grid; gap: 9px; margin-top: 2px; }
.sug { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.sug-name {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 10.5px; font-weight: 600; letter-spacing: 0.07em;
  text-transform: uppercase; color: var(--ink-3);
  flex-basis: 100%;
}
.sug button {
  display: inline-flex; align-items: center;
  font-family: "Cascadia Code", ui-monospace, Consolas, monospace; font-size: 12px;
  padding: 4.5px 11px; border-radius: 999px; cursor: pointer;
  background: transparent; border: 1px solid var(--line); color: var(--ink-2);
  transition: border-color 120ms ease-out, color 120ms ease-out,
    background 120ms ease-out, transform 120ms cubic-bezier(0.23, 1, 0.32, 1);
}
.sug button::before { content: "+"; margin-right: 6px; color: var(--signal); font-weight: 600; }
.sug button small { margin-left: 5px; color: var(--ink-3); font-size: 10.5px; }
.sug button:hover { border-color: var(--signal); color: var(--ink); filter: none; }
.sug button:active { transform: scale(0.96); }
.sug button.added {
  color: var(--signal); background: var(--signal-soft); border-color: transparent;
  cursor: default;
}
.sug button.added::before { content: "✓"; }

/* -- progress & drops --------------------------------------------------- */

.log {
  background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
  padding: 20px 22px; font-size: 13.5px; line-height: 1.9; color: var(--ink-2);
  white-space: pre-wrap; overflow-x: auto;
}
details.drops { margin-top: 44px; }
details.drops summary { cursor: pointer; font-size: 14px; color: var(--ink-2); font-weight: 500; padding: 6px 0; }
details.drops ul { color: var(--ink-3); font-size: 13.5px; line-height: 1.9; }
details.drops a { color: var(--ink-2); }

@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
@media (max-width: 1280px) {
  .toolbar { grid-template-columns: 1fr 1fr 1fr; }
}
@media (max-width: 900px) {
  .app { grid-template-columns: 1fr; }
  .side { position: static; height: auto; flex-direction: row; align-items: center;
    border-right: none; border-bottom: 1px solid var(--line); padding: 12px 16px; }
  .brand { margin: 0 14px 0 0; }
  .side-foot { margin: 0 0 0 auto; border: none; padding: 0; }
  .side-foot .stat { display: none; }
  main { padding: 24px 16px 64px; }
  .toolbar { grid-template-columns: 1fr 1fr; }
  li.job { grid-template-columns: minmax(0, 1fr) auto; }
  .rank { display: none; }
  .cols-3 { grid-template-columns: 1fr; }
}
"""

# --------------------------------------------------------------------------
# shell
# --------------------------------------------------------------------------

NAV = [("/", "Radar"), ("/perfil", "Perfil"), ("/cv", "CV")]


def shell(
    title: str,
    body: str,
    *,
    active: str = "",
    last_sweep: str | None = None,
    foot_stats: str = "",
    running: bool = False,
    refresh: int = 0,
) -> bytes:
    nav = "".join(
        f'<a class="nav-item" href="{href}"'
        + (' aria-current="page"' if href == active else "")
        + f">{label}</a>"
        for href, label in NAV
    )
    if running:
        action = '<a class="sweeping" href="/progreso">barriendo…</a>'
    else:
        action = (
            '<form method="post" action="/scan">'
            '<button type="submit">Barrer ahora</button></form>'
        )
    stats_line = f"<br>{e(foot_stats)}" if foot_stats else ""
    sweep_line = (
        f'<p class="stat">último barrido<br><strong>{e(last_sweep)}</strong>{stats_line}</p>'
        if last_sweep
        else '<p class="stat">sin barridos todavía</p>'
    )
    head = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} · jobscan</title>
{head}
<style>{CSS}</style>
</head>
<body>
<a class="skip" href="#main">Saltar al contenido</a>
<div class="app">
<nav class="side" aria-label="Secciones">
  <div class="brand">jobscan</div>
  {nav}
  <div class="side-foot">
    {sweep_line}
    {action}
  </div>
</nav>
<main id="main">
{body}
</main>
</div>
</body>
</html>""".encode("utf-8")


# --------------------------------------------------------------------------
# radar page
# --------------------------------------------------------------------------


def _options(choices: list[tuple[str, str]], current: str) -> str:
    return "".join(
        f'<option value="{e(value)}"{" selected" if current == value else ""}>{e(label)}</option>'
        for value, label in choices
    )


def render_filters(params: dict, total: int, showing: int) -> str:
    current = params.get("sort") or "score"
    sort_options = _options([(k, label) for k, (label, _) in SORTS.items()], current)
    axis, sub = group_axes(params)
    axis_options = _options(
        [("", "sin agrupar")] + [(k, label) for k, (label, _) in GROUPS.items()], axis
    )
    sub_options = _options(
        [("", "sin subgrupo")] + [(k, label) for k, (label, _) in GROUPS.items() if k != axis],
        sub,
    )
    return f"""
<form class="toolbar" method="get" action="/" role="search" aria-label="Filtrar vacantes">
  <div class="field">
    <label for="f-q">Buscar</label>
    <input type="text" id="f-q" name="q" value="{e(params.get("q") or "")}"
           placeholder="python, backend…">
  </div>
  <div class="field">
    <label for="f-min">Mínimo</label>
    <input type="number" id="f-min" name="min" value="{e(params.get("min") or "")}" step="1" min="0">
  </div>
  <div class="field">
    <label for="f-sort">Orden</label>
    <select id="f-sort" name="sort">{sort_options}</select>
  </div>
  <div class="field">
    <label for="f-group">Agrupar por</label>
    <select id="f-group" name="group">{axis_options}</select>
  </div>
  <div class="field">
    <label for="f-sub">Y dentro por</label>
    <select id="f-sub" name="sub">{sub_options}</select>
  </div>
  <div class="toolbar-foot">
    <div class="field check">
      <input type="checkbox" id="f-new" name="new" value="1"{" checked" if params.get("new") else ""}>
      <label for="f-new">Solo nuevas</label>
    </div>
    <a class="btn ghost" href="/">Limpiar</a>
    <button type="submit">Aplicar filtros</button>
  </div>
</form>
<p class="showing" role="status">Mostrando {showing} de {total} vacantes que pasaron el filtro.</p>
"""


def render_job_card(row: dict, top: float, seniority: dict, rank: int = 0) -> str:
    level = seniority.get(str(row.get("seniority_id")), "sin nivel")
    # Bars are relative to the best posting of this run — the maximum possible
    # total moves with the weights in profile.toml, so an absolute meter would
    # lie. The number beside them is the accessible value; bars are decoration.
    lit = 0 if top <= 0 else max(0, min(4, round(row["score"] / top * 4)))
    # Green means "señal fuerte", the same 25-point line the band is named
    # after. A relative threshold painted half the page green, and an accent
    # that is everywhere stops meaning anything.
    strong = " strong" if row["score"] >= 25 else ""
    bars = "".join(f'<i class="{"on" if n < lit else ""}"></i>' for n in range(4))
    badge = '<span class="badge">NUEVA</span>' if row["is_new"] else ""
    chips = ""
    if row.get("matched") or row.get("penalized"):
        hit = "".join(f'<span class="chip hit">{_icon(t)}{e(t)}</span>' for t in row.get("matched", []))
        warn = "".join(f'<span class="chip warn">{e(t)}</span>' for t in row.get("penalized", []))
        chips = f'<p class="chips">{hit}{warn}</p>'
    return f"""<li class="job">
  <span class="rank" aria-hidden="true">{rank:02d}</span>
  <div class="job-main">
    <h2><a href="/aviso/{e(urllib.parse.quote(row["id"]))}">{e(row["title"])}</a>{badge}</h2>
    {chips}
  </div>
  <div class="job-data">
    <div class="signal-row"><span class="score{strong}">{row["score"]:.1f}</span>
      <span class="bars" aria-hidden="true">{bars}</span></div>
    <p class="meta"><b>{e(level)}</b> · {e(fmt_salary(row))} · {row["applications"]} post. · {e(fmt_age(row["published_at"]))}</p>
  </div>
</li>"""


def render_cards(rows: list[dict], top: float, names: dict, ranks: dict) -> str:
    return (
        '<ol class="jobs">'
        + "".join(render_job_card(r, top, names, ranks.get(r["id"], 0)) for r in rows)
        + "</ol>"
    )


def render_listing(rows: list[dict], params: dict, top: float, names: dict) -> str:
    """The ranked rows, optionally folded into collapsible groups.

    Groups are plain `<details>`: a native disclosure widget every screen
    reader already announces as expandable, with no state to track and no
    script. The count lives in the summary text rather than in styling alone,
    so folding a group never hides how much it holds.
    """
    if not rows:
        return (
            '<div class="empty"><h2>Ninguna vacante coincide con ese filtro</h2>'
            '<p><a href="/">Ver todas</a></p></div>'
        )

    # Rank is the position in the overall ranking, not in the filtered view:
    # a posting is "number 3" because of its score, however the list is sliced.
    ranked = sorted(rows, key=lambda r: -r["score"])
    ranks = {r["id"]: n for n, r in enumerate(ranked, 1)}

    axis, sub = group_axes(params)
    groups = group_rows(rows, axis, names)
    if groups is None:
        return render_cards(rows, top, names, ranks)

    out, opened = [], 0
    for label, items in groups:
        # A group opens only if it fits inside what is left of the budget, not
        # merely because the budget was not spent yet — checking before adding
        # let a 25-posting group through on the strength of 13 already shown,
        # which is the scroll folding was meant to remove. The first group
        # always opens: a page that loads fully folded hides the best posting
        # of the day behind a click.
        is_open = opened == 0 or opened + len(items) <= OPEN_UNTIL
        opened += len(items)

        inner = render_cards(items, top, names, ranks)
        if sub:
            inner = "".join(
                f'<details class="sub" open><summary>{e(sub_label)} '
                f'<span class="count">({len(sub_items)})</span></summary>'
                f"{render_cards(sub_items, top, names, ranks)}</details>"
                for sub_label, sub_items in (group_rows(items, sub, names) or [])
            )
        out.append(
            f'<details class="grp"{" open" if is_open else ""}>'
            f'<summary><span class="grp-name">{e(label)}</span> '
            f'<span class="count">{len(items)} vacantes</span></summary>{inner}</details>'
        )
    return "".join(out)


def render_index(result: scan.Result | None, params: dict, running: bool) -> bytes:
    if result is None:
        body = """<header class="page-head"><h1>Radar</h1></header>
<div class="empty">
  <h2>Todavía no corriste ningún barrido</h2>
  <p>Tres pasos y queda andando:</p>
  <ol class="steps">
    <li><span>Contá qué buscás en <a href="/perfil">tu perfil</a> — stack, vetos, sueldo objetivo.</span></li>
    <li><span>Pegá <a href="/cv">tu CV</a> para afinar la capa semántica (opcional).</span></li>
    <li><span>Barré Get on Board con el botón de la izquierda.</span></li>
  </ol>
  <form method="post" action="/scan"><button type="submit">Barrer ahora</button></form>
</div>"""
        return shell("Radar", body, active="/", running=running)

    rows = apply_filters(result.jobs, params)
    top = max((r["score"] for r in result.jobs), default=0.0)
    listing = render_listing(rows, params, top, result.seniority_names)

    dropped = ""
    if result.dropped:
        items = "".join(
            f'<li><a href="{e(d["url"])}">{e(d["title"])}</a> — {e(d["reasons"][0])}</li>'
            for d in result.dropped[:120]
        )
        rest = (
            f"<li>…y {len(result.dropped) - 120} más</li>"
            if len(result.dropped) > 120
            else ""
        )
        dropped = f"""<details class="drops">
  <summary>Descartadas antes de puntuar ({len(result.dropped)}) — un filtro que no podés auditar esconde trabajo bueno</summary>
  <ul>{items}{rest}</ul>
</details>"""

    semantic = "" if result.semantic_on else " · <b>capa semántica apagada</b>"
    body = f"""<header class="page-head">
  <h1>Radar</h1>
  <p class="statline">{result.swept} avisos revisados · <b>{len(result.jobs)}</b> pasaron el filtro ·
     <b>{result.new_count}</b> {"nueva" if result.new_count == 1 else "nuevas"} desde la última corrida{semantic}</p>
</header>
{render_filters(params, len(result.jobs), len(rows))}
{listing}
{dropped}"""
    nuevas = f"{result.new_count} " + ("nueva" if result.new_count == 1 else "nuevas")
    return shell(
        "Radar", body, active="/",
        last_sweep=fmt_when(result.finished_at),
        foot_stats=f"{len(result.jobs)} en radar · {nuevas}",
        running=running,
    )


# --------------------------------------------------------------------------
# detail page
# --------------------------------------------------------------------------


def render_detail(result: scan.Result | None, job_id: str) -> tuple[int, bytes]:
    row = next((r for r in (result.jobs if result else []) if r["id"] == job_id), None)
    if row is None:
        return 404, shell(
            "No encontrada",
            '<h1>Esa vacante no está en el último barrido</h1>'
            '<p><a href="/">Volver al radar</a></p>',
            active="/",
        )

    level = result.seniority_names.get(str(row.get("seniority_id")), "sin nivel")
    peak = max((abs(v) for v in row["parts"].values()), default=0.0)
    parts = "".join(
        f'<tr><th scope="row">{e(PART_LABELS.get(k, k))}</th>'
        f'<td class="bar"><i style="width:{0 if peak <= 0 else max(2, round(abs(v) / peak * 100))}%"></i></td>'
        f'<td class="num">{v:.2f}</td></tr>'
        for k, v in row["parts"].items()
    )
    sim = "no calculada" if row["semantic"] is None else f"{row['semantic']:.3f}"
    matched = "".join(f'<span class="chip hit">{_icon(t)}{e(t)}</span>' for t in row["matched"]) or (
        '<span class="none">ninguno</span>'
    )
    penalized = "".join(f'<span class="chip warn">{e(t)}</span>' for t in row["penalized"]) or (
        '<span class="none">ninguna</span>'
    )

    body = f"""<p class="crumb"><a href="/">← Radar</a></p>
<header class="detail-head">
  <h1>{e(row["title"])}{'<span class="badge">NUEVA</span>' if row["is_new"] else ""}</h1>
  <div class="detail-stats">
    <p class="stat">puntaje<b>{row["score"]:.2f}</b></p>
    <p class="stat">seniority<b>{e(level)}</b></p>
    <p class="stat">sueldo<b>{e(fmt_salary(row))}</b></p>
    <p class="stat">postulaciones<b>{row["applications"]}</b></p>
    <p class="stat">publicada<b>{e(fmt_age(row["published_at"]))}</b></p>
    <p class="stat">categoría<b>{e(row["category"] or "—")}</b></p>
  </div>
  <p class="apply"><a class="btn" href="{e(row["url"])}" rel="noopener">Abrir el aviso en Get on Board ↗</a></p>
</header>
<section class="panel">
  <h2>De dónde sale el puntaje</h2>
  <table class="parts">
    <tbody>{parts}</tbody>
    <tfoot><tr><th scope="row">Total</th><td></td><td class="num">{row["score"]:.2f}</td></tr></tfoot>
  </table>
  <p class="hint">Cada señal ya multiplicada por su peso en tu perfil. Coseno contra tu perfil: {e(sim)}.</p>
</section>
<section class="panel">
  <h2>Coincidencias</h2>
  <p class="chips">{matched}</p>
  <p class="hint">Tecnologías ajenas mencionadas — penalizan sin descartar:</p>
  <p class="chips">{penalized}</p>
</section>"""
    return 200, shell(
        row["title"], body, active="/", last_sweep=fmt_when(result.finished_at)
    )


# --------------------------------------------------------------------------
# progress page
# --------------------------------------------------------------------------


def render_progress(state: "ScanState") -> bytes:
    lines = state.snapshot()
    log = "\n".join(lines) or "Arrancando…"
    if state.running:
        note = '<p class="statline">Esta página se actualiza sola cada 2 segundos.</p>'
        tail = ""
    elif not lines:
        # Reachable by typing the URL. Saying "listo" here would report a scan
        # that never happened as one that finished.
        note = '<p class="statline">No hay ningún barrido corriendo.</p>'
        tail = '<p><a href="/">Volver al radar</a></p>'
        log = "Nada que mostrar todavía."
    elif state.error:
        note = '<p class="statline"><b>El barrido falló.</b></p>'
        tail = '<p><a href="/">Volver al radar</a></p>'
        log = f"{log}\n\n{state.error}"
    else:
        note = '<p class="statline">Listo.</p>'
        tail = '<p class="apply"><a class="btn" href="/">Ver los resultados</a></p>'
    body = f"""<header class="page-head"><h1>Barriendo Get on Board</h1>{note}</header>
<pre class="log" role="log" aria-label="Progreso del barrido">{e(log)}</pre>
{tail}"""
    return shell(
        "Barriendo", body, active="/", running=state.running,
        refresh=2 if state.running else 0,
    )


# --------------------------------------------------------------------------
# profile page
# --------------------------------------------------------------------------


def _textarea(name: str, label: str, value: str, *, rows: int = 4, hint: str = "") -> str:
    hint_html = f'<p class="hint">{hint}</p>' if hint else ""
    return f"""<div class="field">
  <label for="p-{name}">{e(label)}</label>
  <textarea id="p-{name}" name="{name}" rows="{rows}">{e(value)}</textarea>
  {hint_html}</div>"""


def _number(name: str, label: str, value, *, step: str = "0.5") -> str:
    return f"""<div class="field">
  <label for="p-{name}">{e(label)}</label>
  <input type="number" id="p-{name}" name="{name}" value="{e(value)}" step="{step}">
</div>"""


def _chipfield(
    name: str,
    label: str,
    lines: list[str],
    *,
    hint: str = "",
    starred: bool = False,
    rows: int = 4,
) -> str:
    """A list field: a plain textarea that the page script upgrades to chips.

    The textarea keeps the form's name, so the server reads the same field
    with or without JavaScript — the chips are presentation, never the data.
    """
    hint_html = f'<p class="hint">{e(hint)}</p>' if hint else ""
    star_attr = ' data-starred="1"' if starred else ""
    return f"""<div class="field chipfield"{star_attr}>
  <label for="p-{name}">{e(label)}</label>
  <textarea id="p-{name}" name="{name}" rows="{rows}" data-chips>{e(chr(10).join(lines))}</textarea>
  {hint_html}</div>"""


# Upgrades every [data-chips] textarea into a chip editor: type, Enter (or
# coma, or the button) adds a bubble; the x removes it; on starred fields the
# star marks a term as key (weight 4 instead of 2). The textarea stays the
# source of truth and is rewritten on every change, so submitting works
# identically with the script, without it, or halfway through a repaint.
CHIPS_JS = r"""
function iconFor(term) {
  var slug = TERM_ICONS[term.toLowerCase()];
  if (!slug) return null;
  var img = document.createElement('img');
  img.className = 'ticon';
  img.src = 'https://cdn.simpleicons.org/' + slug + '/8a94a0';
  img.alt = '';
  img.loading = 'lazy';
  img.addEventListener('error', function () { img.remove(); });
  return img;
}

var FIELDS = {};

function refreshSuggestions() {
  document.querySelectorAll('button[data-add]').forEach(function (btn) {
    var field = FIELDS[btn.getAttribute('data-target')];
    if (!field) return;
    var all = btn.getAttribute('data-add').split('|').every(field.has);
    btn.classList.toggle('added', all);
    btn.disabled = all;
  });
}

document.querySelectorAll('textarea[data-chips]').forEach(function (area) {
  var starred = area.closest('.chipfield').hasAttribute('data-starred');
  var label = document.querySelector('label[for="' + area.id + '"]');

  function parse(line) {
    var m = line.split('=');
    var term = m[0].trim().replace(/^"|"$/g, '');
    var w = m.length > 1 ? parseFloat(m[1].replace(',', '.')) : NaN;
    return { term: term, weight: isNaN(w) ? 2 : w };
  }
  var items = area.value.split('\n').filter(function (l) { return l.trim(); }).map(parse);

  var box = document.createElement('div');
  box.className = 'chips-box';
  var entry = document.createElement('input');
  entry.type = 'text';
  entry.className = 'chip-entry';
  entry.placeholder = 'escribí y Enter…';
  if (label) entry.setAttribute('aria-label', 'Agregar a: ' + label.textContent);
  var add = document.createElement('button');
  add.type = 'button';
  add.className = 'chip-add';
  add.textContent = 'Agregar';

  function has(term) {
    return items.some(function (it) { return it.term.toLowerCase() === term.toLowerCase(); });
  }
  function sync() {
    area.value = items.map(function (it) {
      return starred ? it.term + ' = ' + it.weight : it.term;
    }).join('\n');
    refreshSuggestions();
  }
  function render() {
    box.querySelectorAll('.chip-item').forEach(function (n) { n.remove(); });
    items.forEach(function (it, i) {
      var chip = document.createElement('span');
      chip.className = 'chip-item' + (starred && it.weight >= 3 ? ' key' : '');
      var icon = iconFor(it.term);
      if (icon) chip.appendChild(icon);
      var txt = document.createElement('span');
      txt.textContent = it.term;
      chip.appendChild(txt);
      if (starred) {
        var star = document.createElement('button');
        star.type = 'button';
        star.className = 'chip-star';
        star.textContent = '★';
        star.title = 'Marcar como clave (vale más puntos)';
        star.setAttribute('aria-label', (it.weight >= 3 ? 'Quitar clave a ' : 'Marcar clave ') + it.term);
        star.setAttribute('aria-pressed', it.weight >= 3 ? 'true' : 'false');
        star.addEventListener('click', function () {
          it.weight = it.weight >= 3 ? 2 : 4;
          sync(); render();
        });
        chip.appendChild(star);
      }
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'chip-x';
      x.textContent = '×';
      x.setAttribute('aria-label', 'Quitar ' + it.term);
      x.addEventListener('click', function () {
        items.splice(i, 1);
        sync(); render();
      });
      chip.appendChild(x);
      box.insertBefore(chip, entry);
    });
  }
  function push(term) {
    if (term && !has(term)) items.push({ term: term, weight: 2 });
  }
  function commit() {
    var raw = entry.value.trim().replace(/,$/, '');
    if (!raw) return;
    raw.split(',').map(function (t) { return t.trim(); }).filter(Boolean).forEach(push);
    entry.value = '';
    sync(); render();
  }
  entry.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' || ev.key === ',') { ev.preventDefault(); commit(); }
    else if (ev.key === 'Backspace' && !entry.value && items.length) {
      items.pop(); sync(); render();
    }
  });
  add.addEventListener('click', commit);
  entry.addEventListener('blur', commit);

  box.appendChild(entry);
  box.appendChild(add);
  area.classList.add('js-hidden');
  area.setAttribute('aria-hidden', 'true');
  area.tabIndex = -1;
  area.insertAdjacentElement('afterend', box);

  FIELDS[area.name] = {
    has: has,
    add: function (term) { push(term); sync(); render(); },
  };
  render();
});

document.querySelectorAll('button[data-add]').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var field = FIELDS[btn.getAttribute('data-target')];
    if (!field) return;
    btn.getAttribute('data-add').split('|').forEach(field.add);
  });
});
refreshSuggestions();
"""


def _suggestions(target: str, groups: dict[str, list[str]], *, packs: bool = False) -> str:
    """Rows of one-click additions for a chip field.

    `packs` renders one button per group (the whole family at once); otherwise
    one button per term. Clicking beats typing: recognizing a term you use is
    one decision, remembering the list is a working-memory tax.
    """
    rows = []
    for name, terms in groups.items():
        if packs:
            joined = "|".join(terms)
            buttons = (
                f'<button type="button" data-target="{e(target)}" data-add="{e(joined)}">'
                f"{e(name)} <small>({len(terms)})</small></button>"
            )
        else:
            buttons = "".join(
                f'<button type="button" data-target="{e(target)}" data-add="{e(t)}">'
                f"{_icon(t)}{e(t)}</button>"
                for t in terms
            )
            buttons = f'<span class="sug-name">{e(name)}</span>{buttons}'
        rows.append(f'<div class="sug">{buttons}</div>')
    return '<div class="sugs">' + "".join(rows) + "</div>"


def render_profile_page(
    profile: dict,
    seniority_names: dict[str, str],
    *,
    errors: list[str] | None = None,
    saved: bool = False,
    last_sweep: str | None = None,
    local_models: list[str] | None = None,
) -> bytes:
    identity = profile.get("identity", {})
    search = profile.get("search", {})
    filters = profile.get("filters", {})
    scoring = profile.get("scoring", {})
    stack = profile.get("fit", {}).get("stack", {})
    seniority = profile.get("seniority", {})

    stack_lines = [f"{term} = {weight}" for term, weight in stack.items()]
    current_preset = profiles.detect_preset(scoring) if scoring else "equilibrado"

    # What this machine actually has installed leads the suggestions: it is
    # the one group guaranteed to be true for this person.
    stack_suggestions: dict[str, list[str]] = {}
    if local_models:
        stack_suggestions["Tus modelos de Ollama"] = list(local_models)
    stack_suggestions.update(profiles.STACK_SUGGESTIONS)

    preset_cards = "".join(
        f"""<label class="preset-card" for="pr-{key}">
  <input type="radio" id="pr-{key}" name="preset" value="{key}"{
            " checked" if current_preset == key else ""}>
  <strong>{e(title)}</strong><span>{e(desc)}</span>
</label>"""
        for key, (title, desc) in profiles.PRESET_LABELS.items()
    ) + f"""<label class="preset-card" for="pr-custom">
  <input type="radio" id="pr-custom" name="preset" value="custom"{
        " checked" if current_preset == "custom" else ""}>
  <strong>Personalizado</strong><span>los números finos, en ajustes avanzados</span>
</label>"""

    flag_checks = "".join(
        f"""<div class="field check">
  <input type="checkbox" id="fl-{e(flag)}" name="exclude_flags" value="{e(flag)}"{
            " checked" if flag in set(filters.get("exclude_flags", [])) else ""}>
  <label for="fl-{e(flag)}">{e(flag)} — {e(meaning)}</label></div>"""
        for flag, meaning in profiles.KNOWN_FLAGS.items()
    )

    def seniority_checks(field_name: str, chosen: list) -> str:
        chosen_set = set(chosen)
        # Sorted by the id, which the API defines junior-to-expert: a scale
        # the eye can walk, instead of whatever order the dict arrived in.
        ordered = sorted(seniority_names.items(), key=lambda kv: int(kv[0]))
        return "".join(
            f"""<div class="field check">
  <input type="checkbox" id="{field_name}-{sid}" name="{field_name}" value="{sid}"{
                " checked" if int(sid) in chosen_set else ""}>
  <label for="{field_name}-{sid}">{e(name)}</label></div>"""
            for sid, name in ordered
        )

    error_block = ""
    if errors:
        items = "".join(f"<li>{e(err)}</li>" for err in errors)
        error_block = (
            f'<div class="errors" role="alert"><b>No guardé nada</b> — primero esto:'
            f"<ul>{items}</ul></div>"
        )
    saved_note = '<span class="saved" role="status">Guardado ✓</span>' if saved else ""

    body = f"""<header class="page-head">
  <h1>Perfil</h1>
  <p class="statline">Cuatro pasos y listo. Lo fino vive plegado en ajustes avanzados,
     con valores que ya funcionan.</p>
</header>
{error_block}
<form method="post" action="/perfil" class="form-grid">
  <section class="panel">
    <h2>1 · Quién sos</h2>
    {_textarea("summary", "Contá qué hacés, en tus palabras", identity.get("summary", "").strip(), rows=5,
               hint="Como se lo contarías a otro dev. Cada aviso se compara contra este texto (y contra tu CV, si lo cargás).")}
  </section>
  <section class="panel">
    <h2>2 · Tu stack</h2>
    {_chipfield("stack", "Tecnologías que sumen puntos", stack_lines, starred=True, rows=8,
                hint="Escribí una y Enter, o tocá una sugerencia. La ★ marca las claves — valen el doble.")}
    {_suggestions("stack", stack_suggestions)}
  </section>
  <section class="panel">
    <h2>3 · Lo que no querés ni ver</h2>
    {_chipfield("exclude_in_title", "Tecnologías vetadas", filters.get("exclude_in_title", []), rows=5,
                hint="Si aparecen en el título del aviso, se descarta solo. Un pack agrega la familia entera.")}
    {_suggestions("exclude_in_title", profiles.VETO_PACKS, packs=True)}
  </section>
  <section class="panel">
    <h2>4 · Qué te importa más</h2>
    <div class="field"><span class="label">Elegí un modo de ranking</span>
      <div class="presets">{preset_cards}</div></div>
    {_number("target_salary_usd", "Sueldo al que apuntás (USD/mes)", scoring.get("target_salary_usd", 0), step="100")}
    <div class="field"><span class="label">Tu nivel — puntaje completo</span>
      <div class="checks">{seniority_checks("seniority_fit", seniority.get("fit", []))}</div></div>
    <div class="field"><span class="label">Niveles a los que aspirás — puntaje parcial</span>
      <div class="checks">{seniority_checks("seniority_reach", seniority.get("reach", []))}</div></div>
  </section>

  <details class="adv">
    <summary>Ajustes avanzados — no hace falta tocarlos</summary>
    <section class="panel">
      <h2>Búsqueda</h2>
      {_chipfield("queries", "Términos con los que se barre Get on Board", search.get("queries", []), rows=8,
                  hint="Cada uno es un ángulo de búsqueda; los resultados se unen. Sin al menos uno, la API no devuelve nada.")}
      <div class="cols-3">
        <div class="field"><label for="p-category">Categoría de la búsqueda</label>
          <input type="text" id="p-category" name="category" value="{e(search.get("category", "programming"))}"></div>
        {_number("max_pages", "Páginas por búsqueda", search.get("max_pages_per_query", 3), step="1")}
        <div class="field check" style="align-self:end">
          <input type="checkbox" id="p-remote" name="remote_only" value="1"{" checked" if search.get("remote_only", True) else ""}>
          <label for="p-remote">Solo remotas</label></div>
      </div>
    </section>
    <section class="panel">
      <h2>Filtros finos</h2>
      {_chipfield("penalize_in_body", "Restan puntos si aparecen en el cuerpo", filters.get("penalize_in_body", []), rows=3,
                  hint="No descartan: un aviso puede nombrar Java en los deseables y seguir siendo bueno.")}
      {_chipfield("allowed_categories", "Categorías de aviso aceptadas", filters.get("allowed_categories", []), rows=3,
                  hint="Nombres exactos de /api/v0/categories.")}
      <div class="field"><label for="p-langs">Idiomas excluidos (separados por coma)</label>
        <input type="text" id="p-langs" name="exclude_langs" value="{e(", ".join(filters.get("exclude_langs", [])))}"></div>
      <div class="field"><span class="label">Banderas de calidad de Get on Board que descartan</span>
        <div class="checks">{flag_checks}</div></div>
    </section>
    <section class="panel">
      <h2>Pesos a mano</h2>
      <p class="hint">Solo cuentan si elegiste «Personalizado» arriba.</p>
      <div class="cols-3">
        {_number("weight_stack", "Stack", scoring.get("weight_stack", 14.0))}
        {_number("weight_semantic", "Semántica", scoring.get("weight_semantic", 12.0))}
        {_number("weight_competition", "Competencia", scoring.get("weight_competition", 8.0))}
        {_number("weight_freshness", "Frescura", scoring.get("weight_freshness", 5.0))}
        {_number("weight_salary", "Sueldo", scoring.get("weight_salary", 3.0))}
        {_number("weight_seniority", "Seniority", scoring.get("weight_seniority", 4.0))}
      </div>
      <div class="cols-3">
        {_number("stack_half_point", "Saturación del stack", scoring.get("stack_half_point", 12.0))}
        {_number("good_applications_count", "Postulaciones aceptables", scoring.get("good_applications_count", 100), step="10")}
        {_number("stale_after_days", "Vieja a los (días)", scoring.get("stale_after_days", 45.0), step="5")}
      </div>
    </section>
  </details>

  <div class="savebar">
    <button type="submit">Guardar perfil</button>
    {saved_note}
  </div>
</form>
<script>var TERM_ICONS = {ICONS_JSON};{CHIPS_JS}</script>"""
    return shell("Perfil", body, active="/perfil", last_sweep=last_sweep)


# --------------------------------------------------------------------------
# cv page
# --------------------------------------------------------------------------


def render_cv_page(
    cv_text: str, *, semantic_ready: bool, saved: bool = False,
    last_sweep: str | None = None,
) -> bytes:
    if semantic_ready:
        status = '<p class="statline">Ollama está corriendo — el CV entra en el próximo barrido.</p>'
    else:
        status = (
            '<p class="statline"><b>Ollama no está corriendo</b> — el CV se guarda igual, '
            "pero la capa semántica que lo usa está apagada hasta que lo levantes.</p>"
        )
    saved_note = '<span class="saved" role="status">Guardado ✓</span>' if saved else ""
    body = f"""<header class="page-head">
  <h1>CV</h1>
  <p class="statline">Pegalo como texto plano. Se embebe junto a tu resumen y cada aviso se compara
     contra los dos: el resumen dice qué querés, el CV dice qué hiciste.</p>
</header>
{status}
<form method="post" action="/cv" class="form-grid">
  <section class="panel">
    {_textarea("cv", "Tu CV en texto plano", cv_text, rows=22,
               hint="Queda en cv.txt al lado de profile.toml, fuera del repo. Dejarlo vacío lo borra.")}
  </section>
  <div class="savebar">
    <button type="submit">Guardar CV</button>
    {saved_note}
  </div>
</form>"""
    return shell("CV", body, active="/cv", last_sweep=last_sweep)


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------


class ScanState:
    """One scan at a time, and what it has printed so far.

    Guarded by a lock because the server is threaded: two clicks on "barrer"
    arriving together must not start two sweeps against the same SQLite file.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.error: str | None = None
        self._lines: list[str] = []

    def start(self, work) -> bool:
        """True if this call started the scan, False if one was already running."""
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.error = None
            self._lines = []
        threading.Thread(target=self._run, args=(work,), daemon=True).start()
        return True

    def _run(self, work) -> None:
        try:
            work(self.say)
        except Exception:
            # A sweep that dies takes the log with it to the page instead of to
            # a terminal the reader is not looking at.
            self.error = traceback.format_exc(limit=4)
            self.say("El barrido se cortó.")
        finally:
            with self.lock:
                self.running = False

    def say(self, line: str) -> None:
        with self.lock:
            self._lines.append(line)

    def snapshot(self) -> list[str]:
        with self.lock:
            return list(self._lines)


def _seniority_names(result: scan.Result | None) -> dict[str, str]:
    """Names for the seniority checkboxes, cheapest source first."""
    if result and result.seniority_names:
        return result.seniority_names
    try:
        from . import api

        return api.lookup("seniorities") or profiles.SENIORITY_FALLBACK
    except Exception:
        return profiles.SENIORITY_FALLBACK


def make_handler(*, profile_path: Path, db: Path, no_semantic: bool):
    state = ScanState()

    class Handler(BaseHTTPRequestHandler):
        server_version = "jobscan"

        def log_message(self, fmt, *args) -> None:
            pass  # a line per request is noise, not information

        # -- plumbing ----------------------------------------------------

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, where: str) -> None:
            self.send_response(303)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _form(self) -> dict[str, list[str]]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            return urllib.parse.parse_qs(raw, keep_blank_values=True)

        # -- routes ------------------------------------------------------

        def do_GET(self) -> None:
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                params = {
                    k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if v
                }

                if path == "/":
                    self._send(200, render_index(scan.last(db), params, state.running))
                elif path == "/progreso":
                    self._send(200, render_progress(state))
                elif path == "/perfil":
                    profile = scan.load_profile(profile_path)
                    last = scan.last(db)
                    self._send(200, render_profile_page(
                        profile, _seniority_names(last), saved="ok" in params,
                        last_sweep=fmt_when(last.finished_at) if last else None,
                        local_models=[] if no_semantic else embed.list_models(),
                    ))
                elif path == "/cv":
                    ready = not no_semantic and not isinstance(
                        embed.resolve(), embed.NullEmbedder
                    )
                    last = scan.last(db)
                    self._send(200, render_cv_page(
                        profiles.load_cv(profile_path),
                        semantic_ready=ready, saved="ok" in params,
                        last_sweep=fmt_when(last.finished_at) if last else None,
                    ))
                elif path.startswith("/aviso/"):
                    status, body = render_detail(
                        scan.last(db), urllib.parse.unquote(path[len("/aviso/"):])
                    )
                    self._send(status, body)
                else:
                    self._send(404, shell(
                        "No existe",
                        '<h1>Esa página no existe</h1><p><a href="/">Volver al radar</a></p>',
                    ))
            except Exception:
                self._send(500, shell(
                    "Error",
                    "<h1>Algo se rompió</h1>"
                    f'<pre class="log">{e(traceback.format_exc(limit=4))}</pre>'
                    '<p><a href="/">Volver al radar</a></p>',
                ))

        def do_POST(self) -> None:
            try:
                path = urllib.parse.urlparse(self.path).path.rstrip("/")
                if path == "/scan":
                    self._form()  # drain the body or the browser reports a reset
                    profile = scan.load_profile(profile_path)
                    cv = profiles.load_cv(profile_path)
                    state.start(
                        lambda say: scan.run(
                            profile=profile, db=db, cv=cv,
                            no_semantic=no_semantic, on_progress=say,
                        )
                    )
                    self._redirect("/progreso")
                elif path == "/perfil":
                    form = self._form()
                    profile, errors = profiles.from_form(form)
                    if errors:
                        self._send(200, render_profile_page(
                            profile, _seniority_names(scan.last(db)), errors=errors,
                        ))
                    else:
                        profiles.save(profile, profile_path)
                        self._redirect("/perfil?ok=1")
                elif path == "/cv":
                    form = self._form()
                    profiles.save_cv(profile_path, (form.get("cv") or [""])[0])
                    self._redirect("/cv?ok=1")
                else:
                    self._redirect("/")
            except Exception:
                self._send(500, shell(
                    "Error",
                    "<h1>Algo se rompió</h1>"
                    f'<pre class="log">{e(traceback.format_exc(limit=4))}</pre>'
                    '<p><a href="/">Volver al radar</a></p>',
                ))

    return Handler


def serve(
    *,
    profile_path: Path,
    db: Path,
    host: str = "127.0.0.1",
    port: int = 8787,
    no_semantic: bool = False,
    open_browser: bool = True,
) -> int:
    handler = make_handler(profile_path=profile_path, db=db, no_semantic=no_semantic)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{httpd.server_port}"
    print(f"jobscan en {url}   (Ctrl+C para salir)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nchau")
    finally:
        httpd.server_close()
    return 0
