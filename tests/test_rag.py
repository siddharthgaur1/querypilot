"""Tests for the rag/ package: schema indexing/retrieval, history-store
similarity search, and the prompt builder's fallback behavior."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # rag/ lives at repo root, not under src/

from rag import history_store, schema_indexer
from rag.prompt_builder import build_prompt


@pytest.fixture
def temp_db_with_fk(tmp_path) -> Path:
    """Like conftest's `temp_db`, but accounts.customer_id has a real FOREIGN KEY
    constraint -- the shared fixture doesn't declare one, so relationship-chunk
    tests need their own DB where PRAGMA foreign_key_list has something to find."""
    import sqlite3
    db_path = tmp_path / "test_fk.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            city TEXT
        );
        INSERT INTO customers VALUES (1, 'Alice', 'Mumbai'), (2, 'Bob', 'Delhi');
        CREATE TABLE accounts (
            account_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
            balance REAL
        );
        INSERT INTO accounts VALUES (1, 1, 100.0), (2, 2, 50.0);
    """)
    conn.commit()
    conn.close()
    return db_path


class TestSchemaIndexer:
    def test_db_hash_is_stable_for_same_schema(self, temp_db):
        assert schema_indexer.compute_db_hash(temp_db) == schema_indexer.compute_db_hash(temp_db)

    def test_db_hash_differs_for_different_schema(self, temp_db, tmp_path):
        import sqlite3
        other = tmp_path / "other.db"
        conn = sqlite3.connect(other)
        conn.executescript("CREATE TABLE widgets (widget_id INTEGER PRIMARY KEY, name TEXT);")
        conn.commit()
        conn.close()
        assert schema_indexer.compute_db_hash(temp_db) != schema_indexer.compute_db_hash(other)

    def test_extract_schema_finds_foreign_key(self, temp_db_with_fk):
        tables = schema_indexer._extract_schema(temp_db_with_fk)
        assert "customers" in tables and "accounts" in tables
        fks = tables["accounts"]["foreign_keys"]
        assert any(fk["to_table"] == "customers" for fk in fks)

    def test_build_chunks_has_table_column_and_relationship_types(self, temp_db_with_fk):
        tables = schema_indexer._extract_schema(temp_db_with_fk)
        chunks = schema_indexer._build_chunks(tables)
        chunk_types = {c["metadata"]["chunk_type"] for c in chunks}
        assert chunk_types == {"table", "column", "relationship"}

    @pytest.mark.skipif(not schema_indexer.RAG_AVAILABLE, reason="chromadb/sentence-transformers not installed")
    def test_retrieve_schema_chunks_returns_relevant_table(self, temp_db_with_fk):
        chunks = schema_indexer.retrieve_schema_chunks(
            "what accounts does each customer have", temp_db_with_fk, top_k=4
        )
        assert len(chunks) > 0
        assert any(c["metadata"]["table_name"] == "accounts" for c in chunks)

    def test_retrieve_schema_chunks_empty_when_rag_unavailable(self, temp_db, monkeypatch):
        monkeypatch.setattr(schema_indexer, "RAG_AVAILABLE", False)
        assert schema_indexer.retrieve_schema_chunks("anything", temp_db) == []


class TestHistoryStore:
    @pytest.fixture(autouse=True)
    def _isolated_history_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history_store, "DB_PATH", tmp_path / "history.db")

    def test_round_trips_and_ranks_by_similarity(self):
        if not schema_indexer.RAG_AVAILABLE:
            pytest.skip("chromadb/sentence-transformers not installed")
        history_store.add_query("how many customers are there", "SELECT COUNT(*) FROM customers", True, 1.0, "h1")
        history_store.add_query("list all merchants", "SELECT * FROM merchants", True, 1.0, "h1")

        results = history_store.retrieve_similar("count of customers", "h1", top_k=3)
        assert len(results) == 2
        assert results[0]["question"] == "how many customers are there"  # closer match ranked first

    def test_failed_queries_are_not_retrievable(self):
        if not schema_indexer.RAG_AVAILABLE:
            pytest.skip("chromadb/sentence-transformers not installed")
        history_store.add_query("a bad question", "SELECT * FROM nonexistent", False, 0.0, "h1")
        assert history_store.retrieve_similar("a bad question", "h1") == []

    def test_scoped_by_db_hash(self):
        if not schema_indexer.RAG_AVAILABLE:
            pytest.skip("chromadb/sentence-transformers not installed")
        history_store.add_query("q1", "SELECT 1", True, 1.0, "db_a")
        assert history_store.retrieve_similar("q1", "db_b") == []

    def test_empty_history_returns_empty_list(self):
        assert history_store.retrieve_similar("anything", "no_such_hash") == []

    def test_retrieve_similar_never_raises_when_rag_unavailable(self, monkeypatch):
        monkeypatch.setattr(history_store, "RAG_AVAILABLE", False)
        history_store.add_query("q", "SELECT 1", True, 1.0, "h1")  # must not raise
        assert history_store.retrieve_similar("q", "h1") == []


