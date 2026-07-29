"""Schema-aware indexing: turn a SQLite database's structure into retrievable
ChromaDB chunks (per-table schema+samples, per-table column descriptions, and
foreign-key join hints), so the prompt builder can pull in only what's relevant
to a given question instead of the entire schema every time.

Indexing is cached by `db_hash` (a hash of the schema DDL, not the data) — the
same database re-opened later reuses its existing collection instead of
re-embedding on every run.
"""

from __future__ import annotations

import hashlib
import sqlite3
from functools import lru_cache
from pathlib import Path

try:
    import chromadb
    from sentence_transformers import SentenceTransformer

    RAG_AVAILABLE = True
except ImportError:
    RAG_AVAILABLE = False

CHROMA_DIR = Path(__file__).resolve().parent.parent / "data" / "chroma"
EMBED_MODEL_NAME = "all-MiniLM-L6-v2"


@lru_cache(maxsize=1)
def _get_embedder() -> "SentenceTransformer":
    return SentenceTransformer(EMBED_MODEL_NAME)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Shared embedder for both schema chunks and query-history few-shot lookup."""
    return _get_embedder().encode(list(texts), convert_to_numpy=True).tolist()


@lru_cache(maxsize=1)
def _get_client() -> "chromadb.ClientAPI":
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(CHROMA_DIR))


def compute_db_hash(db_path: Path) -> str:
    """Hash of the schema DDL (table definitions), not the data — two databases
    with identical structure but different rows share an index; a schema change
    (new column, new table) gets a fresh hash and is re-indexed automatically."""
    conn = sqlite3.connect(db_path)
    try:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    text = "\n".join(row[0] or "" for row in ddl)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _extract_schema(db_path: Path) -> dict:
    """Introspect tables, columns, foreign keys, and sample rows via SQLite PRAGMAs
    (not DDL regex parsing — PRAGMA foreign_key_list is the engine's own view of
    the relationships and handles quoting/formatting variance DDL text won't)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tables = {}
    try:
        table_names = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        for table in table_names:
            columns = [
                {"name": r[1], "type": r[2], "pk": bool(r[5])}
                for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            ]
            fks = [
                {"from_col": r[3], "to_table": r[2], "to_col": r[4]}
                for r in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            ]
            col_names = [c["name"] for c in columns]
            samples = conn.execute(f"SELECT * FROM {table} LIMIT 3").fetchall()
            tables[table] = {
                "columns": columns,
                "foreign_keys": fks,
                "samples": [tuple(row) for row in samples],
                "col_names": col_names,
            }
    finally:
        conn.close()
    return tables


def _build_chunks(tables: dict) -> list[dict]:
    """One document per table (schema + samples), one per table's column
    descriptions, and one per foreign-key relationship — three chunk_types."""
    chunks = []

    for table, info in tables.items():
        col_lines = "\n".join(
            f"  {c['name']} ({c['type']}){' PRIMARY KEY' if c['pk'] else ''}" for c in info["columns"]
        )
        sample_lines = "\n".join(f"  {row}" for row in info["samples"])
        text = (
            f"Table: {table}\n"
            f"Columns:\n{col_lines}\n"
            f"Sample rows ({', '.join(info['col_names'])}):\n{sample_lines}"
        )
        chunks.append({
            "id": f"table::{table}",
            "text": text,
            "metadata": {"table_name": table, "chunk_type": "table"},
        })

        col_desc = ", ".join(f"{table}.{c['name']} ({c['type']})" for c in info["columns"])
        chunks.append({
            "id": f"column::{table}",
            "text": f"Column descriptions for {table}: {col_desc}",
            "metadata": {"table_name": table, "chunk_type": "column"},
        })

        for fk in info["foreign_keys"]:
            text = f"{table} joins {fk['to_table']} on {table}.{fk['from_col']} = {fk['to_table']}.{fk['to_col']}"
            chunks.append({
                "id": f"relationship::{table}::{fk['from_col']}::{fk['to_table']}",
                "text": text,
                "metadata": {"table_name": table, "chunk_type": "relationship"},
            })

    return chunks


def index_schema(db_path: Path, force: bool = False) -> str:
    """Index `db_path` into its ChromaDB collection (schema_{db_hash}), skipping
    re-embedding if it's already populated. Returns the db_hash used as the
    collection key. Raises if chromadb/sentence-transformers aren't installed —
    callers (prompt_builder) are responsible for catching that and falling back."""
    if not RAG_AVAILABLE:
        raise RuntimeError("chromadb / sentence-transformers not installed")

    db_hash = compute_db_hash(db_path)
    client = _get_client()
    collection = client.get_or_create_collection(f"schema_{db_hash}")

    if not force and collection.count() > 0:
        return db_hash

    tables = _extract_schema(db_path)
    chunks = _build_chunks(tables)
    if not chunks:
        return db_hash

    embeddings = embed_texts([c["text"] for c in chunks])
    collection.upsert(
        ids=[c["id"] for c in chunks],
        documents=[c["text"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
        embeddings=embeddings,
    )
    return db_hash


def retrieve_schema_chunks(question: str, db_path: Path, top_k: int = 4) -> list[dict]:
    """Top-k schema chunks relevant to `question`, filtered to this database's
    collection. Returns [] (not an exception) if nothing is indexed yet or the
    collection is empty — prompt_builder treats an empty list as "fall back"."""
    if not RAG_AVAILABLE:
        return []

    db_hash = index_schema(db_path)
    client = _get_client()
    collection = client.get_or_create_collection(f"schema_{db_hash}")
    if collection.count() == 0:
        return []

    query_embedding = embed_texts([question])[0]
    result = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, collection.count()),
    )
    chunks = []
    for doc, meta, dist in zip(result["documents"][0], result["metadatas"][0], result["distances"][0]):
        chunks.append({"text": doc, "metadata": meta, "distance": dist})
    return chunks
