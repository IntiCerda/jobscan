"""Knockouts and ranking.

Two stages, kept separate because they answer different questions.

`knockouts` answers "is this worth reading at all" and is absolute: a posting
whose title is built around Oracle does not become relevant because it also
says Docker. Returning the reasons rather than a bool means the report can show
what was dropped and why, which is how you notice a filter is too aggressive.

`score` answers "of the ones worth reading, which first" and is relative. The
weights encode a lesson from applying by hand: a fresh posting with few
applicants beats a marginally better stack match from three months ago that
already has nine hundred applications.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from .api import Job
from .embed import cosine


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern:
    r"""Match `term` at a word boundary, allowing a suffix.

    Plain substring matching awarded a Digital Marketing posting the full RAG
    bonus because "rag" appears inside "fragmented". A leading `\b` stops that.
    The trailing `\w*` is what keeps "embedding" matching "embeddings" and
    "microservicio" matching "microservicios", which a closing `\b` would
    break.
    """
    return re.compile(r"\b" + re.escape(term.lower()) + r"\w*")


# Function words, not vocabulary. A Spanish posting is full of English
# technology names and an English one carries none of these; the words around
# the stack are what separate the two.
_ES_WORDS = frozenset(
    "de la que el en y los las del un una para con por su al se lo como más "
    "experiencia conocimientos buscamos trabajo puesto sobre nuestro".split()
)
_EN_WORDS = frozenset(
    "the of and to in for with you your we our are is will have as on that "
    "be this from at their they".split()
)


def detect_lang(text: str) -> str | None:
    """'es', 'en', or None when there is not enough prose to decide.

    Get on Board reports `lang_not_specified` on a good share of postings, and
    some of those are written end to end in English — a Zagged listing whose
    entire body was English sailed past the `lang == "en"` knockout and landed
    in the ranking, where it cost a real read to discard. Counting function
    words recovers the field the API left blank.

    Returning None rather than guessing matters: a stub posting of three lines
    should fall through to whatever the API said, not be assigned a language on
    the strength of four words.
    """
    words = re.findall(r"[a-záéíóúñ]+", text.lower())
    if len(words) < 40:
        return None
    es = sum(w in _ES_WORDS for w in words)
    en = sum(w in _EN_WORDS for w in words)
    if es == en:
        return None
    return "es" if es > en else "en"


# An English *requirement*, as opposed to a mention. "inglés técnico de lectura"
# and "inglés deseable" are not requirements and must not match, so the level and
# the insistence have to be adjacent to the word — a bare "inglés" is not enough.
_DEMANDS_ENGLISH = re.compile(
    r"ingl[eé]s[^.\n]{0,60}(avanzado|intermedio|fluido|conversacional|"
    r"excluyente|indispensable|obligatorio|requerido|b2|c1|c2|"
    # "Inglés hablado y escrito sólido" — a level named without a CEFR label.
    r"hablado|escrito|s[oó]lido|bilingüe|bilingue|nativo)"
    r"|(avanzado|intermedio|fluido|conversacional|excluyente|indispensable|"
    r"obligatorio|requerido|nivel|dominio|manejo)[^.\n]{0,40}ingl[eé]s"
    r"|english[^.\n]{0,40}(proficiency|required|fluent|b2|c1)",
    re.I,
)


@dataclass
class Score:
    total: float
    parts: dict[str, float] = field(default_factory=dict)
    matched: list[str] = field(default_factory=list)
    penalized: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    openness: list[str] = field(default_factory=list)
    semantic: float | None = None


def knockouts(job: Job, profile: dict) -> list[str]:
    """Reasons this posting is not worth an application. Empty means it passed."""
    f = profile.get("filters", {})
    reasons: list[str] = []

    excluded_langs = set(f.get("exclude_langs", []))
    lang, inferred = job.lang, False
    if lang in ("", "lang_not_specified"):
        sniffed = detect_lang(job.text)
        if sniffed:
            lang, inferred = sniffed, True
    if lang in excluded_langs:
        how = " (deducido del texto)" if inferred else ""
        reasons.append(f"escrito en '{lang}'{how} — exige postular en ese idioma")

    # Applied here rather than as an API parameter: the server's `remote=true`
    # filter drops postings that describe themselves as remote. Done at this
    # stage a rejection is at least reported, instead of the posting never
    # arriving at all.
    if profile.get("search", {}).get("remote_only", True) and not job.remote:
        reasons.append("presencial o híbrido")

    # A posting written in Spanish that demands English is just as closed as one
    # written in English, and `exclude_langs` only ever looked at the language of
    # the prose. Improving's "Inglés intermedio-avanzado o avanzado
    # (indispensable)" sailed through, as did a posting whose own URL says
    # `con-ingles-avanzado`.
    if "en" in excluded_langs and _DEMANDS_ENGLISH.search(job.text):
        reasons.append("pide inglés como requisito")

    # Salary is one weighted signal among six, so a posting can pay a quarter of
    # what you need and still rank on stack alone — a $500-650 React role sat at
    # #16. A published ceiling below the floor is not a trade-off to weigh, it is
    # a no, so it belongs here. Postings that publish nothing are untouched:
    # most good ones omit it, and silence is not an offer.
    floor = f.get("salary_floor_usd")
    if floor and job.max_salary and job.max_salary < float(floor):
        reasons.append(f"paga hasta ${job.max_salary} — bajo el piso de ${floor}")

    excluded_flags = set(f.get("exclude_flags", []))
    hit_flags = sorted(excluded_flags.intersection(job.flags))
    if hit_flags:
        reasons.append("marcado por Get on Board: " + ", ".join(hit_flags))

    # The API's `category` query parameter narrows the search but does not
    # constrain results: a Digital Marketing posting came back from a
    # `category=programming` request. Filtering on the category the posting
    # actually carries is what keeps sales and finance roles out.
    allowed = f.get("allowed_categories")
    if allowed and job.category and job.category not in set(allowed):
        reasons.append(f"categoría '{job.category}' fuera de las buscadas")

    title = job.title.lower()
    for term in f.get("exclude_in_title", []):
        if term in title:
            reasons.append(f"'{term}' en el título — es el eje del puesto")
            break

    return reasons


def _stack_points(job: Job, profile: dict) -> tuple[float, list[str]]:
    """Sum of weights for terms present, counted once each.

    Once each, not once per occurrence: otherwise a posting that repeats
    "Python" in every bullet outranks one that quietly requires the whole stack.
    """
    haystack = job.text.lower()
    total, matched = 0.0, []
    for term, weight in (profile.get("fit", {}).get("stack", {})).items():
        if _term_pattern(term).search(haystack):
            total += float(weight)
            matched.append(term)
    return total, matched


def _body_penalty(job: Job, profile: dict) -> tuple[float, list[str]]:
    """Foreign stacks mentioned in the body cost points but do not disqualify.

    A posting can list Java among nice-to-haves and still be a Node role; the
    penalty ranks it below a clean match without hiding it.
    """
    haystack = job.text.lower()
    hits = [
        t
        for t in profile.get("filters", {}).get("penalize_in_body", [])
        if _term_pattern(t).search(haystack)
    ]
    return -1.5 * len(hits), hits


# Where a requirements block stops being mandatory. Everything from an
# "excluyente" marker up to one of these is read as hard requirements.
_STOP_MARKER = re.compile(
    r"deseable|deseado|opcional|valorar|valoramos|valoraremos|plus\b"
    r"|nice to have|beneficio|ofrecemos|qué ofrecemos|te ofrecemos",
    re.I,
)
_EXCLUYENTE = re.compile(r"excluyentes?", re.I)

# How far past the marker to keep reading, and how far back to look. The lookback
# catches the inline form ("INGLÉS AVANZADO EXCLUYENTE"), where the requirement is
# written before the word rather than under it.
_BLOCK_AHEAD = 1200
_BLOCK_BACK = 200


def hard_requirement_text(text: str) -> str:
    """The parts of `text` that read as mandatory requirements.

    Returns "" when the posting never says "excluyente", which is most of them —
    a posting that does not separate must-have from nice-to-have gets no penalty,
    because there is nothing to read as a wall.
    """
    regions: list[str] = []
    for m in _EXCLUYENTE.finditer(text):
        start = max(0, m.start() - _BLOCK_BACK)
        tail = text[m.end() : m.end() + _BLOCK_AHEAD]
        stop = _STOP_MARKER.search(tail)
        regions.append(text[start : m.end()] + tail[: stop.start() if stop else len(tail)])
    return "\n".join(regions).lower()


def _blocker_penalty(job: Job, profile: dict) -> tuple[float, list[str]]:
    """Terms you do not have, found where the posting says they are mandatory.

    The keyword ranker cannot tell "we build with hexagonal architecture" from
    "hexagonal architecture required, plus six AWS services you have never
    touched". Both are the same words in the same body, so a posting that is a
    wall scored as a match — Witi sat at #2 for six days on `hexagonal`, `ddd`
    and `event-driven` while listing AWS, IaC and retail experience as
    excluyentes.

    Deliberately a penalty and not a knockout: "excluyente" is often aspirational,
    and a posting worth arguing with should still be visible. It drops, it does
    not disappear.
    """
    block = hard_requirement_text(job.text)
    if not block:
        return 0.0, []
    hits = [
        t
        for t in profile.get("filters", {}).get("blockers", [])
        if _term_pattern(t).search(block)
    ]
    return float(len(hits)), hits


def _openness(job: Job, profile: dict) -> tuple[float, list[str]]:
    """Signals that the posting will train rather than demand a finished engineer.

    The stack table can only reward what you already know, which quietly biases
    the whole ranking toward jobs you have already done. A startup writing
    "junior, te enseñamos, stack Go" scores nothing on stack — and it is exactly
    the posting worth reading, because the gate there is attitude, not a
    checklist.

    Counted once per term like the stack, so a posting cannot win by saying
    "junior" nine times.
    """
    haystack = job.text.lower()
    hits = [
        t
        for t in profile.get("filters", {}).get("openness", [])
        if _term_pattern(t).search(haystack)
    ]
    return float(len(hits)), hits


def _competition(job: Job, profile: dict) -> float:
    """1.0 when few people have applied in total, decaying toward 0.

    Raw count, not a per-day rate. The first version divided by age, which
    ranked a posting from this morning with 92 applicants *below* a six-month
    old one with 447 — because 92/day looks worse than 2.7/day. But you compete
    against everyone who applied, not against the arrival rate: 447 CVs ahead
    of yours is a worse race than 92, whenever they arrived. Staleness is a
    separate concern and `_freshness` already carries it.
    """
    good = float(profile.get("scoring", {}).get("good_applications_count", 100.0))
    count = float(job.applications_count)
    if count <= good:
        return 1.0
    return max(0.0, good / count)


def _freshness(job: Job, profile: dict) -> float:
    stale = float(profile.get("scoring", {}).get("stale_after_days", 45.0))
    if stale <= 0:
        return 0.0
    return max(0.0, 1.0 - (job.age_days / stale))


def _salary(job: Job, profile: dict) -> float:
    """Scored against the target, neutral when the posting publishes nothing.

    Most good postings omit salary. Treating silence as a penalty would rank
    the honest-but-quiet ones below a published lowball, which is backwards.
    """
    target = float(profile.get("scoring", {}).get("target_salary_usd", 0) or 0)
    if not target:
        return 0.5
    top = job.max_salary or job.min_salary
    if not top:
        return 0.5
    return max(0.0, min(1.5, top / target))


def _seniority(job: Job, profile: dict) -> float:
    conf = profile.get("seniority", {})
    fit = set(conf.get("fit", []))
    reach = set(conf.get("reach", []))
    if job.seniority_id is None:
        return 0.5
    if job.seniority_id in fit:
        return 1.0
    if job.seniority_id in reach:
        return 0.4
    return 0.0


def score(
    job: Job,
    profile: dict,
    *,
    job_vector: list[float] | None = None,
    profile_vector: list[float] | None = None,
) -> Score:
    w = profile.get("scoring", {})

    stack, matched = _stack_points(job, profile)
    penalty, penalized = _body_penalty(job, profile)
    blockers, blocked = _blocker_penalty(job, profile)
    openness, open_hits = _openness(job, profile)

    sim = cosine(job_vector, profile_vector)
    # Cosine over text embeddings clusters in roughly 0.4-0.9; rescaling from
    # 0.5 spreads that band across the usable range instead of handing every
    # posting most of the semantic weight.
    sim_points = 0.0 if sim is None else max(0.0, (sim - 0.5) / 0.4)

    # Saturating rather than linear. Raw points are unbounded, so a posting
    # that enumerates fourteen technologies scored ~35 while every other signal
    # combined topped out near 30 — which is how a dead, underpaid posting with
    # 867 applicants outranked a fresh one paying twice as much. Diminishing
    # returns put stack on the same 0-1 footing as the rest: matching ten of
    # your terms is clearly better than five, barely better than twenty.
    raw = max(0.0, stack + penalty)
    half = float(w.get("stack_half_point", 12.0))
    stack_norm = raw / (raw + half) if half > 0 else 0.0

    parts = {
        "stack": float(w.get("weight_stack", 1.0)) * stack_norm,
        "semantic": float(w.get("weight_semantic", 0.0)) * sim_points,
        "competition": float(w.get("weight_competition", 0.0)) * _competition(job, profile),
        "freshness": float(w.get("weight_freshness", 0.0)) * _freshness(job, profile),
        "salary": float(w.get("weight_salary", 0.0)) * _salary(job, profile),
        "seniority": float(w.get("weight_seniority", 0.0)) * _seniority(job, profile),
        # Its own part rather than more negative stack points: the saturating
        # norm would swallow it, and a posting that is a wall should say so in
        # the breakdown instead of quietly ranking lower. Capped so a listing
        # that repeats every cloud service cannot drive the total to nonsense.
        "blockers": -float(w.get("weight_blocker", 2.5))
        * min(blockers, float(w.get("blocker_cap", 3.0))),
        # Capped like the blockers, and for the same reason: two or three of
        # these say "they will train you", a dozen just means a wordy advert.
        "openness": float(w.get("weight_openness", 2.0))
        * min(openness, float(w.get("openness_cap", 3.0))),
    }

    return Score(
        total=round(sum(parts.values()), 2),
        parts={k: round(v, 2) for k, v in parts.items()},
        matched=sorted(matched),
        penalized=sorted(penalized),
        blocked=sorted(blocked),
        openness=sorted(open_hits),
        semantic=None if sim is None else round(sim, 4),
    )