class TestPromptBuilder:
    def test_falls_back_to_full_schema_when_retrieval_empty(self, temp_db, monkeypatch):
        monkeypatch.setattr("rag.prompt_builder.retrieve_schema_chunks", lambda *a, **k: [])
        result = build_prompt("any question", temp_db, fallback_schema="FULL SCHEMA TEXT HERE")
        assert result.used_fallback is True
        assert result.confidence == "low"
        assert "FULL SCHEMA TEXT HERE" in result.user_content

    def test_uses_retrieved_chunks_when_available(self, temp_db, monkeypatch):
        fake_chunks = [
            {"text": "chunk 1", "metadata": {"chunk_type": "table", "table_name": "customers"}},
            {"text": "chunk 2", "metadata": {"chunk_type": "table", "table_name": "accounts"}},
            {"text": "chunk 3", "metadata": {"chunk_type": "relationship", "table_name": "accounts"}},
            {"text": "chunk 4", "metadata": {"chunk_type": "column", "table_name": "customers"}},
        ]
        monkeypatch.setattr("rag.prompt_builder.retrieve_schema_chunks", lambda *a, **k: fake_chunks)
        monkeypatch.setattr("rag.prompt_builder.history_store.retrieve_similar", lambda *a, **k: [])
        result = build_prompt("any question", temp_db, fallback_schema="FULL SCHEMA TEXT HERE")
        assert result.used_fallback is False
        assert result.confidence == "high"
        assert "FULL SCHEMA TEXT HERE" not in result.user_content
        assert "chunk 1" in result.user_content

    @pytest.mark.parametrize("n_chunks,expected", [(4, "high"), (3, "medium"), (2, "medium"), (1, "low"), (0, "low")])
    def test_confidence_thresholds(self, n_chunks, expected):
        from rag.prompt_builder import _confidence
        assert _confidence(n_chunks) == expected

    def test_includes_few_shot_examples_when_present(self, temp_db, monkeypatch):
        fake_chunks = [{"text": "t", "metadata": {"chunk_type": "table", "table_name": "customers"}}]
        fake_history = [{"question": "past q", "sql": "SELECT 1", "similarity": 0.9}]
        monkeypatch.setattr("rag.prompt_builder.retrieve_schema_chunks", lambda *a, **k: fake_chunks)
        monkeypatch.setattr("rag.prompt_builder.history_store.retrieve_similar", lambda *a, **k: fake_history)
        result = build_prompt("any question", temp_db, fallback_schema="FULL SCHEMA")
        assert "past q" in result.user_content
        assert "SELECT 1" in result.user_content
        assert result.few_shot_examples == fake_history

    def test_no_few_shot_placeholder_when_history_empty(self, temp_db, monkeypatch):
        fake_chunks = [{"text": "t", "metadata": {"chunk_type": "table", "table_name": "customers"}}]
        monkeypatch.setattr("rag.prompt_builder.retrieve_schema_chunks", lambda *a, **k: fake_chunks)
        monkeypatch.setattr("rag.prompt_builder.history_store.retrieve_similar", lambda *a, **k: [])
        result = build_prompt("any question", temp_db, fallback_schema="FULL SCHEMA")
        assert "(none yet)" in result.user_content
