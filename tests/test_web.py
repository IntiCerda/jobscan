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


# -- grouping ---------------------------------------------------------------


def spread(n: int = 30) -> list[dict]:
    """Rows spread across every axis, so no grouping lands them all in one pile."""
    cats = ["Programming", "SysAdmin / DevOps / QA", "Data Science / Analytics"]
    return [
        make_row(
            id=str(i),
            title=f"Puesto {i}",
            score=float(30 - i),
            seniority_id=2 + (i % 3),
            category=cats[i % 3],
            published_at=(datetime.now(timezone.utc) - timedelta(days=i * 3)).isoformat(),
        )
        for i in range(n)
    ]


def test_grouping_never_loses_a_posting():
    # The quiet failure: a fold that drops rows still renders a page that looks
    # complete, and the count in each summary agrees with what is inside it.
    rows = spread()
    for axis in web.GROUPS:
        groups = web.group_rows(rows, axis, {"2": "Junior", "3": "Semi Senior", "4": "Senior"})
        total = sum(len(items) for _, items in groups)
        check(f"agrupar por {axis} conserva las {len(rows)}", total == len(rows))
        seen = {r["id"] for _, items in groups for r in items}
        check(f"agrupar por {axis} no duplica ninguna", len(seen) == len(rows))


def test_every_posting_reaches_the_page_even_when_folded():
    # Folded is not the same as absent. A collapsed <details> still contains its
    # cards, so the whole ranking has to be in the document either way.
    rows = spread()
    html = web.render_listing(rows, {}, 30.0, {})
    check("every card is rendered", html.count('<li class="job"') == len(rows))
    check("the counts add up to the list", sum(
        int(n) for n in re.findall(r'class="count">(\d+) vacantes', html)
    ) == len(rows))


def test_folding_does_not_bury_the_best_posting():
    # The whole point of the tool is the ranking. A page that loads with the
    # top posting inside a collapsed group has traded away its reason to exist.
    rows = spread()
    best = max(rows, key=lambda r: r["score"])
    html = web.render_listing(rows, {}, 30.0, {})
    first_group = html.split("</details>")[0]
    check("the first group is open", first_group.startswith('<details class="grp" open'))
    check("and it holds the best posting", f'>{best["title"]}<' in first_group)


def test_the_page_does_not_load_fully_expanded():
    # The request that started this: 64 cards is too much scroll. Folding that
    # opens everything anyway has fixed nothing.
    rows = spread(30)
    html = web.render_listing(rows, {}, 30.0, {})
    opened = sum(
        len(re.findall(r'<li class="job"', chunk))
        for chunk in re.split(r'<details class="grp"', html)[1:]
        if chunk.startswith(" open")
    )
    check(f"only {opened} of {len(rows)} are open on load", opened < len(rows))
    check("but at least one group is showing", opened > 0)


def test_the_same_axis_twice_is_not_a_subgroup():
    # Grouping by band inside band gives one subgroup per group holding
    # everything: noise dressed as structure.
    axis, sub = web.group_axes({"group": "band", "sub": "band"})
    check("the repeated axis drops out", (axis, sub) == ("band", ""))


def test_the_default_view_groups_by_band_and_an_empty_axis_stays_flat():
    axis, sub = web.group_axes({})
    check("the default is band over category", (axis, sub) == ("band", "category"))
    check("an explicit empty axis means flat", web.group_axes({"group": ""})[0] == "")
    check("an invented axis does not raise", web.group_axes({"group": "colour"})[0] == "")
    check("a flat listing has no groups", '<details class="grp"' not in
          web.render_listing(spread(), {"group": ""}, 30.0, {}))


