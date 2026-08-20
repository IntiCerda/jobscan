"""Tests for the parts of the UI that fail silently.

A broken renderer is loud — the page does not load and you notice. What is
quiet is a filter that hides postings, a title that escapes into markup, an age
frozen at scan time, or a form control that lost its label and became invisible
to a screen reader while still looking fine on screen. Those are what is locked
here.

Run: python tests/test_web.py       (or: python -m pytest tests/ -q)
"""

from __future__ import annotations

import re
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobscan import web  # noqa: E402
from jobscan.scan import Result, age_days  # noqa: E402
from jobscan.store import Store  # noqa: E402


def check(name, cond):
    assert cond, name
    print(f"  ok  {name}")


def make_row(**over) -> dict:
    base = dict(
        id="1",
        title="Backend Developer",
        url="https://www.getonbrd.com/empleos/programacion/1",
        company_id="9",
        category="Programming",
        countries=["Chile"],
        seniority_id=3,
        min_salary=1800,
        max_salary=2400,
        published_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        applications=12,
        is_new=True,
        score=31.5,
        parts={"stack": 9.1, "semantic": 7.0, "competition": 8.0},
        matched=["python", "fastapi"],
        penalized=[],
        semantic=0.71,
    )
    base.update(over)
    return base


def make_result(**over) -> Result:
    base = dict(
        finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        semantic_on=True,
        swept=200,
        jobs=[make_row()],
        dropped=[{"id": "9", "title": "Dev Oracle", "url": "http://x", "reasons": ["oracle en el título"]}],
        seniority_names={"3": "Semi Senior"},
    )
    base.update(over)
    return Result(**base)


# -- escaping ---------------------------------------------------------------


def test_markup_in_a_posting_does_not_execute_in_the_page():
    # Every string on the page arrives from a third-party API. A posting whose
    # title is markup has to render as that title, not run as one.
    hostile = "<script>alert(1)</script>"
    result = make_result(jobs=[make_row(title=hostile, matched=['" onmouseover="x'])])
    html = web.render_index(result, {}, False).decode()
    check("the raw tag never reaches the document", "<script>alert" not in html)
    check("it is shown as text instead", "&lt;script&gt;" in html)
    check("a quote cannot break out of an attribute", '" onmouseover="x' not in html)


# -- filtering --------------------------------------------------------------


def test_a_junk_filter_shows_everything_instead_of_a_stack_trace():
    # Query strings get hand-edited and bookmarked. A value that does not parse
    # must degrade to "do not narrow", never to an empty page or a 500.
    rows = [make_row(id="1", score=30.0), make_row(id="2", score=10.0)]
    check("junk in min is ignored", len(web.apply_filters(rows, {"min": "cualquiera"})) == 2)
    check("an unknown sort falls back to score", web.apply_filters(rows, {"sort": "???"})[0]["id"] == "1")
    check("an absent filter narrows nothing", len(web.apply_filters(rows, {})) == 2)


def test_filters_narrow_on_what_the_reader_can_see():
    rows = [
        make_row(id="1", title="Backend Python", score=30.0, applications=400, is_new=True),
        make_row(id="2", title="Data Engineer", score=10.0, applications=5, is_new=False),
    ]
    check("text matches the title", [r["id"] for r in web.apply_filters(rows, {"q": "data"})] == ["2"])
    check("text also matches a stack term", len(web.apply_filters(rows, {"q": "fastapi"})) == 2)
    check("min-score cuts below the floor", [r["id"] for r in web.apply_filters(rows, {"min": "20"})] == ["1"])
    check("only-new keeps the new one", [r["id"] for r in web.apply_filters(rows, {"new": "1"})] == ["1"])
    check(
        "sorting by competition puts the quiet one first",
        web.apply_filters(rows, {"sort": "quiet"})[0]["id"] == "2",
    )


# -- time -------------------------------------------------------------------


