"""A local web UI over the same scan the CLI reports.

Why a browser and not a terminal UI: the browser is already the accessible
surface. Screen readers, 200% zoom, keyboard navigation, forced colours and the
reader's own font size all work without this file asking for them. A curses
layer would have to reimplement each one, badly.

Why no JavaScript: filtering is a GET form and scan progress is a page that
refreshes itself. Both are understood by every assistive technology without a
single aria-live region hoping to be announced. It is also the smaller program.

Only the standard library is used, in keeping with the rest of the tool.

    python -m jobscan --serve
"""

from __future__ import annotations

import html
import threading
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import scan

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


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def e(value) -> str:
    """Escape for HTML text and attributes.

    Every string on the page comes from a third-party API. A posting titled
    `Dev <script>` has to render as that title, not run as one.
    """
    return html.escape(str(value), quote=True)


def fmt_age(published_at: str | None) -> str:
    if not published_at:
        return "sin fecha"
    days = scan.age_days(published_at)
    if days < 1:
        return "publicada hoy"
    if days < 2:
        return "ayer"
    if days < 60:
        return f"hace {days:.0f} días"
    return f"hace {days / 30:.0f} meses"


def fmt_salary(row: dict) -> str:
    lo, hi = row.get("min_salary"), row.get("max_salary")
    if lo and hi:
        return f"US${lo:,}-{hi:,}"
    if lo or hi:
        return f"US${(hi or lo):,}"
    return "no publicado"


def fmt_when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m/%Y %H:%M")
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
# pages
# --------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --panel: #ffffff; --ink: #14161a; --muted: #545a63;
  --line: #d7dae0; --accent: #1f4fd8; --accent-ink: #ffffff;
  --new: #0a5530; --new-bg: #d8f2e4; --warn: #6f3705; --warn-bg: #fcecd6;
  --bar: #ccd6f4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a; --panel: #1c1f25; --ink: #eceef1; --muted: #a7aeb9;
    --line: #333842; --accent: #9db8ff; --accent-ink: #10131a;
    --new: #90e2b8; --new-bg: #10321f; --warn: #f3c68f; --warn-bg: #3a2a12;
    --bar: #2f3d63;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 1rem/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 56rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
