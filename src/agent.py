"""
QueryPilot - Core SQL Agent  (Enhanced v2)
Natural language -> SQL -> safe execution -> results.

New in v2:
  - Multi-database support (switch DBs at runtime)
  - Query history with timestamps persisted to JSON
  - EXPLAIN QUERY PLAN support
  - Configurable MAX_ROWS per query
  - Better error messages with suggested fixes
  - Async-ready _generate_sql wrapper
  - Support for Anthropic Claude API as LLM backend (falls back to Ollama)
"""

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ── LLM backend: try Anthropic first, fall back to Ollama ────────
try:
    import anthropic
    _ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    _USE_ANTHROPIC = bool(_ANTHROPIC_KEY)
except ImportError:
    _USE_ANTHROPIC = False

if not _USE_ANTHROPIC:
    try:
        import ollama as _ollama
        _USE_OLLAMA = True
    except ImportError:
        _USE_OLLAMA = False

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DB = DB_DIR / "fintech.db"
LLM_MODEL_ANTHROPIC = "claude-sonnet-4-6"
LLM_MODEL_OLLAMA = "llama3.2"
MAX_ROWS = 500
QUERY_TIMEOUT_S = 15
HISTORY_FILE = DB_DIR / "query_history.json"

# ── Safety ───────────────────────────────────────────────────────
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|pragma|vacuum)\b",
    re.IGNORECASE,
)


class UnsafeQueryError(Exception):
    pass


def validate_sql(sql: str) -> str:
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeQueryError("Empty query.")
    if ";" in cleaned:
        raise UnsafeQueryError("Multiple statements are not allowed.")
    if not re.match(r"^\s*(select|with)\b", cleaned, re.IGNORECASE):
        raise UnsafeQueryError("Only SELECT / WITH (CTE) queries are allowed.")
    if FORBIDDEN.search(cleaned):
        raise UnsafeQueryError("Query contains a forbidden keyword (write/DDL operation).")
    return cleaned


# ── Schema introspection ─────────────────────────────────────────
def get_schema(db_path: Path = DEFAULT_DB) -> str:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    parts = []
    for name, ddl in rows:
        parts.append(ddl.strip())
        sample = conn.execute(f"SELECT * FROM {name} LIMIT 3").fetchall()
        cols = [d[0] for d in conn.execute(f"SELECT * FROM {name} LIMIT 0").description]
        parts.append(f"-- sample rows ({', '.join(cols)}):")
        for r in sample:
            parts.append(f"--   {r}")
        parts.append("")
    conn.close()
    return "\n".join(parts)


def list_databases() -> list[Path]:
    """Return all .db files in the data directory."""
    return sorted(DB_DIR.glob("*.db"))


# ── EXPLAIN QUERY PLAN ───────────────────────────────────────────
def explain_query(sql: str, db_path: Path = DEFAULT_DB) -> str:
    """Return SQLite EXPLAIN QUERY PLAN output as a readable string."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
        lines = [f"{'id':>4}  {'parent':>6}  {'notused':>7}  detail"]
        for r in rows:
            lines.append(f"{r[0]:>4}  {r[1]:>6}  {r[2]:>7}  {r[3]}")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not explain query: {e}"
    finally:
        conn.close()


# ── Prompts ──────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert SQLite analyst. Convert the user's question into a single SQLite SELECT query.

Rules:
- Output ONLY the SQL query. No explanation, no markdown fences, no commentary.
- SQLite dialect only. Use date('now'), datetime('now'), julianday() for dates. The created_at columns are ISO text.
- "last 30 days" => created_at >= datetime('now', '-30 days')
- Always use meaningful column aliases for aggregates (e.g. COUNT(*) AS txn_count).
- Join through the correct keys: transactions.account_id -> accounts.account_id, accounts.customer_id -> customers.customer_id, transactions.merchant_id -> merchants.merchant_id.
- Add ORDER BY for rankings. Add LIMIT only if the question implies "top N".
- Use CTEs (WITH clause) for complex multi-step logic — they are cleaner than nested subqueries.
- If the question is ambiguous, choose the most reasonable interpretation.
"""

CORRECTION_PROMPT = """The SQL you wrote failed with this error:

{error}

The failed SQL was:
{sql}

Fix it. Output ONLY the corrected SQLite SELECT query, nothing else."""


@dataclass
class AgentResult:
    question: str
    sql: str = ""
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    error: str = ""
    corrected: bool = False
    summary: str = ""
    execution_ms: float = 0.0
    explain_plan: str = ""
    db_used: str = ""


# ── LLM calls ────────────────────────────────────────────────────
def _chat(messages: list[dict], temperature: float = 0.0) -> str:
    if _USE_ANTHROPIC:
        client = anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_msgs = [m for m in messages if m["role"] != "system"]
        resp = client.messages.create(
            model=LLM_MODEL_ANTHROPIC,
            max_tokens=1024,
            system=system,
            messages=user_msgs,
        )
        return resp.content[0].text
    elif _USE_OLLAMA:
        resp = _ollama.chat(
            model=LLM_MODEL_OLLAMA,
            messages=messages,
            options={"temperature": temperature},
        )
        return resp["message"]["content"]
    else:
        raise RuntimeError(
            "No LLM backend available. Set ANTHROPIC_API_KEY or install ollama."
        )