def test_age_is_recomputed_from_publication_not_frozen_at_scan():
    # The web UI opens on a stored snapshot. If age had been computed once at
    # scan time, a posting swept last week would still read "publicada hoy".
    week_old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    check("a week-old posting reads as a week old", 6.5 < age_days(week_old) < 7.5)
    check("an undated posting reads as brand new", age_days(None) == 0.0)
    check("an unparseable date does not raise", age_days("not-a-date") == 0.0)
    check("the label follows the recomputed age", "días" in web.fmt_age(week_old))


# -- persistence ------------------------------------------------------------


def test_a_snapshot_survives_the_round_trip_through_sqlite():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.sqlite3"
        original = make_result()
        with Store(db) as store:
            check("an empty database has no run to show", store.last_run() is None)
            store.save_run(original.to_dict())
            store.commit()
        with Store(db) as store:
            back = Result.from_dict(store.last_run())
        check("the ranking comes back", back.jobs[0]["title"] == original.jobs[0]["title"])
        check("the score survives", back.jobs[0]["score"] == 31.5)
        check("the drop reasons survive", back.dropped[0]["reasons"] == ["oracle en el título"])
        check("new-count is derived, not stored stale", back.new_count == 1)


def test_a_snapshot_from_an_older_version_still_opens():
    # Fields get added. A stored run that predates them must render, because
    # the alternative is a page that cannot even offer the button to re-scan.
    back = Result.from_dict({"jobs": [], "campo_que_ya_no_existe": 1})
    check("unknown keys are dropped rather than raised on", back.jobs == [])


# -- pages ------------------------------------------------------------------


def test_an_empty_database_offers_a_scan_instead_of_a_blank_page():
    html = web.render_index(None, {}, False).decode()
    check("it explains there is nothing yet", "Todavía no corriste" in html)
    check("and gives you the way out", 'action="/scan"' in html)


def test_an_unknown_posting_id_is_a_404_with_a_way_back():
    status, body = web.render_detail(make_result(), "no-existe")
    check("the status is 404, not 200", status == 404)
    check("the page links back to the list", 'href="/"' in body.decode())


def test_the_breakdown_page_shows_every_signal_not_just_the_total():
    # This page is the whole point of --explain in the browser: if the ranking
    # looks wrong, the reader has to see which weight caused it.
    _, body = web.render_detail(make_result(), "1")
    html = body.decode()
    for label in ("Stack", "Semántica", "Competencia"):
        check(f"{label} is broken out", label in html)
    check("the total is stated", "31.50" in html)


# -- accessibility ----------------------------------------------------------


class _Controls(HTMLParser):
    """Collects form-control ids and the ids that labels point at."""

    def __init__(self) -> None:
        super().__init__()
        self.controls: list[tuple[str, str | None]] = []
        self.labelled: set[str] = set()
        self.lang: str | None = None
        self.ids: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        if a.get("id"):
            self.ids.add(a["id"])
        if tag in ("input", "select", "textarea") and a.get("type") not in ("submit", "hidden"):
            self.controls.append((tag, a.get("id")))
        if tag == "label" and a.get("for"):
            self.labelled.add(a["for"])
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])


def _relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(fg: str, bg: str) -> float:
    hi, lo = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def palette(dark: bool) -> dict[str, str]:
    """The custom properties in effect for one colour scheme.

    Read out of the stylesheet rather than duplicated here, so changing a colour
    is what this test reacts to.
    """
    light, _, rest = web.CSS.partition("@media (prefers-color-scheme: dark)")
    block = rest if dark else light
    found = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))
    if dark:  # the dark block only overrides; the rest is inherited
        return {**palette(False), **found}
    return found


