"""Compare the old (full-schema-in-every-prompt) approach against the new RAG
approach (retrieved schema chunks + few-shot examples) on a fixed 20-question
test set covering single-table, joins, aggregation, subqueries, and ambiguous
column names.

Reports, per approach: exact-match %, execution-success %, avg estimated
prompt tokens, avg latency. Results saved to eval/results.json.

Usage:
    python eval/benchmark.py                # against agent.DEFAULT_DB
    python eval/benchmark.py --db path.db

Requires a working LLM backend (ANTHROPIC_API_KEY or a local Ollama daemon) --
every test case is run through the real backend under BOTH approaches, so this
is 2x the test-set size in LLM calls. Prefer Ollama (free, local) for repeated
runs; the Anthropic path costs real money per call.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402

TEST_CASES_FILE = Path(__file__).resolve().parent / "test_cases.json"
RESULTS_FILE = Path(__file__).resolve().parent / "results.json"


def _normalize_sql(sql: str) -> str:
    """Loose equality for exact-match scoring: collapse whitespace, drop the
    trailing semicolon, lowercase. Real SQL equivalence is undecidable in
    general (many correct rewrites exist) — this is deliberately strict, so
    exact-match is a lower bound and execution-success is the more meaningful
    correctness signal."""
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).strip().lower()


def _estimate_tokens(text: str) -> int:
    """Rough, tokenizer-free proxy (chars/4) -- avoids depending on a specific
    provider's tokenizer while still being a fair, consistent comparison
    between the two approaches (same estimator applied to both)."""
    return max(1, len(text) // 4)


def _run_case(question: str, db_path: Path, schema: str, approach: str) -> dict:
    t0 = time.perf_counter()
    if approach == "old_full_schema":
        prompt_text = f"Database schema:\n\n{schema}\n\nQuestion: {question}"
        sql = agent._generate_sql(question, schema)
    else:
        sql, prompt_result = agent._generate_sql_rag(question, schema, db_path)
        prompt_text = prompt_result.user_content
    latency = time.perf_counter() - t0

    exec_success = False
    try:
        safe_sql = agent.validate_sql(sql)
        agent._execute(safe_sql, db_path)
        exec_success = True
    except Exception:
        pass

    return {
        "sql": sql,
        "prompt_tokens_est": _estimate_tokens(prompt_text) + _estimate_tokens(agent.SYSTEM_PROMPT),
        "latency_s": round(latency, 3),
        "exec_success": exec_success,
    }


def run_benchmark(db_path: Path = agent.DEFAULT_DB, test_cases_file: Path = TEST_CASES_FILE) -> dict:
    if not agent.has_llm_backend():
        raise RuntimeError(
            "No LLM backend available (set ANTHROPIC_API_KEY or run a local Ollama "
            "daemon). This benchmark makes a real LLM call per test case per approach."
        )

    cases = json.loads(test_cases_file.read_text(encoding="utf-8"))
    schema = agent.get_schema(db_path)

    per_approach: dict[str, list[dict]] = {"old_full_schema": [], "new_rag": []}
    for case in cases:
        for approach in per_approach:
            r = _run_case(case["question"], db_path, schema, approach)
            r["question"] = case["question"]
            r["category"] = case["category"]
            r["expected_sql"] = case["expected_sql"]
            r["exact_match"] = _normalize_sql(r["sql"]) == _normalize_sql(case["expected_sql"])
            per_approach[approach].append(r)

    summary = {}
    for approach, results in per_approach.items():
        n = len(results)
        summary[approach] = {
            "n_cases": n,
            "exact_match_pct": round(100 * sum(r["exact_match"] for r in results) / n, 1),
            "execution_success_pct": round(100 * sum(r["exec_success"] for r in results) / n, 1),
            "avg_prompt_tokens_est": round(sum(r["prompt_tokens_est"] for r in results) / n, 1),
            "avg_latency_s": round(sum(r["latency_s"] for r in results) / n, 3),
        }

    output = {"summary": summary, "per_case": per_approach}
    RESULTS_FILE.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=agent.DEFAULT_DB)
    args = parser.parse_args()
    out = run_benchmark(args.db)
    print(json.dumps(out["summary"], indent=2))
