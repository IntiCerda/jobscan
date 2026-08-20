"""Get on Board API client.

The public search endpoint (`/api/v0/search/jobs`) needs no credentials but
does require a query term — passing an empty query returns zero pages. That is
why callers sweep several terms and union the results rather than asking for
"everything".

Only the standard library is used, so the tool runs on a bare Python install.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

BASE = "https://www.getonbrd.com/api/v0"

# A descriptive agent is courtesy on a free public API; some hosts also reject
# the default urllib string outright.
USER_AGENT = "jobscan/1.0 (personal job search; +https://github.com/IntiCerda)"


class ApiError(RuntimeError):
    pass


def _get(path: str, params: dict | None = None, *, timeout: int = 30) -> dict:
    """GET a JSON:API document, retrying twice on transport errors.

    Retries cover the transient case (connection reset, 502 from a proxy). A
    4xx is not retried: the request itself is wrong and repeating it will not
    change that.
    """
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise ApiError(f"{exc.code} on {url}") from exc
            last = exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        # Linear backoff is enough here; this is a handful of requests against a
        # public endpoint, not a hot path.
        time.sleep(1.5 * (attempt + 1))

    raise ApiError(f"failed after 3 attempts: {url}") from last


@dataclass(frozen=True)
class Job:
    """A posting, flattened to the fields ranking actually reads.

    `text` concatenates every prose field once so scoring and embedding both
    work off a single haystack instead of re-walking the raw payload.
    """

    id: str
    title: str
    company_id: str
    category: str
    lang: str
    remote: bool
    remote_modality: str
    countries: tuple[str, ...]
    seniority_id: int | None
    modality_id: int | None
    min_salary: int | None
    max_salary: int | None
    published_at: datetime | None
    applications_count: int
    flags: tuple[str, ...]
    text: str
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def url(self) -> str:
        return f"https://www.getonbrd.com/empleos/programacion/{self.id}"

    @property
    def age_days(self) -> float:
        """Days since publication. Unknown dates are treated as brand new so a
        missing timestamp never silently buries an otherwise good posting."""
        if self.published_at is None:
            return 0.0
        delta = datetime.now(timezone.utc) - self.published_at
        return max(delta.total_seconds() / 86400.0, 0.0)

    @property
    def applications_per_day(self) -> float:
        # Clamped at one day so a posting published hours ago does not divide by
        # a fraction and report an absurd rate.
        return self.applications_count / max(self.age_days, 1.0)


def _strip_html(value: str) -> str:
    """Crude tag removal. The API returns small HTML fragments, not documents,
    so a parser would cost a dependency for no gain in fidelity."""
    out, depth = [], 0
    for ch in value:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(depth - 1, 0)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


_PROSE_FIELDS = ("description", "projects", "functions", "desirable", "benefits")


def _relation_id(attrs: dict, key: str) -> str | None:
    node = (attrs.get(key) or {}).get("data")
    if isinstance(node, dict):
        return str(node.get("id"))
    return None


def parse_job(node: dict) -> Job:
    a = node.get("attributes", {})

    published = a.get("published_at")
    when = (
        datetime.fromtimestamp(published, tz=timezone.utc)
        if isinstance(published, (int, float))
        else None
    )

    # `rejected_reasons` is Get on Board's own review of the posting — entries
    # like `talent_pool` or `not_really_remote`. Flattened to plain keys so the
    # filter config can name them directly.
    flags: list[str] = []
    for reason in a.get("rejected_reasons") or []:
        if isinstance(reason, dict):
            flags.extend(reason.keys())

    body = " ".join(_strip_html(a.get(f) or "") for f in _PROSE_FIELDS)

    seniority = _relation_id(a, "seniority")
    modality = _relation_id(a, "modality")

    return Job(
        id=str(node.get("id", "")),
        title=(a.get("title") or "").strip(),
        company_id=_relation_id(a, "company") or "",
        category=a.get("category_name") or "",
        lang=a.get("lang") or "",
        remote=bool(a.get("remote")),
        remote_modality=a.get("remote_modality") or "",
        countries=tuple(a.get("countries") or []),
        seniority_id=int(seniority) if seniority and seniority.isdigit() else None,
        modality_id=int(modality) if modality and modality.isdigit() else None,
        min_salary=a.get("min_salary"),
        max_salary=a.get("max_salary"),
        published_at=when,
        applications_count=int(a.get("applications_count") or 0),
        flags=tuple(flags),
        text=f"{a.get('title') or ''} {body}",
        raw=node,
    )


def search(
    query: str,
    *,
    category: str | None = None,
    remote_only: bool = True,
    per_page: int = 100,
    max_pages: int = 3,
) -> list[Job]:
    """One query angle, paginated. Stops early when the API reports fewer pages
    than requested so a narrow term costs a single round trip."""
    params: dict[str, str | int] = {"query": query, "per_page": per_page}
    if category:
        params["category"] = category
    if remote_only:
        params["remote"] = "true"

    jobs: list[Job] = []
    page = 1
    while page <= max_pages:
        params["page"] = page
        doc = _get("/search/jobs", params)
        rows = doc.get("data") or []
        jobs.extend(parse_job(r) for r in rows)

        total = (doc.get("meta") or {}).get("total_pages") or 1
        if page >= total or not rows:
            break
        page += 1

    return jobs


def sweep(
    queries: list[str],
    *,
    category: str | None = None,
    remote_only: bool = True,
    max_pages: int = 3,
    on_progress=None,
) -> list[Job]:
    """Union of several query angles, deduplicated by posting id.

    No single term covers the board: "python" misses a TypeScript service role,
    "backend" misses one titled "Ingeniero de IA". Running the angles and
    merging is cheaper than trying to write one perfect query.
    """
    found: dict[str, Job] = {}
    for q in queries:
        try:
            hits = search(
                q, category=category, remote_only=remote_only, max_pages=max_pages
            )
        except ApiError as exc:
            # One bad angle must not sink the sweep.
            if on_progress:
                on_progress(q, 0, str(exc))
            continue
        new = sum(1 for j in hits if j.id not in found)
        for j in hits:
            found.setdefault(j.id, j)
        if on_progress:
            on_progress(q, new, None)
    return list(found.values())


def lookup(kind: str) -> dict[str, str]:
    """id -> name for a reference collection (`seniorities`, `modalities`)."""
    doc = _get(f"/{kind}")
    return {
        str(n.get("id")): (n.get("attributes") or {}).get("name", "")
        for n in doc.get("data") or []
    }