def test_how_much_is_folded_away_is_stated_in_text():
    # A collapsed group whose size is only implied by styling tells a screen
    # reader nothing about what it is hiding.
    html = web.render_listing(spread(), {}, 30.0, {})
    check("each summary carries its count as words", "vacantes</span></summary>" in html)


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
            ("texto secundario sobre superficie", p["ink-2"], p["surface"]),
            ("texto terciario sobre el fondo", p["ink-3"], p["bg"]),
            ("texto terciario sobre superficie", p["ink-3"], p["surface"]),
            ("la señal como texto sobre el fondo", p["signal"], p["bg"]),
            ("la señal como texto sobre superficie", p["signal"], p["surface"]),
            ("texto del botón y la etiqueta NUEVA", p["signal-ink"], p["signal"]),
            ("chips de coincidencia", p["signal"], p["signal-soft"]),
            ("chips de advertencia", p["amber"], p["amber-bg"]),
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


# -- profile and cv ---------------------------------------------------------


def test_the_profile_survives_the_form_round_trip():
    # The form rewrites profile.toml. A field it silently drops or mangles is a
    # tuning someone made by hand, gone without an error — so the whole cycle
    # dict -> TOML -> dict has to be lossless for every shape a profile uses.
    import tomllib

    from jobscan import profiles

    original = tomllib.loads(Path("profile.toml").read_text(encoding="utf-8"))
    check("the emitted TOML parses back to the same profile",
          tomllib.loads(profiles.dumps(original)) == original)

    tricky = {
        "identity": {"summary": 'línea 1\ncon "comillas" y \\barras'},
        "fit": {"stack": {"next.js": 2.0, "búsqueda semántica": 3.5}},
    }
    check("quoted keys and multiline strings survive",
          tomllib.loads(profiles.dumps(tricky)) == tricky)


def test_a_broken_form_never_touches_the_file():
    # The one honest failure mode of a settings form: half-understanding the
    # input and writing that. Any error must block the save entirely.
    from jobscan import profiles

    form = {
        "summary": ["dev"], "queries": ["python"], "category": ["programming"],
        "stack": ["python = tres"],  # unreadable weight
        "weight_stack": ["catorce"],  # unreadable number
    }
    _, errors = profiles.from_form(form)
    check("the bad weight is reported by line", any("python = tres" in e for e in errors))
    check("the bad number names its field", any("weight_stack" in e for e in errors))

    empty_queries = {"summary": ["dev"], "queries": [""], "stack": ["python = 3"]}
    _, errors = profiles.from_form(empty_queries)
    check("no queries is an error, not an empty sweep", any("búsqueda" in e for e in errors))


def test_a_valid_form_writes_a_profile_the_scanner_can_read():
    import tomllib

    from jobscan import profiles

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "profile.toml"
        form = {
            "summary": ["backend con Python"],
            "queries": ["python\nbackend"],
            "category": ["programming"], "remote_only": ["1"], "max_pages": ["2"],
            "stack": ["python = 3\nfastapi"],
            "exclude_in_title": ["oracle"], "penalize_in_body": ["spring boot"],
            "allowed_categories": ["Programming"], "exclude_langs": ["en"],
            "exclude_flags": ["talent_pool", "not_really_remote"],
            "weight_stack": ["14"], "weight_semantic": ["12"],
            "weight_competition": ["8"], "weight_freshness": ["5"],
            "weight_salary": ["3"], "weight_seniority": ["4"],
            "stack_half_point": ["12"], "good_applications_count": ["100"],
            "stale_after_days": ["45"], "target_salary_usd": ["2000"],
            "seniority_fit": ["2", "3"], "seniority_reach": ["4"],
        }
        profile, errors = profiles.from_form(form)
        check("a complete form has no errors", errors == [])
        profiles.save(profile, path)
        back = tomllib.loads(path.read_text(encoding="utf-8"))
        check("queries land as a list", back["search"]["queries"] == ["python", "backend"])
        check("a term without weight defaults", back["fit"]["stack"]["fastapi"] == 2.0)
        check("flags land from the checkboxes",
              back["filters"]["exclude_flags"] == ["talent_pool", "not_really_remote"])
        check("seniority ids land as ints", back["seniority"]["fit"] == [2, 3])

        profiles.save(profile, path)
        check("saving keeps a backup of the previous file",
              path.with_suffix(".toml.bak").exists())


