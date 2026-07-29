"""RAG layer for QueryPilot: schema-aware retrieval + few-shot query history.

Public entry point for callers outside this package is `prompt_builder.build_prompt`;
the other modules (`schema_indexer`, `history_store`) are implementation details it
composes. Everything here degrades gracefully — if chromadb/sentence-transformers
aren't installed or retrieval fails for any reason, `prompt_builder` falls back to
the old full-schema-in-prompt behavior rather than raising.
"""

from .prompt_builder import build_prompt

__all__ = ["build_prompt"]
