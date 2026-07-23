# Security

## Threat model

QueryPilot turns untrusted natural language into SQL and runs it against a
database. Two things are untrusted: the user's question (which steers what SQL an
LLM writes) and, by extension, the generated SQL itself. The database must be
treated as something the user can *read* through a narrow slit and can **never
write to, alter, or escape from** — regardless of what SQL the model produces or
what a user types to try to jailbreak it.

Assumed trusted: the operator, the schema, the committed example queries.
Assumed untrusted: the user's question and every byte of generated SQL.

## Defense in depth

Four independent layers, so no single bypass reaches the data destructively:

| Layer | Mechanism | Where |
|---|---|---|
| 1. Statement shape | `validate_sql`: single statement only, must start `SELECT`/`WITH`, forbidden-keyword regex | `src/agent.py:63` |
| 2. **Read-only engine** | `PRAGMA query_only = ON` — SQLite itself refuses every write | `src/agent.py` (`_connect_readonly`) |
| 3. **SQLite authorizer** | Per-access callback: only `SELECT`/`READ`/`FUNCTION`/`RECURSIVE` allowed; everything else (INSERT/UPDATE/DELETE/DDL/ATTACH/PRAGMA) denied *at prepare time* | `src/agent.py` (`_authorizer`) |
| 4. Table allow-list | Optional `QUERYPILOT_ALLOWED_TABLES` scopes reads to named tables via the authorizer | `src/agent.py` (`ALLOWED_TABLES`) |
| Timeout | Wall-clock cap via `set_progress_handler`, not just the lock timeout | `src/agent.py:_execute` |
| Row cap | `fetchmany(MAX_ROWS)` — a `SELECT *` on a huge table cannot exhaust memory | `src/agent.py:_execute` |

### Why layers 2–4 matter more than layer 1

Layer 1 (the regex) is string matching, and string matching on SQL is defeatable
(comments, unusual whitespace, keywords in identifiers). It exists for **friendly,
early rejection with a clear message**, not as the security boundary. The real
boundary is the SQLite **authorizer** (layer 3): it runs *inside* SQLite while the
statement is prepared, sees the actual parsed operation and table, and denies
anything that is not a read. It cannot be talked around by clever SQL text because
it does not look at text — it looks at the compiled operation.

This is regression-tested: `tests/test_agent.py::TestReadOnlyEnforcement` feeds
writes (`UPDATE`, `DROP`, `INSERT`, `PRAGMA writable_schema`, `ATTACH`) **directly
to the connection, bypassing `validate_sql` entirely**, and asserts every one is
denied at the engine. The table allow-list is tested the same way.

## What is mitigated

- **SQL injection / destructive SQL** — layers 2 and 3; confirmed by tests that
  bypass the regex.
- **DDL / DML** (write, alter, drop, truncate) — denied by `query_only` and the
  authorizer.
- **`ATTACH`** (reading other database files) — denied by the authorizer.
- **Multi-statement / stacked queries** — rejected by `validate_sql`; a second
  statement would also be a fresh authorized prepare.
- **Runaway queries** — wall-clock timeout.
- **Memory exhaustion via large result sets** — row cap.
- **Secrets in history** — `gitleaks`: 0 findings; `.env` and `*.db` gitignored.
- **Dependency CVEs** — `pip-audit`: none; versions pinned.

## What is NOT mitigated

- **No authentication.** Single-operator / demo tool. Anyone who can reach the
  Streamlit port can query the database (read-only).
- **Prompt injection into the generated SQL.** A crafted question can make the LLM
  write *a different read query* than intended (e.g. reading a table you did not
  mean to expose). The mitigation is the optional table allow-list — set
  `QUERYPILOT_ALLOWED_TABLES` when pointing at anything real. Injection cannot
  produce a *write*, because writes are impossible on this connection.
- **The demo DB is synthetic** and contains no real personal data. If you point
  QueryPilot at a real database, the read-only guarantee holds, but you are
  responsible for the table allow-list and for not exposing sensitive columns.

## Reporting

Open an issue. Portfolio/demo project, no production deployment, no security SLA.
