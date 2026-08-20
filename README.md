# jobscan

Ranks [Get on Board](https://www.getonbrd.com) postings against a profile you
describe once, so the daily question becomes *what appeared since yesterday*
instead of *let me scroll through two hundred listings again*.

Built after spending an afternoon applying by hand and noticing that the
expensive part was never writing the application — it was deciding which
postings were worth opening.

```bash
python -m jobscan                 # rank everything, mark what is new
python -m jobscan --new-only      # only postings unseen until now
python -m jobscan --explain       # show the score breakdown per posting
python -m jobscan --no-semantic   # keyword scoring only
python -m jobscan -o hoy.md       # write the report to a file
```

No dependencies. `urllib`, `sqlite3`, `tomllib`, `array`, `math` — a bare
Python 3.11+ install runs it.

---

## How it decides

**Sweep.** The public search endpoint requires a query term; an empty query
returns nothing. No single term covers the board either — `python` misses a
TypeScript service role, `backend` misses one titled *Ingeniero de IA*. So the
tool runs a list of query angles and unions the results by posting id. In
practice `node` contributes forty postings that `python` never surfaces.

**Knockouts.** Absolute, and they run before scoring:

- `lang == "en"` — the posting requires applying in a language you listed as
  out of reach. One field, no reading required.
- Get on Board's own `rejected_reasons` — the site flags postings as
  `talent_pool` (collecting CVs, not filling a role), `not_really_remote`,
  `unclear_functions`, `seniority_mismatch`. Free quality signal that most
  scrapers throw away.
- A vetoed technology **in the title**. A technology in the title is the spine
  of the job. The same word in the body is only a penalty, because a Node role
  can mention Java once under nice-to-haves and still be a Node role.

Dropped postings are reported with the reason, in a collapsed section. A filter
you cannot audit is a filter that quietly hides good work.

**Score.** Six weighted signals, each normalised to roughly 0–1 so no single
one can run away with the ranking:

| Signal | Reading |
|---|---|
| `stack` | Configured terms present, counted once each, saturating |
| `semantic` | Cosine between the posting and your profile summary |
| `competition` | How many people already applied |
| `freshness` | Days since publication |
| `salary` | Published range against your target |
| `seniority` | Fit / reach / no |

**Semantic layer.** Optional by construction. The posting and your profile
summary are embedded with a local `nomic-embed-text` through Ollama and
compared by cosine. If Ollama is not running, `resolve()` returns a
`NullEmbedder`, similarity comes back `None`, and scoring falls through to
keywords — `None` is deliberately distinct from `0.0`, so *not computed* is
never scored as *computed and unrelated*. Vectors are cached in SQLite; the
second run does not re-embed four hundred unchanged postings.

---

## Two things the tests caught

Both were design errors, not typos, and both came from running the thing
against real data.

**Competition is a count, not a rate.** The first version divided applications
by the posting's age. That ranked a posting published this morning with 92
applicants *below* a six-month-old one with 447 — because 92/day looks worse
than 2.7/day. But you compete against everyone whose CV is already in the pile,
not against the arrival rate. Staleness is a separate signal and `freshness`
already carries it.

**Stack has to saturate.** Raw keyword points were unbounded, so a posting
enumerating fourteen technologies scored ~35 while every other signal combined
topped out near 30. A dead, underpaid listing with 867 applicants outranked a
fresh one paying three times as much. Diminishing returns (`raw / (raw + k)`)
put stack on the same footing as everything else: matching ten of your terms is
clearly better than five, barely better than twenty.

Both are locked by regression tests named after the mistake.

---

## Layout

```
profile.toml        everything personal — stack, vetoes, weights, target pay
jobscan/api.py      Get on Board client, JSON:API flattened to a Job
jobscan/embed.py    Embedder port, Ollama adapter, null adapter, cosine
jobscan/scoring.py  knockouts + weighted score
jobscan/store.py    SQLite: what has been seen, cached vectors
jobscan/cli.py      orchestration and the markdown report
tests/              the filtering and ranking rules
```

The code holds no opinions about any particular person: the stack, the vetoes,
the weights and the prose summary all live in `profile.toml`. Point it at a
different profile and it ranks for someone else.

```bash
python tests/test_scoring.py      # or: python -m pytest tests/ -q
```

## Tuning

Run with `--explain` and read the breakdown. If postings you like sit below
ones you don't, the weights in `[scoring]` are wrong for you — that is the
knob, not the code. Terms in `[fit.stack]` are matched as plain substrings
against the whole posting, so `node` also matches `Node.js`.