def test_the_palette_stays_readable_in_both_schemes():
    # Contrast is the accessibility failure nobody reports: the page looks fine
    # to whoever picked the colours and is unreadable to everyone else. 4.5:1 is
    # the WCAG AA floor for body text.
    for dark in (False, True):
        p = palette(dark)
        scheme = "oscuro" if dark else "claro"
        pairs = [
            ("texto sobre el fondo", p["ink"], p["bg"]),
            ("texto apagado sobre el panel", p["muted"], p["panel"]),
            ("enlaces sobre el panel", p["accent"], p["panel"]),
            ("texto del botón", p["accent-ink"], p["accent"]),
            ("la etiqueta NUEVA", p["new"], p["new-bg"]),
            ("la etiqueta OJO", p["warn"], p["warn-bg"]),
        ]
        for label, fg, bg in pairs:
            got = contrast(fg, bg)
            check(f"{scheme}: {label} llega a AA ({got:.1f}:1)", got >= 4.5)


def test_every_form_control_keeps_its_label():
    # A control that loses its label still looks fine on screen and becomes an
    # unnamed field to a screen reader. Nothing about the page reveals it.
    parser = _Controls()
    parser.feed(web.render_index(make_result(), {}, False).decode())
    check("there are controls to check", len(parser.controls) >= 4)
    for tag, control_id in parser.controls:
        check(f"the {tag} has an id", bool(control_id))
        check(f"a label points at {control_id}", control_id in parser.labelled)


def test_the_page_keeps_its_landmarks_and_language():
    parser = _Controls()
    parser.feed(web.render_index(make_result(), {}, False).decode())
    check("the document declares its language", parser.lang == "es")
    check("there is a main landmark to skip to", "main" in parser.ids)
    check("and a skip link that targets it", "#main" in parser.links)


# -- concurrency ------------------------------------------------------------


def test_a_second_scan_cannot_start_while_one_is_running():
    # The server is threaded. Two clicks on "barrer" arriving together would
    # otherwise run two sweeps against the same SQLite file.
    state = web.ScanState()
    release = threading.Event()
    state.start(lambda say: release.wait(2))
    check("the first click starts a scan", state.running)
    check("the second click is refused", state.start(lambda say: None) is False)
    release.set()


def test_progress_does_not_report_a_scan_that_never_ran_as_finished():
    # The URL is reachable by hand. "Listo · ver los resultados" on a scan that
    # was never started sends the reader looking for results that do not exist.
    html = web.render_progress(web.ScanState()).decode()
    check("it says nothing is running", "No hay ningún barrido" in html)
    check("and does not claim to be done", "Ver los resultados" not in html)


def test_a_failed_scan_reports_on_the_page_instead_of_vanishing():
    state = web.ScanState()
    done = threading.Event()

    def boom(say):
        say("empezando")
        done.set()
        raise RuntimeError("la red se cayó")

    state.start(boom)
    done.wait(2)
    for _ in range(200):  # the thread clears `running` just after raising
        if not state.running:
            break
        threading.Event().wait(0.01)
    check("the failure is recorded", state.error is not None and "la red" in state.error)
    check("the log survives the failure", "empezando" in state.snapshot())
    check("and the page says so", "falló" in web.render_progress(state).decode())


# -- end to end -------------------------------------------------------------


def test_the_server_actually_answers():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.sqlite3"
        with Store(db) as store:
            store.save_run(make_result().to_dict())
            store.commit()

        handler = web.make_handler(profile_path=Path("profile.toml"), db=db, no_semantic=True)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            with urllib.request.urlopen(f"{base}/", timeout=5) as r:
                index = r.read().decode()
                check("the index answers 200", r.status == 200)
            check("and shows the ranking", "Backend Developer" in index)

            with urllib.request.urlopen(f"{base}/aviso/1", timeout=5) as r:
                check("the detail page answers 200", r.status == 200)

            with urllib.request.urlopen(f"{base}/?q=backend&sort=quiet", timeout=5) as r:
                check("a filtered URL answers 200", r.status == 200)

            with urllib.request.urlopen(f"{base}/progreso", timeout=5) as r:
                check("the progress page answers 200", r.status == 200)

            try:
                urllib.request.urlopen(f"{base}/no-existe", timeout=5)
                check("an unknown path is a 404", False)
            except urllib.error.HTTPError as exc:
                check("an unknown path is a 404", exc.code == 404)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print("\ntodo verde")