a { color: var(--accent); }
a:focus-visible, button:focus-visible, input:focus-visible,
select:focus-visible, summary:focus-visible {
  outline: 3px solid var(--accent); outline-offset: 2px; border-radius: 3px;
}
.skip {
  position: absolute; left: -9999px; top: 0; z-index: 9;
  background: var(--panel); color: var(--ink); padding: .6rem 1rem;
  border: 1px solid var(--line); border-radius: 6px;
}
.skip:focus { left: .5rem; top: .5rem; }
header h1 { font-size: 1.55rem; margin: 0 0 .3rem; line-height: 1.25; }
.sub { color: var(--muted); margin: 0 0 1rem; }
.bar {
  display: flex; flex-wrap: wrap; gap: .8rem; align-items: flex-end;
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 1rem; margin: 1.25rem 0 1.25rem;
}
.field { display: flex; flex-direction: column; gap: .25rem; }
.field label { font-size: .8rem; color: var(--muted); font-weight: 600; }
input[type=text], input[type=number], select {
  font: inherit; padding: .45rem .6rem; background: var(--bg);
  color: var(--ink); border: 1px solid var(--line); border-radius: 6px;
}
.check { flex-direction: row; align-items: center; gap: .4rem; padding-bottom: .45rem; }
.check label { font-size: 1rem; color: var(--ink); font-weight: 400; }
.check input { width: 1.1rem; height: 1.1rem; }
button {
  font: inherit; font-weight: 600; cursor: pointer; padding: .5rem 1rem;
  border-radius: 6px; border: 1px solid var(--accent);
  background: var(--accent); color: var(--accent-ink);
}
ol.jobs { list-style: none; margin: 0; padding: 0; display: grid; gap: .85rem; }
.job {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 1rem 1.1rem;
}
.job h2 { font-size: 1.08rem; margin: 0 0 .35rem; line-height: 1.35; }
.score { font-variant-numeric: tabular-nums; font-weight: 700; color: var(--ink); }
.meter { height: 5px; border-radius: 3px; background: var(--bar); margin: .6rem 0; }
.meter i { display: block; height: 100%; border-radius: 3px; background: var(--accent); }
.meta { color: var(--muted); font-size: .9rem; margin: 0; }
.meta span + span::before { content: " · "; }
.tag {
  display: inline-block; font-size: .75rem; font-weight: 700; letter-spacing: .03em;
  padding: .1rem .45rem; border-radius: 4px; vertical-align: .12em;
}
.tag.new { background: var(--new-bg); color: var(--new); }
.tag.warn { background: var(--warn-bg); color: var(--warn); }
.terms { font-size: .88rem; margin: .5rem 0 0; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1.5rem; }
caption { text-align: left; padding-bottom: .5rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--line); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tfoot th, tfoot td { font-weight: 700; border-bottom: none; }
details { margin-top: 2rem; }
summary { cursor: pointer; font-weight: 600; padding: .5rem 0; }
details ul { color: var(--muted); }
.empty {
  background: var(--panel); border: 1px dashed var(--line);
  border-radius: 10px; padding: 2.5rem 1rem; text-align: center; color: var(--muted);
}
pre.log {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 1rem; overflow-x: auto; white-space: pre-wrap; font-size: .9rem;
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


def page(title: str, body: str, *, refresh: int = 0) -> bytes:
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
<div class="wrap">
{body}
</div>
</body>
</html>""".encode("utf-8")


def render_filters(params: dict, total: int, showing: int) -> str:
    current = params.get("sort") or "score"
    options = "".join(
        f'<option value="{k}"{" selected" if current == k else ""}>{e(label)}</option>'
        for k, (label, _) in SORTS.items()
    )
    return f"""
<form class="bar" method="get" action="/" role="search" aria-label="Filtrar vacantes">
  <div class="field">
    <label for="f-q">Buscar en el título</label>
    <input type="text" id="f-q" name="q" value="{e(params.get("q") or "")}"
           placeholder="python, backend…">
  </div>
  <div class="field">
    <label for="f-min">Puntaje mínimo</label>
    <input type="number" id="f-min" name="min" value="{e(params.get("min") or "")}"
           step="1" min="0" style="width:7rem">
  </div>
  <div class="field">
    <label for="f-sort">Ordenar por</label>
    <select id="f-sort" name="sort">{options}</select>
  </div>
  <div class="field check">
    <input type="checkbox" id="f-new" name="new" value="1"{" checked" if params.get("new") else ""}>
    <label for="f-new">Solo nuevas</label>
  </div>
  <div class="field"><button type="submit">Filtrar</button></div>
  <div class="field"><a href="/">Limpiar</a></div>
</form>
<p class="sub" role="status">Mostrando {showing} de {total} vacantes que pasaron el filtro.</p>
"""


def render_job_card(row: dict, top: float, seniority: dict) -> str:
    level = seniority.get(str(row.get("seniority_id")), "sin nivel")
    # Relative to the best result in this run: the maximum possible total moves
    # with the weights in profile.toml, so an absolute bar would be a lie. The
    # number beside it is the accessible value; the bar is decoration.
    width = 0 if top <= 0 else max(0, min(100, round(row["score"] / top * 100)))
    badge = ' <span class="tag new">NUEVA</span>' if row["is_new"] else ""
    terms = (
        f'<p class="terms">Coincide en: {e(", ".join(row["matched"]))}</p>'
        if row.get("matched")
        else ""
    )
    warn = (
        f'<p class="terms"><span class="tag warn">OJO</span> '
        f'También menciona: {e(", ".join(row["penalized"]))}</p>'
        if row.get("penalized")
        else ""
    )
    return f"""<li class="job">
  <h2><a href="/aviso/{e(urllib.parse.quote(row["id"]))}">{e(row["title"])}</a>{badge}</h2>
  <p class="meta"><span class="score">{row["score"]:.1f} puntos</span><span>{e(level)}</span><span>{e(fmt_salary(row))}</span><span>{row["applications"]} postulaciones</span><span>{e(fmt_age(row["published_at"]))}</span></p>
  <div class="meter" aria-hidden="true"><i style="width:{width}%"></i></div>
  {terms}{warn}
</li>"""


def render_index(result: scan.Result | None, params: dict, running: bool) -> bytes:
    if result is None:
        return page(
            "Sin datos",
            """<header><h1>jobscan</h1></header>
<main id="main">
  <div class="empty">
    <p>Todavía no corriste ningún barrido sobre esta base.</p>
    <form method="post" action="/scan"><button type="submit">Barrer ahora</button></form>
  </div>
</main>""",
        )

    rows = apply_filters(result.jobs, params)
    top = max((r["score"] for r in result.jobs), default=0.0)
    listing = (
        '<ol class="jobs">'
        + "\n".join(render_job_card(r, top, result.seniority_names) for r in rows)
        + "</ol>"
        if rows
        else '<div class="empty"><p>Ninguna vacante coincide con ese filtro.</p>'
        '<p><a href="/">Ver todas</a></p></div>'
    )

    dropped = ""
    if result.dropped:
        items = "\n".join(
            f'<li><a href="{e(d["url"])}">{e(d["title"])}</a> — {e(d["reasons"][0])}</li>'
            for d in result.dropped[:120]
        )
        rest = (
            f"<li>…y {len(result.dropped) - 120} más</li>"
            if len(result.dropped) > 120
            else ""
        )
        dropped = f"""<details>
  <summary>Descartadas antes de puntuar ({len(result.dropped)})</summary>
  <p class="sub">Un filtro que no podés auditar es un filtro que esconde trabajo bueno.</p>
  <ul>{items}{rest}</ul>
</details>"""

    if running:
        action = ('<p class="sub"><strong>Hay un barrido corriendo.</strong> '
                  '<a href="/progreso">Ver el progreso</a>.</p>')
    else:
        action = ('<form method="post" action="/scan">'
                  '<button type="submit">Barrer de nuevo</button></form>')

    semantic = "" if result.semantic_on else " · capa semántica apagada"
    body = f"""<header>
  <h1>Vacantes</h1>
  <p class="sub">Último barrido: {e(fmt_when(result.finished_at))} · {result.swept} avisos revisados · {len(result.jobs)} pasaron el filtro · {result.new_count} nuevas{e(semantic)}</p>
  {action}
</header>
<main id="main">
{render_filters(params, len(result.jobs), len(rows))}
{listing}
{dropped}
</main>"""
    return page("Vacantes", body)


def render_detail(result: scan.Result | None, job_id: str) -> tuple[int, bytes]:
    row = next((r for r in (result.jobs if result else []) if r["id"] == job_id), None)
    if row is None:
        return 404, page(
            "No encontrada",
            '<main id="main"><h1>Esa vacante no está en el último barrido</h1>'
            '<p><a href="/">Volver a la lista</a></p></main>',
        )

    level = result.seniority_names.get(str(row.get("seniority_id")), "sin nivel")
    parts = "\n".join(
        f'<tr><th scope="row">{e(PART_LABELS.get(k, k))}</th>'
        f'<td class="num">{v:.2f}</td></tr>'
        for k, v in row["parts"].items()
    )
    sim = "no calculada" if row["semantic"] is None else f"{row['semantic']:.3f}"

    body = f"""<header>
  <p class="sub"><a href="/">← Volver a la lista</a></p>
  <h1>{e(row["title"])}</h1>
  <p class="meta"><span class="score">{row["score"]:.1f} puntos</span><span>{e(level)}</span><span>{e(fmt_salary(row))}</span><span>{row["applications"]} postulaciones</span><span>{e(fmt_age(row["published_at"]))}</span><span>{e(row["category"] or "sin categoría")}</span></p>
  <p><a href="{e(row["url"])}" rel="noopener">Abrir el aviso en Get on Board</a></p>
</header>
<main id="main">
  <h2>De dónde sale el puntaje</h2>
  <table>
    <caption class="sub">Cada señal ya multiplicada por su peso en profile.toml.</caption>
    <thead><tr><th scope="col">Señal</th><th scope="col" class="num">Aporte</th></tr></thead>
    <tbody>{parts}</tbody>
    <tfoot><tr><th scope="row">Total</th><td class="num">{row["score"]:.2f}</td></tr></tfoot>
  </table>
  <h2>Coincidencias</h2>
  <p>Términos de tu stack presentes: {e(", ".join(row["matched"]) or "ninguno")}.</p>
  <p>Tecnologías ajenas mencionadas, que penalizan sin descartar:
     {e(", ".join(row["penalized"]) or "ninguna")}.</p>
  <p>Coseno contra tu perfil: {e(sim)}.</p>
</main>"""
    return 200, page(row["title"], body)


def render_progress(state: "ScanState") -> bytes:
    lines = state.snapshot()
    log = "\n".join(lines) or "Arrancando…"
    if state.running:
        note = '<p class="sub">Esta página se actualiza sola cada 3 segundos.</p>'
        tail = ""
    elif not lines:
        # Reachable by typing the URL. Saying "listo" here would report a scan
        # that never happened as one that finished.
        note = '<p class="sub">No hay ningún barrido corriendo.</p>'
        tail = '<p><a href="/">Volver a la lista</a></p>'
        log = "Nada que mostrar todavía."
    elif state.error:
        note = '<p class="sub"><strong>El barrido falló.</strong></p>'
        tail = '<p><a href="/">Volver a la lista</a></p>'
        log = f"{log}\n\n{state.error}"
    else:
        note = '<p class="sub">Listo.</p>'
        tail = '<p><a href="/">Ver los resultados</a></p>'
    body = f"""<header><h1>Barriendo Get on Board</h1>{note}</header>
<main id="main">
  <pre class="log" role="log" aria-label="Progreso del barrido">{e(log)}</pre>
  {tail}
</main>"""
    return page("Barriendo", body, refresh=3 if state.running else 0)


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
                elif path.startswith("/aviso/"):
                    status, body = render_detail(
                        scan.last(db), urllib.parse.unquote(path[len("/aviso/"):])
                    )
                    self._send(status, body)
                else:
                    self._send(404, page(
                        "No existe",
                        '<main id="main"><h1>Esa página no existe</h1>'
                        '<p><a href="/">Volver a la lista</a></p></main>',
                    ))
            except Exception:
                self._send(500, page(
                    "Error",
                    '<main id="main"><h1>Algo se rompió</h1>'
                    f'<pre class="log">{e(traceback.format_exc(limit=4))}</pre>'
                    '<p><a href="/">Volver a la lista</a></p></main>',
                ))

        def do_POST(self) -> None:
            if urllib.parse.urlparse(self.path).path.rstrip("/") != "/scan":
                self._redirect("/")
                return
            # The body carries nothing, but it has to be drained or the browser
            # sees the connection close mid-request and reports a reset.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)

            profile = scan.load_profile(profile_path)
            state.start(
                lambda say: scan.run(
                    profile=profile, db=db, no_semantic=no_semantic, on_progress=say
                )
            )
            self._redirect("/progreso")

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
