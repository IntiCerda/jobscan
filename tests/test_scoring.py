"""Tests for the parts that decide what you never see.

Filtering is where a bug is invisible: a too-greedy knockout silently hides
good postings and the report still looks healthy. These lock the behaviour that
matters — what gets dropped, and that competition outranks a marginal stack win.

Run: python -m pytest tests/ -q      (or: python tests/test_scoring.py)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobscan.api import Job, _strip_html, parse_job  # noqa: E402
from jobscan.embed import cosine  # noqa: E402
from jobscan.scoring import (  # noqa: E402
    detect_lang,
    hard_requirement_text,
    knockouts,
    score,
)

PROFILE = {
    "filters": {
        "exclude_langs": ["en"],
        "exclude_flags": ["talent_pool"],
        "exclude_in_title": ["oracle", ".net"],
        "penalize_in_body": ["spring boot"],
        "blockers": ["aws", "retail"],
        "openness": ["junior", "te ensenamos", "sin experiencia"],
    },
    "fit": {"stack": {"python": 3.0, "fastapi": 3.0, "rag": 4.0}},
    "scoring": {
        "weight_stack": 14.0,
        "stack_half_point": 12.0,
        "weight_semantic": 12.0,
        "weight_competition": 8.0,
        "weight_freshness": 5.0,
        "weight_salary": 3.0,
        "weight_seniority": 4.0,
        "good_applications_count": 100,
        "stale_after_days": 45.0,
        "target_salary_usd": 2000,
        "weight_blocker": 2.5,
        "blocker_cap": 3.0,
        "weight_openness": 2.0,
        "openness_cap": 3.0,
    },
    "search": {"remote_only": True},
    "seniority": {"fit": [1, 2, 3], "reach": [4]},
}


def make_job(**over) -> Job:
    base = dict(
        id="x",
        title="Backend Developer",
        company_id="1",
        category="Programming",
        lang="es",
        remote=True,
        remote_modality="fully_remote",
        countries=("Chile",),
        seniority_id=3,
        modality_id=1,
        min_salary=None,
        max_salary=None,
        published_at=datetime.now(timezone.utc) - timedelta(days=2),
        applications_count=10,
        flags=(),
        text="Backend Developer. Buscamos Python y FastAPI.",
    )
    base.update(over)
    return Job(**base)


EN_BODY = (
    "We are seeking a mid level engineer who can confidently build and iterate on both app and web components. You should be comfortable working across the full delivery cycle from scoping and implementation to testing and deployment support. We value engineers who take ownership of their work, bringing issues to light early and following through to completion with the rest of the team."
)

ES_BODY = (
    "Buscamos un desarrollador backend con experiencia en Python y FastAPI para sumarse al equipo de la plataforma. El puesto es remoto desde cualquier lugar y el trabajo se organiza por sprints. Entre las responsabilidades del cargo estan el diseno de servicios, la escritura de pruebas y la revision de codigo con el resto de los ingenieros de nuestro equipo."
)


def check(name, cond):
    assert cond, name
    print(f"  ok  {name}")


def test_knockouts():
    check("english is dropped", knockouts(make_job(lang="en"), PROFILE))
    check("spanish passes", not knockouts(make_job(lang="es"), PROFILE))
    check(
        "talent pool is dropped",
        knockouts(make_job(flags=("talent_pool",)), PROFILE),
    )
    check(
        "an unlisted flag does not drop",
        not knockouts(make_job(flags=("unclear_functions",)), PROFILE),
    )
    check(
        "excluded tech in the title drops",
        knockouts(make_job(title="Desarrollador Oracle PL/SQL"), PROFILE),
    )
    check(
        "the same tech only in the body does not drop",
        not knockouts(
            make_job(text="Backend. Deseable Oracle."), PROFILE
        ),
    )


def test_short_terms_do_not_match_inside_words():
    # Regression on a live false positive: a Digital Marketing posting earned
    # the full RAG bonus because "rag" sits inside "fragmented".
    trap = make_job(text="replace manual, fragmented customer flows")
    real = make_job(text="pipelines RAG en producción")
    check("'rag' does not match inside 'fragmented'", score(trap, PROFILE).parts["stack"] == 0.0)
    check("but a real mention still counts", score(real, PROFILE).parts["stack"] > 0)


def test_plurals_and_suffixes_still_match():
    p = dict(PROFILE, fit={"stack": {"embedding": 3.0, "microservicio": 2.0}})
    both = make_job(text="embeddings sobre microservicios")
    check(
        "a trailing suffix does not break the match",
        set(score(both, p).matched) == {"embedding", "microservicio"},
    )


def test_category_knockout():
    p = dict(PROFILE)
    p["filters"] = dict(PROFILE["filters"], allowed_categories=["Programming"])
    check("a foreign category drops", knockouts(make_job(category="Digital Marketing"), p))
    check("an allowed one passes", not knockouts(make_job(category="Programming"), p))
    check(
        "with no list configured, category is ignored",
        not knockouts(make_job(category="Digital Marketing"), PROFILE),
    )


def test_stack_counted_once():
    repeated = make_job(text="python python python python python")
    single = make_job(text="python")
    a = score(repeated, PROFILE).parts["stack"]
    b = score(single, PROFILE).parts["stack"]
    check("repetition does not inflate the stack score", a == b)


def test_body_penalty_ranks_down_without_hiding():
    clean = make_job(text="Python FastAPI")
    mixed = make_job(text="Python FastAPI y algo de Spring Boot")
    check(
        "a foreign stack in the body costs points",
        score(mixed, PROFILE).total < score(clean, PROFILE).total,
    )
    check("but does not disqualify", not knockouts(mixed, PROFILE))


def test_competition_counts_rivals_not_arrival_rate():
    # Regression on a real design error. The first version divided applications
    # by age, which scored a posting from today with 92 applicants *below* a
    # six-month-old one with 447 — because 92/day beats 2.7/day. You compete
    # against the pile, not the rate.
    zerviz = make_job(
        applications_count=92,
        published_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    usercode = make_job(
        applications_count=447,
        published_at=datetime.now(timezone.utc) - timedelta(days=168),
    )
    check(
        "92 rivals beats 447 regardless of when they arrived",
        score(zerviz, PROFILE).parts["competition"]
        > score(usercode, PROFILE).parts["competition"],
    )


def test_fresh_and_quiet_beats_stale_and_crowded():
    # The lesson from applying by hand: a slightly better keyword match on a
    # stale, crowded posting is not worth more than a fresh, quiet one.
    crowded = make_job(
        text="Python FastAPI RAG",
        applications_count=867,
        published_at=datetime.now(timezone.utc) - timedelta(days=190),
    )
    quiet = make_job(
        text="Python FastAPI",
        applications_count=92,
        published_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    check(
        "fresh and uncrowded beats a marginally better match that is stale",
        score(quiet, PROFILE).total > score(crowded, PROFILE).total,
    )


def test_stack_saturates_so_a_long_list_cannot_dominate():
    # Regression on the second design error the live run exposed: with a linear
    # stack score, a stale, underpaid posting that enumerated fourteen
    # technologies outranked a fresh one paying three times as much.
    kitchen_sink = make_job(
        text="python fastapi rag " * 3,
        applications_count=867,
        min_salary=900,
        max_salary=1200,
        published_at=datetime.now(timezone.utc) - timedelta(days=191),
    )
    focused = make_job(
        text="python fastapi",
        applications_count=52,
        min_salary=3000,
        max_salary=3300,
        published_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    check(
        "a fresh, well-paid, uncrowded posting wins on fewer keyword hits",
        score(focused, PROFILE).total > score(kitchen_sink, PROFILE).total,
    )
    check(
        "stack stays inside its weight",
        score(kitchen_sink, PROFILE).parts["stack"] < PROFILE["scoring"]["weight_stack"],
    )


def test_missing_salary_is_neutral_not_punished():
    silent = make_job()
    lowball = make_job(min_salary=500, max_salary=700)
    check(
        "publishing nothing beats publishing a lowball",
        score(silent, PROFILE).parts["salary"]
        > score(lowball, PROFILE).parts["salary"],
    )


def test_seniority_bands():
    check("semi senior is a full fit", score(make_job(seniority_id=3), PROFILE).parts["seniority"] > 0)
    check("expert scores zero", score(make_job(seniority_id=5), PROFILE).parts["seniority"] == 0.0)
    check(
        "senior is a partial reach",
        0 < score(make_job(seniority_id=4), PROFILE).parts["seniority"]
        < score(make_job(seniority_id=3), PROFILE).parts["seniority"],
    )


def test_semantic_absence_is_not_a_zero_similarity():
    without = score(make_job(), PROFILE)
    check("no vectors means no semantic reading", without.semantic is None)
    check("and contributes nothing rather than a penalty", without.parts["semantic"] == 0.0)
    check("cosine of a missing side is None", cosine(None, [1.0, 2.0]) is None)
    check("cosine of a zero vector is None", cosine([0.0, 0.0], [1.0, 2.0]) is None)
    check("identical vectors are 1.0", abs(cosine([1.0, 2.0], [1.0, 2.0]) - 1.0) < 1e-9)


def test_html_stripping_and_parsing():
    check(
        "tags are removed, text is kept",
        _strip_html("<div><strong>Hola</strong> mundo</div>") == "Hola mundo",
    )
    job = parse_job(
        {
            "id": "abc",
            "attributes": {
                "title": "Dev",
                "description": "<p>Python</p>",
                "lang": "es",
                "published_at": 1700000000,
                "applications_count": 5,
                "rejected_reasons": [{"talent_pool": {"title": "Talent pool"}}],
                "seniority": {"data": {"id": 3}},
                "company": {"data": {"id": 42}},
            },
        }
    )
    check("flags are flattened to keys", job.flags == ("talent_pool",))
    check("seniority is parsed as an int", job.seniority_id == 3)
    check("prose lands in the haystack", "Python" in job.text)


def test_age_of_undated_posting_does_not_bury_it():
    check("an unknown date reads as brand new", make_job(published_at=None).age_days == 0.0)


def test_english_body_is_caught_when_the_api_leaves_lang_blank():
    """A Zagged posting reported `lang_not_specified` with an all-English body
    and sailed straight past the language knockout into the ranking. The field
    the API left blank is recoverable from the prose."""
    job = make_job(lang="lang_not_specified", text=EN_BODY)
    reasons = knockouts(job, PROFILE)
    check("the English body is knocked out", reasons)
    check("the report says the language was deduced", "deducido" in reasons[0])


def test_a_spanish_body_survives_a_blank_lang_field():
    check(
        "Spanish prose is not mistaken for English",
        not knockouts(make_job(lang="lang_not_specified", text=ES_BODY), PROFILE),
    )


def test_a_declared_language_is_never_second_guessed():
    """`lang` is what the employer chose to publish under. Only a blank field
    gets sniffed — otherwise a Spanish posting quoting an English job
    description could be dropped on the strength of the quote."""
    check(
        "a declared 'es' is trusted over the prose",
        not knockouts(make_job(lang="es", text=EN_BODY), PROFILE),
    )


def test_a_stub_posting_is_left_alone():
    check("too little prose returns no verdict", detect_lang("Backend Developer") is None)


# The posting that motivated the blocker penalty: strong on the words the ranker
# rewards, and a wall in the section that says what is mandatory.
WITI_BODY = chr(10).join([
    "Buscamos Full Stack Senior. Arquitectura hexagonal y microservicios, NestJS y TypeScript.",
    "Requisitos Excluyentes",
    "Experiencia en servicios AWS: Lambda, S3, DynamoDB, SQS, SNS y EventBridge.",
    "Experiencia comprobada en el rubro retail y/o ecommerce.",
    "Requisitos Deseables",
    "Experiencia con Next.js y con herramientas de observabilidad.",
])


def test_a_wall_of_excluyentes_ranks_below_the_same_posting_without_them():
    walled = make_job(text=WITI_BODY)
    clean = make_job(text=WITI_BODY.replace("Requisitos Excluyentes", "Requisitos Deseables"))
    assert score(walled, PROFILE).total < score(clean, PROFILE).total


def test_the_blocked_terms_are_reported_so_the_penalty_is_explainable():
    assert score(make_job(text=WITI_BODY), PROFILE).blocked == ["aws", "retail"]


def test_a_walled_posting_is_ranked_down_but_never_hidden():
    # The whole point of a penalty over a knockout: it still passes the filter.
    assert knockouts(make_job(text=WITI_BODY), PROFILE) == []
    assert score(make_job(text=WITI_BODY), PROFILE).total > 0


def test_the_same_terms_outside_the_excluyente_block_cost_nothing():
    # "we build on AWS" is a fact about the team, not a wall in front of you.
    plain = make_job(text="Trabajamos sobre AWS y atendemos clientes de retail. Python y FastAPI.")
    assert score(plain, PROFILE).blocked == []


def test_a_posting_that_never_says_excluyente_has_no_hard_requirements():
    assert hard_requirement_text("Buscamos Python. Deseable AWS y retail.") == ""


def test_the_block_stops_at_the_deseables_heading():
    block = hard_requirement_text(WITI_BODY)
    assert "aws" in block
    assert "observabilidad" not in block


def test_the_inline_form_is_caught_too():
    # LinkedIn postings write it on one line, before the word.
    block = hard_requirement_text("Remoto. INGLES AVANZADO Y AWS EXCLUYENTE. Postula ya.")
    assert "aws" in block


def test_the_penalty_is_capped_so_a_service_list_cannot_dominate():
    many = {**PROFILE, "filters": {**PROFILE["filters"],
                                   "blockers": ["aws", "retail", "gcp", "azure", "banca"]}}
    body = WITI_BODY.replace("Requisitos Excluyentes",
                             chr(10).join(["Requisitos Excluyentes", "GCP, Azure y banca tambien."]))
    got = score(make_job(text=body), many)
    assert len(got.blocked) == 5
    # cap 3 x weight 2.5 -- five hits cost the same as three.
    assert got.parts["blockers"] == -7.5


def test_a_posting_that_trains_beats_the_same_one_that_does_not():
    # The bias this closes: the stack table can only reward what you already
    # know, so a shop willing to teach scored no better than one demanding a
    # finished engineer.
    teaches = make_job(text="Buscamos junior, te ensenamos, sin experiencia previa. Stack Go.")
    demands = make_job(text="Buscamos ingeniero. Stack Go.")
    assert score(teaches, PROFILE).total > score(demands, PROFILE).total


def test_the_openness_signals_are_reported():
    got = score(make_job(text="Buscamos junior, te ensenamos."), PROFILE)
    assert got.openness == ["junior", "te ensenamos"]


def test_no_experience_required_is_not_ranked_below_senior():
    # seniority id 1 scored 0.0 while a Senior posting scored 0.4 -- backwards
    # for someone breaking in.
    entry = make_job(seniority_id=1)
    senior = make_job(seniority_id=4)
    assert score(entry, PROFILE).parts["seniority"] > score(senior, PROFILE).parts["seniority"]


def test_an_onsite_posting_is_dropped_here_not_by_the_api():
    # The API's remote=true filter omits postings that call themselves remote,
    # so the sweep fetches everything and the decision is made on the attribute.
    assert "presencial o híbrido" in knockouts(make_job(remote=False), PROFILE)


def test_a_remote_posting_survives():
    assert knockouts(make_job(remote=True), PROFILE) == []


def test_remote_only_can_be_turned_off():
    loose = {**PROFILE, "search": {"remote_only": False}}
    assert knockouts(make_job(remote=False), loose) == []


def test_a_spanish_posting_that_demands_english_is_dropped():
    body = "Buscamos Python y Docker. Ingles intermedio-avanzado o avanzado (indispensable)."
    assert "pide ingles como requisito" in [r.replace("é","e") for r in knockouts(make_job(text=body), PROFILE)]


def test_the_level_can_come_before_the_word():
    body = "Requisitos: Python solido. Nivel de ingles avanzado. Docker."
    assert knockouts(make_job(text=body), PROFILE)


def test_reading_english_is_not_a_requirement_to_speak_it():
    # What he actually has, and what many postings ask for: reading technical
    # docs. Knocking these out would throw away good roles.
    body = "Buscamos Python y Docker. Ingles tecnico de lectura. Trabajo en espanol."
    assert knockouts(make_job(text=body), PROFILE) == []


def test_a_bare_mention_of_english_is_not_a_requirement():
    body = "Buscamos Python. El equipo es diverso y hay companeros que hablan ingles."
    assert knockouts(make_job(text=body), PROFILE) == []


def test_a_level_named_without_a_cefr_label_still_counts():
    body = "Python y Docker. Ingles hablado y escrito solido: stakeholders en USA."
    assert knockouts(make_job(text=body), PROFILE)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print("\ntodo verde")
