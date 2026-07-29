"""Persistent store of past question -> SQL pairs, embedded so similar past
questions can be retrieved as few-shot examples for a new question.

Separate from `agent.HISTORY_FILE` (the JSON file backing the existing "Query
History" UI tab) — that one is a display log; this one is a retrieval index
keyed by db_hash and scoped to successful queries, since only SQL that actually
ran is worth showing the LLM as an example.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np

from .schema_indexer import RAG_AVAILABLE, embed_texts

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "query_rag_history.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            question        TEXT NOT NULL,
            generated_sql   TEXT NOT NULL,
            success         INTEGER NOT NULL,
            execution_time  REAL,
            db_hash         TEXT NOT NULL,
            embedding       BLOB,
            ts              TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def add_query(question: str, sql: str, success: bool, execution_time: float, db_hash: str) -> None:
    """Best-effort: only successful queries get an embedding (they're the only
    ones worth retrieving as examples), and any embedding failure is swallowed —
    losing one history row must never break the calling query."""
    embedding_blob = None
    if success and RAG_AVAILABLE:
        try:
            vec = np.array(embed_texts([question])[0], dtype=np.float32)
            embedding_blob = vec.tobytes()
        except Exception:
            embedding_blob = None

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO query_history (question, generated_sql, success, execution_time, db_hash, embedding, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (question, sql, int(success), execution_time, db_hash, embedding_blob,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def retrieve_similar(question: str, db_hash: str, top_k: int = 3) -> list[dict]:
    """Top-k most similar past *successful* questions for this database, by
    cosine similarity. Returns [] if RAG is unavailable or there's no history
    yet — never raises, so prompt_builder can call it unconditionally."""
    if not RAG_AVAILABLE:
        return []

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT question, generated_sql, embedding FROM query_history "
            "WHERE db_hash = ? AND success = 1 AND embedding IS NOT NULL",
            (db_hash,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    try:
        query_vec = np.array(embed_texts([question])[0], dtype=np.float32)
    except Exception:
        return []

    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return []

    scored = []
    for past_question, past_sql, blob in rows:
        vec = np.frombuffer(blob, dtype=np.float32)
        denom = query_norm * np.linalg.norm(vec)
        similarity = float(np.dot(query_vec, vec) / denom) if denom > 0 else 0.0
        scored.append({"question": past_question, "sql": past_sql, "similarity": similarity})

    scored.sort(key=lambda r: r["similarity"], reverse=True)
    return scored[:top_k]
