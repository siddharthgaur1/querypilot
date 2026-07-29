"""Assembles the RAG-enhanced NL->SQL prompt: retrieve relevant schema chunks +
similar past queries, then build the user-message content that replaces the old
"dump the entire schema in the prompt" approach.

Graceful fallback: if schema retrieval comes back empty (chromadb missing,
sentence-transformers missing, nothing indexed yet, or any exception along the
way), this falls back to the caller-supplied full schema text — same behavior
as before this feature existed. Callers should always get a usable prompt back,
never an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import history_store
from .schema_indexer import compute_db_hash, retrieve_schema_chunks

PROMPT_TEMPLATE = """You are an expert SQL assistant. Use ONLY the schema provided.

Relevant schema:
{schema_section}

Similar past queries for reference:
{few_shot_section}

Question: {question}
Generate a SQLite-compatible SQL query."""

NO_FEW_SHOT_TEXT = "(none yet)"


@dataclass
class PromptResult:
    user_content: str
    retrieved_chunks: list[dict] = field(default_factory=list)
    few_shot_examples: list[dict] = field(default_factory=list)
    used_fallback: bool = False
    db_hash: str = ""
    confidence: str = "low"  # high (>=4 chunks) / medium (2-3) / low (<2)


def _confidence(n_chunks: int) -> str:
    if n_chunks >= 4:
        return "high"
    if n_chunks >= 2:
        return "medium"
    return "low"


def _format_schema_chunks(chunks: list[dict]) -> str:
    return "\n\n".join(c["text"] for c in chunks)


def _format_few_shot(examples: list[dict]) -> str:
    if not examples:
        return NO_FEW_SHOT_TEXT
    return "\n\n".join(f"Q: {e['question']}\nSQL: {e['sql']}" for e in examples)


def build_prompt(
    question: str,
    db_path: Path,
    fallback_schema: str,
    top_k_schema: int = 4,
    top_k_history: int = 3,
) -> PromptResult:
    """fallback_schema: the full-schema text (agent.get_schema output) the caller
    already has on hand, used verbatim when retrieval doesn't return anything —
    keeps this module from needing to re-implement schema introspection or import
    back into agent.py (which would create a circular import)."""
    try:
        db_hash = compute_db_hash(db_path)
    except Exception:
        db_hash = ""

    try:
        chunks = retrieve_schema_chunks(question, db_path, top_k=top_k_schema)
    except Exception:
        chunks = []

    try:
        few_shot = history_store.retrieve_similar(question, db_hash, top_k=top_k_history) if db_hash else []
    except Exception:
        few_shot = []

    if not chunks:
        user_content = PROMPT_TEMPLATE.format(
            schema_section=fallback_schema,
            few_shot_section=_format_few_shot(few_shot),
            question=question,
        )
        return PromptResult(
            user_content=user_content,
            retrieved_chunks=[],
            few_shot_examples=few_shot,
            used_fallback=True,
            db_hash=db_hash,
            confidence="low",
        )

    user_content = PROMPT_TEMPLATE.format(
        schema_section=_format_schema_chunks(chunks),
        few_shot_section=_format_few_shot(few_shot),
        question=question,
    )
    return PromptResult(
        user_content=user_content,
        retrieved_chunks=chunks,
        few_shot_examples=few_shot,
        used_fallback=False,
        db_hash=db_hash,
        confidence=_confidence(len(chunks)),
    )