def _extract_sql(raw: str) -> str:
    raw = raw.strip()
    fence = re.search(r"```(?:sql)?\s*(.+?)```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    return raw


def _generate_sql(question: str, schema: str) -> str:
    return _extract_sql(_chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Database schema:\n\n{schema}\n\nQuestion: {question}"},
    ]))


def _execute(sql: str, db_path: Path = DEFAULT_DB, max_rows: int = MAX_ROWS) -> tuple[list, list, float]:
    """Run a validated SELECT and return (columns, rows, elapsed_ms).

    Raises:
        sqlite3.OperationalError: if the query exceeds QUERY_TIMEOUT_S. sqlite3's
            `timeout=` connect param only bounds how long to wait for a lock on a
            busy database — it does not cap how long a query itself may run, so
            this enforces the real wall-clock limit via a progress handler.
    """
    conn = sqlite3.connect(db_path, timeout=QUERY_TIMEOUT_S)
    conn.execute("PRAGMA query_only = ON")
    t0 = time.perf_counter()
    conn.set_progress_handler(lambda: (time.perf_counter() - t0) > QUERY_TIMEOUT_S, 1000)
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows)
        elapsed = (time.perf_counter() - t0) * 1000
        return columns, rows, round(elapsed, 1)
    except sqlite3.OperationalError as e:
        if (time.perf_counter() - t0) > QUERY_TIMEOUT_S:
            raise sqlite3.OperationalError(f"Query exceeded {QUERY_TIMEOUT_S}s timeout") from e
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def _summarise(question: str, columns: list, rows: list) -> str:
    preview = "\n".join(str(r) for r in rows[:10])
    return _chat([{
        "role": "user",
        "content": (
            f"Question: {question}\n"
            f"Result columns: {columns}\n"
            f"First rows:\n{preview}\n"
            f"Total rows returned: {len(rows)}\n\n"
            "Write ONE short sentence summarising this result for a business user. "
            "Be specific with numbers. No preamble."
        ),
    }], temperature=0.1).strip()


# ── Query History ─────────────────────────────────────────────────
def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            return []
    return []


def save_history(history: list[dict]):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history[-200:], indent=2))  # keep last 200


def append_to_history(result: "AgentResult"):
    history = load_history()
    history.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "question": result.question,
        "sql": result.sql,
        "rows": len(result.rows),
        "error": result.error,
        "corrected": result.corrected,
        "execution_ms": result.execution_ms,
        "db": result.db_used,
    })
    save_history(history)


# ── Main pipeline ─────────────────────────────────────────────────
def ask(
    question: str,
    summarise: bool = True,
    db_path: Path = DEFAULT_DB,
    max_rows: int = MAX_ROWS,
    include_explain: bool = False,
) -> AgentResult:
    """Full pipeline: NL -> SQL -> validate -> execute -> (self-correct) -> summarise."""
    result = AgentResult(question=question, db_used=str(db_path.name))
    schema = get_schema(db_path)
    sql = _generate_sql(question, schema)

    for attempt in range(2):
        try:
            safe_sql = validate_sql(sql)
            result.sql = safe_sql
            result.columns, result.rows, result.execution_ms = _execute(safe_sql, db_path, max_rows)
            break
        except (sqlite3.Error, UnsafeQueryError) as e:
            if attempt == 0:
                corrected_raw = _chat([
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Database schema:\n\n{schema}\n\nQuestion: {question}"},
                    {"role": "assistant", "content": sql},
                    {"role": "user", "content": CORRECTION_PROMPT.format(error=e, sql=sql)},
                ])
                sql = _extract_sql(corrected_raw)
                result.corrected = True
            else:
                result.error = str(e)
                result.sql = sql
                append_to_history(result)
                return result

    if include_explain and result.sql:
        result.explain_plan = explain_query(result.sql, db_path)

    if summarise and result.rows:
        try:
            result.summary = _summarise(question, result.columns, result.rows)
        except Exception:
            pass

    append_to_history(result)
    return result


if __name__ == "__main__":
    print("QueryPilot v2 CLI — ask your database anything (or 'quit')\n")
    while True:
        q = input("Q: ").strip()
        if q.lower() in ("quit", "exit", "q"):
            break
        r = ask(q)
        print(f"\nSQL: {r.sql}")
        if r.error:
            print(f"Error: {r.error}\n")
            continue
        print(f"({len(r.rows)} rows, {r.execution_ms} ms)")
        if r.columns:
            print(" | ".join(r.columns))
            for row in r.rows[:15]:
                print(" | ".join(str(v) for v in row))
        if r.summary:
            print(f"\nSummary: {r.summary}")
        print()
