"""Reading and writing the personal side of the tool: profile.toml and the CV.

The profile is the whole point of the repo being shareable — the code holds no
opinions about any particular person, so someone else only has to edit their
own profile to use it. Editing TOML by hand is exactly the kind of chore that
stops that person, which is why the web UI writes this file for them.

Python ships a TOML reader but no writer, and the profile has a small, known
shape, so a serializer for that shape is a page of code — smaller and easier
to audit than a dependency. Comments in a hand-edited file do not survive a
rewrite; the emitted file carries its own header saying so.

The CV lives beside the profile as plain text, not inside it: it is pasted
prose, it can be long, and keeping it out of the TOML means a broken paste can
never corrupt the rest of the configuration.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

HEADER = (
    "# Search profile. Everything personal lives here; the code ranks for\n"
    "# whoever points it at their own file.\n"
    "#\n"
    "# This file is rewritten by the web UI (Perfil page). Hand-written\n"
    "# comments do not survive a save from the browser.\n"
)

# The seniority ids Get on Board uses, as a fallback for when the API is not
# reachable while the form renders. Names come from /api/v0/seniorities.
SENIORITY_FALLBACK = {
    "1": "Sin experiencia",
    "2": "Junior",
    "3": "Semi Senior",
    "4": "Senior",
    "5": "Expert",
}

# Named weight sets, so choosing what matters is one decision instead of six
# numbers. "custom" means the numeric fields are read as-is.
PRESETS = {
    "equilibrado": {
        "weight_stack": 14.0, "weight_semantic": 12.0, "weight_competition": 8.0,
        "weight_freshness": 5.0, "weight_salary": 3.0, "weight_seniority": 4.0,
    },
    "nuevas": {
        "weight_stack": 10.0, "weight_semantic": 8.0, "weight_competition": 10.0,
        "weight_freshness": 9.0, "weight_salary": 3.0, "weight_seniority": 4.0,
    },
    "sueldo": {
        "weight_stack": 12.0, "weight_semantic": 10.0, "weight_competition": 6.0,
        "weight_freshness": 4.0, "weight_salary": 8.0, "weight_seniority": 4.0,
    },
    "match": {
        "weight_stack": 16.0, "weight_semantic": 14.0, "weight_competition": 5.0,
        "weight_freshness": 4.0, "weight_salary": 3.0, "weight_seniority": 4.0,
    },
}

PRESET_LABELS = {
    "equilibrado": ("Equilibrado", "un poco de todo, el punto de partida"),
    "nuevas": ("Primero lo nuevo", "avisos frescos y con poca competencia arriba"),
    "sueldo": ("Mejor pagado", "el sueldo publicado pesa mucho más"),
    "match": ("Mi stack exacto", "prioriza coincidencia técnica sobre todo"),
}


def detect_preset(scoring: dict) -> str:
    """Which preset the stored weights correspond to, or "custom"."""
    for name, weights in PRESETS.items():
        if all(float(scoring.get(k, -1)) == v for k, v in weights.items()):
            return name
    return "custom"


# One-click suggestion groups for the stack field. Clicking beats typing for
# recall: recognizing a term you use is one decision, remembering the whole
# list is a working-memory tax.
STACK_SUGGESTIONS = {
    "Lenguajes & backend": [
        "python", "fastapi", "django", "node", "typescript", "nestjs", "go", "rust",
    ],
    "Frontend": ["react", "next.js", "vue", "angular", "svelte", "tailwind"],
    "Datos & bases": ["postgresql", "mongodb", "redis", "kafka", "elasticsearch"],
    "IA & modelos locales": [
        "llm", "rag", "embedding", "pgvector", "langchain", "ollama",
        "llama", "qwen", "deepseek", "mistral", "kimi", "nvidia",
    ],
    "Cloud & DevOps": [
        "docker", "kubernetes", "terraform", "aws", "gcp", "grafana", "prometheus",
    ],
}

# Veto packs: whole families that usually travel together. One click adds the
# family; individual x removes any survivor worth keeping.
VETO_PACKS = {
    "Mundo Java": ["java", "spring"],
    "Microsoft / .NET": [".net", "c#", "sharepoint"],
    "Enterprise legacy": ["sap", "abap", "oracle", "pl/sql", "cobol", "mainframe"],
    "CMS & e-commerce": ["wordpress", "shopify", "vtex", "magento"],
    "PHP": ["php", "laravel"],
}

# term -> Simple Icons slug, for the small monochrome logos on chips. A term
# that is not here (or a slug the CDN lacks) simply renders without an icon.
ICON_SLUGS = {
    "python": "python", "fastapi": "fastapi", "django": "django",
    "node": "nodedotjs", "node.js": "nodedotjs", "typescript": "typescript",
    "nestjs": "nestjs", "go": "go", "rust": "rust",
    "react": "react", "next.js": "nextdotjs", "vue": "vuedotjs",
    "angular": "angular", "svelte": "svelte", "tailwind": "tailwindcss",
    "postgresql": "postgresql", "postgres": "postgresql", "mongodb": "mongodb",
    "redis": "redis", "kafka": "apachekafka", "elasticsearch": "elasticsearch",
    "ollama": "ollama", "langchain": "langchain", "llama": "meta",
    "qwen": "alibabacloud", "deepseek": "deepseek", "mistral": "mistralai",
    "nvidia": "nvidia", "docker": "docker", "kubernetes": "kubernetes",
    "terraform": "terraform", "aws": "amazonwebservices", "gcp": "googlecloud",
    "grafana": "grafana", "prometheus": "prometheus", "pytest": "pytest",
    "java": "openjdk", "spring": "spring", "php": "php", "laravel": "laravel",
    "wordpress": "wordpress", "shopify": "shopify", "oracle": "oracle",
    "ruby": "ruby", "rails": "rubyonrails", "salesforce": "salesforce",
    "sap": "sap", "cobol": "ibm", "vtex": "vtex", "magento": "magento",
    ".net": "dotnet", "c#": "dotnet", "flutter": "flutter", "swift": "swift",
    "kotlin": "kotlin", "graphql": "graphql", "git": "git",
}


# GetOnBoard's own quality flags a posting can carry, with what they mean.
KNOWN_FLAGS = {
    "talent_pool": "junta CVs sin un puesto concreto",
    "not_really_remote": "dice remoto pero no lo es",
    "unclear_functions": "no explica qué se hace en el puesto",
    "seniority_mismatch": "el nivel pedido no coincide con el aviso",
}


def _key(name: str) -> str:
    return name if _BARE_KEY.match(name) else json.dumps(name, ensure_ascii=False)


def _value(v) -> str:
    # TOML basic strings share JSON's escape rules, so json.dumps emits a
    # valid TOML string — including for summaries with quotes and newlines.
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"cannot serialize {type(v).__name__} to TOML")


def dumps(profile: dict) -> str:
    """The profile as TOML. Handles the shapes a profile actually uses:
    tables, one level of nesting, strings, numbers, booleans and flat lists."""
    out = [HEADER]
    for table, content in profile.items():
        flat = {k: v for k, v in content.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in content.items() if isinstance(v, dict)}
        if flat or not nested:
            out.append(f"\n[{_key(table)}]")
            for k, v in flat.items():
                out.append(f"{_key(k)} = {_value(v)}")
        for sub, sub_content in nested.items():
            out.append(f"\n[{_key(table)}.{_key(sub)}]")
            for k, v in sub_content.items():
                out.append(f"{_key(k)} = {_value(v)}")
    return "\n".join(out) + "\n"


def save(profile: dict, path: Path) -> None:
    """Write the profile, keeping one backup of what was there.

    The backup is insurance against the one honest failure mode of a form
    that rewrites a file: a bug here silently eating a profile someone tuned
    for weeks. One `.bak` is enough to undo that; a history is not needed.
    """
    text = dumps(profile)
    # Parse what we are about to write before touching the file: a serializer
    # bug should fail the save, never corrupt the profile on disk.
    import tomllib

    tomllib.loads(text)
    if path.exists():
        path.with_suffix(".toml.bak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path.write_text(text, encoding="utf-8")


# -- the CV -----------------------------------------------------------------


def cv_path(profile_path: Path) -> Path:
    return profile_path.with_name("cv.txt")


def load_cv(profile_path: Path) -> str:
    p = cv_path(profile_path)
    try:
        return p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def save_cv(profile_path: Path, text: str) -> None:
    cv_path(profile_path).write_text(text.strip() + "\n", encoding="utf-8")


# -- form parsing -----------------------------------------------------------


def _lines(raw: str) -> list[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _floats(form: dict, names: list[str], errors: list[str]) -> dict[str, float]:
    out = {}
    for name in names:
        raw = (form.get(name) or [""])[0].strip().replace(",", ".")
        try:
            out[name] = float(raw)
        except ValueError:
            errors.append(f"'{name}' tiene que ser un número — recibí «{raw}»")
    return out


def parse_stack(raw: str, errors: list[str]) -> dict[str, float]:
    """Lines of `term = weight`, weight optional.

    Written for a textarea someone edits half-awake: blank lines vanish, a
    missing weight defaults instead of failing, and only an unreadable weight
    is an actual error — reported by line so it can be found.
    """
    stack: dict[str, float] = {}
    for line in _lines(raw):
        term, _, weight = line.partition("=")
        term = term.strip().strip('"')
        if not term:
            continue
        weight = weight.strip()
        if not weight:
            stack[term] = 2.0
            continue
        try:
            stack[term] = float(weight.replace(",", "."))
        except ValueError:
            errors.append(f"peso ilegible en el stack: «{line}»")
    return stack


def from_form(form: dict[str, list[str]]) -> tuple[dict, list[str]]:
    """A profile dict built from the settings form, plus every problem found.

    Any error means the caller must not save: a form that writes what it half
    understood is worse than one that asks again.
    """
    errors: list[str] = []

    def first(name: str, default: str = "") -> str:
        return (form.get(name) or [default])[0].strip()

    def many(name: str) -> list[str]:
        return [v for v in form.get(name, []) if v.strip()]

    numbers = _floats(
        form,
        [
            "weight_stack", "weight_semantic", "weight_competition",
            "weight_freshness", "weight_salary", "weight_seniority",
            "stack_half_point", "good_applications_count",
            "weight_blocker", "blocker_cap",
            "weight_openness", "openness_cap",
            "stale_after_days", "target_salary_usd",
        ],
        errors,
    )
    stack = parse_stack(first("stack", ""), errors)

    queries = _lines(first("queries"))
    if not queries:
        errors.append("hace falta al menos un término de búsqueda — la API no devuelve nada sin query")

    summary = first("summary")
    max_pages_raw = first("max_pages", "3")
    try:
        max_pages = max(1, int(max_pages_raw))
    except ValueError:
        errors.append(f"'páginas por búsqueda' tiene que ser un entero — recibí «{max_pages_raw}»")
        max_pages = 3

    def int_list(name: str) -> list[int]:
        return sorted(int(v) for v in many(name) if v.isdigit())

    # A named preset overrides the six weight numbers: picking what matters
    # is one decision, and the numeric grid only counts when asked for.
    preset = (form.get("preset") or ["custom"])[0]
    weights = PRESETS.get(preset) or {
        k: numbers.get(k, PRESETS["equilibrado"][k])
        for k in PRESETS["equilibrado"]
    }

    profile = {
        "identity": {"summary": summary},
        "search": {
            "queries": queries,
            "category": first("category", "programming"),
            "remote_only": bool(form.get("remote_only")),
            "max_pages_per_query": max_pages,
        },
        "filters": {
            "exclude_langs": _lines(first("exclude_langs").replace(",", "\n")),
            "allowed_categories": _lines(first("allowed_categories")),
            "exclude_flags": many("exclude_flags"),
            "exclude_in_title": _lines(first("exclude_in_title")),
            "penalize_in_body": _lines(first("penalize_in_body")),
            "blockers": _lines(first("blockers")),
            "openness": _lines(first("openness")),
        },
        "fit": {"stack": stack},
        "scoring": {
            **weights,
            "stack_half_point": numbers.get("stack_half_point", 12.0),
            "weight_blocker": numbers.get("weight_blocker", 2.5),
            "blocker_cap": numbers.get("blocker_cap", 3.0),
            "weight_openness": numbers.get("weight_openness", 2.0),
            "openness_cap": numbers.get("openness_cap", 3.0),
            "good_applications_count": int(numbers.get("good_applications_count", 100)),
            "stale_after_days": numbers.get("stale_after_days", 45.0),
            "target_salary_usd": int(numbers.get("target_salary_usd", 0)),
        },
        "seniority": {"fit": int_list("seniority_fit"), "reach": int_list("seniority_reach")},
    }
    return profile, errors