def test_the_cv_reaches_the_semantic_reference():
    # A CV that saves fine but never reaches the embedding is the quiet
    # failure: the page says "guardado" and the ranking ignores it forever.
    from jobscan import profiles
    from jobscan.scan import identity_text

    with tempfile.TemporaryDirectory() as tmp:
        prof = Path(tmp) / "profile.toml"
        profiles.save_cv(prof, "  Trabajé en pagos con FastAPI  ")
        check("the cv round-trips through its file",
              profiles.load_cv(prof) == "Trabajé en pagos con FastAPI")

        text = identity_text({"identity": {"summary": "Backend dev"}}, profiles.load_cv(prof))
        check("summary and cv are both in the reference",
              "Backend dev" in text and "FastAPI" in text)
        check("without a cv the summary stands alone",
              identity_text({"identity": {"summary": "Backend dev"}}, "") == "Backend dev")


def test_the_settings_pages_answer_and_keep_their_labels():
    from jobscan import profiles as profiles_mod

    prof_page = web.render_profile_page(
        {"identity": {}, "search": {}, "filters": {}, "scoring": {}, "fit": {}, "seniority": {}},
        profiles_mod.SENIORITY_FALLBACK,
    ).decode()
    parser = _Controls()
    parser.feed(prof_page)
    check("the profile form is fully labelled",
          all(cid in parser.labelled for _, cid in parser.controls if cid))
    check("no control is missing its id", all(cid for _, cid in parser.controls))

    errors_page = web.render_profile_page(
        {"identity": {}, "search": {}, "filters": {}, "scoring": {}, "fit": {}, "seniority": {}},
        profiles_mod.SENIORITY_FALLBACK, errors=["'weight_stack' tiene que ser un número"],
    ).decode()
    check("errors are announced, not just colored", 'role="alert"' in errors_page)
    check("and say nothing was saved", "No guardé nada" in errors_page)

    cv_page = web.render_cv_page("mi cv", semantic_ready=False).decode()
    check("the cv comes back into the textarea", ">mi cv</textarea>" in cv_page)
    check("a missing ollama is stated, not hidden", "no está corriendo" in cv_page)


def test_every_page_carries_the_navigation():
    pages = {
        "radar": web.render_index(make_result(), {}, False).decode(),
        "perfil": web.render_profile_page(
            {"identity": {}, "search": {}, "filters": {}, "scoring": {}, "fit": {}, "seniority": {}},
            {"3": "Semi Senior"},
        ).decode(),
        "cv": web.render_cv_page("", semantic_ready=True).decode(),
        "progreso": web.render_progress(web.ScanState()).decode(),
    }
    for name, html in pages.items():
        check(f"{name} shows the nav", 'aria-label="Secciones"' in html)
        check(f"{name} links the other sections", '/perfil' in html and '/cv' in html)
    check("the active page is marked for assistive tech",
          'aria-current="page"' in pages["perfil"])


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

        # A copy, never the real profile: this test POSTs to /perfil and /cv,
        # and a test that can rewrite the profile someone tuned is a landmine.
        profile_path = Path(tmp) / "profile.toml"
        profile_path.write_text(
            Path("profile.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )

        handler = web.make_handler(profile_path=profile_path, db=db, no_semantic=True)
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

            with urllib.request.urlopen(f"{base}/perfil", timeout=5) as r:
                check("the profile page answers 200", r.status == 200)
                check("and carries the stack from the file", "fastapi" in r.read().decode())

            body = urllib.parse.urlencode({"cv": "Mi experiencia real"}).encode()
            req = urllib.request.Request(f"{base}/cv", data=body, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                check("posting a cv lands back on the cv page", r.status == 200)
            check("and the cv landed beside the profile",
                  (Path(tmp) / "cv.txt").read_text(encoding="utf-8").strip()
                  == "Mi experiencia real")

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
