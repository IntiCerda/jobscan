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


@dataclass
class Score:
    total: float
    parts: dict[str, float] = field(default_factory=dict)
    matched: list[str] = field(default_factory=list)
    penalized: list[str] = field(default_factory=list)
    semantic: float | None = None


def knockouts(job: Job, profile: dict) -> list[str]:
    """Reasons this posting is not worth an application. Empty means it passed."""
    f = profile.get("filters", {})
    reasons: list[str] = []

    if job.lang in set(f.get("exclude_langs", [])):
        reasons.append(f"escrito en '{job.lang}' — exige postular en ese idioma")

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
    }

    return Score(
        total=round(sum(parts.values()), 2),
        parts={k: round(v, 2) for k, v in parts.items()},
        matched=sorted(matched),
        penalized=sorted(penalized),
        semantic=None if sim is None else round(sim, 4),
    )
