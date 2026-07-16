# QueryPilot

[![CI](https://github.com/siddharthgaur1/querypilot/actions/workflows/ci.yml/badge.svg)](https://github.com/siddharthgaur1/querypilot/actions/workflows/ci.yml) [![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Ad-hoc business questions against a database usually mean writing SQL or
waiting on whoever can. QueryPilot converts plain-English questions into
SQLite, runs them against a live schema (introspected, never hardcoded), and
summarizes the result — with a validation layer that makes it structurally
impossible for a generated query to write to or damage the database, even if
the LLM is adversarially prompted or just wrong.

## Architecture

```
User question
      │
      ▼
Schema introspection  ←── reads live DDL + sample rows from SQLite
      │
      ▼
LLM (Claude / Llama3.2)  →  raw SQL
      │
      ▼
validate_sql() ──── fail ──→ LLM self-correction (1 retry, then give up)
      │ pass
      ▼
_execute()  [PRAGMA query_only=ON, row cap, real wall-clock timeout]
      │
      ▼
Results + AI summary + auto-chart
```

## Tech stack

| Choice | Why |
|---|---|
| SQLite + `PRAGMA query_only=ON` | Every connection is read-only at the engine level — even a validator bypass can't produce a write, because SQLite itself refuses. |
| Blocklist + prefix check + engine-level read-only (3 layers, not 1) | A regex blocklist alone is guessable; a read-only connection alone gives no useful error message. Layering them means a bypass at one layer still fails safe at the next. |
| Claude, with Ollama fallback | Claude for SQL generation quality; Ollama lets it run fully offline/free for local dev without an API key. |
| SQLite over Postgres/MySQL for the demo DB | Zero setup for a portfolio project — the whole point is the NL→SQL→safety pipeline, not database administration. |
| Streamlit | Chat UI, schema browser, and chart/export in one file, no separate frontend. |

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # or install Ollama for a free local backend
python scripts/setup_db.py --rows 10000
```

## Running it

```bash
streamlit run src/app.py
pytest tests/ -v
```

## The safety layers

1. **Regex blocklist** — `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`,
   `CREATE`, `ATTACH`, `PRAGMA`, `VACUUM`, etc.
2. **Statement shape check** — only `SELECT` or `WITH` (CTE) queries pass.
3. **No semicolons** — blocks stacked statements
   (`SELECT ...; DROP TABLE ...`).
4. **Engine-level** — `PRAGMA query_only = ON` on every connection. This is
   the layer that actually matters: even if 1–3 were bypassed, SQLite itself
   refuses to execute a write on a read-only connection.
5. **Row cap** — configurable 50–1000 row limit via `fetchmany()`.
6. **Real query timeout** — `set_progress_handler()` interrupts a query that
   runs past `QUERY_TIMEOUT_S`. (See "bugs fixed" below — the original
   implementation of this layer didn't actually work.)

`tests/test_agent.py` exercises layers 1–3 directly (adapted from
`scripts/test_safety.py`, which still runs standalone) and layer 6 against a
real runaway recursive CTE, not just a mock.

## Bugs found and fixed during polish

- **The query timeout didn't do anything.** `sqlite3.connect(db_path,
  timeout=15)` sets sqlite3's *busy* timeout — how long to wait for a lock on
  a contended database — not a cap on how long a query itself may run. A
  pathological query (a recursive CTE, a big cross join) could run
  indefinitely despite the README claiming a 15-second timeout. Fixed with
  `conn.set_progress_handler()`, which SQLite calls periodically during
  execution and can use to interrupt a long-running query. Verified against
  an actual runaway recursive CTE, not just a timing assumption.
- **The Streamlit UI crashed on the first query.** `_render_chart()` and
  `_render_export()` were called before they were defined in the script —
  Python executes top-to-bottom, and Streamlit re-runs the whole script on
  every interaction, so this was a guaranteed `NameError` on the very first
  result. Moved the definitions above their call sites.

## What I'd improve with more time

1. **No prompt-injection testing.** The blocklist defends against the LLM
   generating a write; it doesn't defend against a user crafting a *question*
   designed to make the LLM emit a query that leaks data it shouldn't (e.g.
   phrasing that gets around a `WHERE` clause a real analyst would add). The
   engine-level read-only guarantee limits the damage to *read* leakage, but
   that's still worth testing explicitly.
2. **`EXPLAIN QUERY PLAN` isn't used to pre-empt slow queries** — it's only
   shown after the fact as an optional UI toggle. Running it before execution
   and rejecting queries with an obviously catastrophic plan (e.g. no index
   usage on a large table) would catch expensive queries before spending the
   full timeout budget on them.
3. **Self-correction retries only once** and doesn't distinguish "SQL syntax
   error" (worth retrying) from "the question is unanswerable with this
   schema" (retrying just burns another LLM call for the same failure).

## Related projects

- [llm-regression-detector](https://github.com/siddharthgaur1/llm-regression-detector) — same eval-gate pattern that would apply to NL→SQL accuracy regressions here.
- [finrag](https://github.com/siddharthgaur1/finrag) — hybrid RAG over financial PDFs.
- [rag-hybrid-search](https://github.com/siddharthgaur1/rag-hybrid-search) — hybrid dense+BM25 RAG pipeline.
- [ipo-gmp](https://github.com/siddharthgaur1/ipo-gmp) — XGBoost IPO listing-return predictor.
