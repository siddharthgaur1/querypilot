"""Tests for src/agent.py — SQL safety validation, schema introspection,
SQL-fence extraction, history persistence, and the query timeout."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import agent
from agent import UnsafeQueryError, _execute, _extract_sql, get_schema, validate_sql

SHOULD_PASS = [
    "SELECT * FROM customers",
    "select name, city from customers where kyc_verified = 1",
    "WITH t AS (SELECT account_id, COUNT(*) c FROM transactions GROUP BY 1) SELECT * FROM t",
    "SELECT COUNT(*) AS n FROM transactions WHERE status = 'failed'",
    "SELECT * FROM customers ORDER BY created_at DESC LIMIT 10;",
]

SHOULD_FAIL = [
    "DROP TABLE customers",
    "DELETE FROM transactions WHERE 1=1",
    "UPDATE accounts SET balance = 999999",
    "INSERT INTO customers VALUES (1,'x','x','x','x',1,'low','2024-01-01')",
    "SELECT * FROM customers; DROP TABLE customers",
    "PRAGMA writable_schema = ON",
    "CREATE TABLE evil (x)",
    "ALTER TABLE customers ADD COLUMN hacked TEXT",
    "select * from customers; delete from accounts",
    "",
]


class TestValidateSql:
    @pytest.mark.parametrize("sql", SHOULD_PASS)
    def test_allows_safe_select_queries(self, sql):
        validate_sql(sql)  # must not raise

    @pytest.mark.parametrize("sql", SHOULD_FAIL)
    def test_blocks_unsafe_queries(self, sql):
        with pytest.raises(UnsafeQueryError):
            validate_sql(sql)

    def test_strips_trailing_semicolon(self):
        assert validate_sql("SELECT 1;") == "SELECT 1"


class TestExtractSql:
    def test_strips_markdown_fence_with_language_tag(self):
        raw = "```sql\nSELECT * FROM customers\n```"
        assert _extract_sql(raw) == "SELECT * FROM customers"

    def test_strips_markdown_fence_without_language_tag(self):
        raw = "```\nSELECT 1\n```"
        assert _extract_sql(raw) == "SELECT 1"

    def test_passes_through_plain_sql_unchanged(self):
        assert _extract_sql("SELECT 1") == "SELECT 1"


class TestGetSchema:
    def test_includes_table_ddl_and_sample_rows(self, temp_db):
        schema = get_schema(temp_db)
        assert "CREATE TABLE customers" in schema
        assert "Alice" in schema or "'Alice'" in schema


class TestExecuteTimeout:
    def test_runaway_query_is_interrupted(self, temp_db, monkeypatch):
        monkeypatch.setattr(agent, "QUERY_TIMEOUT_S", 0.5)
        # Recursive CTE with a huge bound — takes far longer than 0.5s unless interrupted.
        sql = "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x < 100000000) SELECT COUNT(*) FROM cnt"
        with pytest.raises(sqlite3.OperationalError, match="timeout"):
            _execute(sql, db_path=temp_db)

    def test_fast_query_returns_normally(self, temp_db):
        columns, rows, elapsed_ms = _execute("SELECT * FROM customers", db_path=temp_db)
        assert columns == ["customer_id", "name", "city"]
        assert len(rows) == 2
        assert elapsed_ms >= 0


class TestReadOnlyEnforcement:
    """The authorizer is the real guard: it denies writes even when the SQL never
    went through validate_sql, i.e. a bypass of the friendly regex layer."""

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE customers SET name='x'",
            "DROP TABLE customers",
            "INSERT INTO customers DEFAULT VALUES",
            "PRAGMA writable_schema = ON",
            "ATTACH DATABASE 'evil.db' AS e",
        ],
    )
    def test_writes_are_denied_at_the_engine(self, temp_db, sql):
        conn = agent._connect_readonly(temp_db)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
        finally:
            conn.close()

    def test_reads_still_work(self, temp_db):
        conn = agent._connect_readonly(temp_db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2
        finally:
            conn.close()

    def test_table_allowlist_denies_off_list_tables(self, temp_db, monkeypatch):
        monkeypatch.setattr(agent, "ALLOWED_TABLES", frozenset({"customers"}))
        conn = agent._connect_readonly(temp_db)
        try:
            conn.execute("SELECT * FROM customers").fetchall()  # allowed
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("SELECT * FROM accounts").fetchall()  # off-list
        finally:
            conn.close()


class TestHistory:
    def test_round_trips_through_json(self, tmp_path, monkeypatch):
        history_file = tmp_path / "history.json"
        monkeypatch.setattr(agent, "HISTORY_FILE", history_file)

        assert agent.load_history() == []
        agent.save_history([{"question": "how many customers?", "sql": "SELECT COUNT(*) FROM customers"}])
        loaded = agent.load_history()
        assert len(loaded) == 1
        assert loaded[0]["question"] == "how many customers?"

    def test_keeps_only_last_200_entries(self, tmp_path, monkeypatch):
        history_file = tmp_path / "history.json"
        monkeypatch.setattr(agent, "HISTORY_FILE", history_file)

        agent.save_history([{"question": f"q{i}"} for i in range(250)])
        loaded = agent.load_history()
        assert len(loaded) == 200
        assert loaded[0]["question"] == "q50"  # oldest 50 dropped

    def test_missing_history_file_returns_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "HISTORY_FILE", tmp_path / "does_not_exist.json")
        assert agent.load_history() == []
