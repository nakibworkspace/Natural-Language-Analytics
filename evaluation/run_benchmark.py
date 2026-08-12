"""Run the benchmark suite against the Query Skill.

For each question in ``benchmark.json`` this script:

  1. Calls the Query Skill (LLM-backed or offline stub).
  2. Validates the SQL safety (``SQLValidationError`` -> FAIL).
  3. Checks intent understanding (``expected_kind`` vs ``analysis.semantic_kind``).
  4. Executes the SQL and reports ``row_count`` vs ``expected_min_rows``.
  5. (Optional) Compares the result against the ground-truth execution.

Usage:
    python -m evaluation.run_benchmark                       # offline stub
    python -m evaluation.run_benchmark --use-llm             # LLM-backed
    python -m evaluation.run_benchmark --output runs/x.json  # save detail
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.postgres import execute_sql  # noqa: E402
from shared.schemas import QueryResult  # noqa: E402


def run_offline(benchmark: list[dict]) -> tuple[dict, dict]:
    from tools.query import find_sql, annotate_from_question
    out = []
    for q in benchmark:
        entry: dict[str, Any] = {"id": q["id"], "question": q["question"]}
        match = find_sql(q["question"])
        if not match:
            entry.update(understanding=False, sql_valid=False, executed=False, sql=None)
            out.append(entry)
            continue
        sql, _ = match
        entry["sql"] = sql
        try:
            result = execute_sql(question=q["question"], sql=sql)
        except Exception as e:
            entry.update(understanding=True, sql_valid=False, executed=False, error=str(e))
            out.append(entry)
            continue
        result.analysis = annotate_from_question(q["question"], [c.name for c in result.columns])
        entry.update(
            understanding=(result.analysis.semantic_kind == q["expected_kind"]),
            sql_valid=True,
            executed=True,
            row_count=result.row_count,
            min_rows_expected=q["expected_min_rows"],
            row_count_ok=(result.row_count >= q["expected_min_rows"]),
            kind=result.analysis.semantic_kind,
        )
        out.append(entry)
    return summarize(out), {"runs": out}


def run_llm(benchmark: list[dict]) -> tuple[dict, dict]:
    """Use the LLM-backed run_query. Requires a configured LLM client."""
    from tools.query import run_with_llm as run
    out = []
    for q in benchmark:
        entry: dict[str, Any] = {"id": q["id"], "question": q["question"]}
        try:
            result = run(q["question"])
        except Exception as e:
            entry.update(understanding=False, sql_valid=False, executed=False, error=str(e))
            out.append(entry)
            continue
        if result.analysis.semantic_kind == "error":
            entry.update(understanding=False, sql_valid=False, executed=False, sql=result.sql)
            out.append(entry)
            continue
        entry.update(
            understanding=(result.analysis.semantic_kind == q["expected_kind"]),
            sql_valid=True,
            executed=True,
            row_count=result.row_count,
            min_rows_expected=q["expected_min_rows"],
            row_count_ok=(result.row_count >= q["expected_min_rows"]),
            kind=result.analysis.semantic_kind,
            sql=result.sql,
        )
        out.append(entry)
    return summarize(out), {"runs": out}


def summarize(runs: list[dict]) -> dict:
    n = len(runs)
    understood = sum(1 for r in runs if r.get("understanding"))
    sql_valid  = sum(1 for r in runs if r.get("sql_valid"))
    executed   = sum(1 for r in runs if r.get("executed"))
    rc_ok      = sum(1 for r in runs if r.get("row_count_ok"))
    return {
        "total": n,
        "intent_understanding": f"{understood}/{n}",
        "sql_safety":           f"{sql_valid}/{n}",
        "execution":            f"{executed}/{n}",
        "row_count_ok":         f"{rc_ok}/{n}",
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--use-llm", action="store_true",
                   help="Use the LLM-backed run_query instead of the offline stub")
    p.add_argument("--limit", type=int, default=0, help="Limit to N questions")
    p.add_argument("--output", type=str, default="", help="Save detailed run output")
    p.add_argument("--benchmark", type=str, default=str(ROOT / "evaluation" / "benchmark.json"))
    args = p.parse_args(argv)

    with open(args.benchmark) as f:
        benchmark = json.load(f)
    if args.limit:
        benchmark = benchmark[: args.limit]

    summary, detail = (run_llm if args.use_llm else run_offline)(benchmark)

    print("\n=== Benchmark summary ===")
    for k, v in summary.items():
        print(f"  {k:24s} {v}")
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump({"summary": summary, "detail": detail}, f, indent=2, default=str)
        print(f"\nDetailed output written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())