# Evaluation

The benchmark/evaluation suite for the ride-sharing analytics lab.

## Files

| File                | Purpose                                                            |
| ------------------- | ------------------------------------------------------------------ |
| `benchmark.json`    | 20 natural-language questions with expected kind and ground truth. |
| `run_benchmark.py`  | Run the benchmark via the offline stub or the LLM-backed skill.    |

## Run

```bash
# Offline (no LLM needed)
python -m evaluation.run_benchmark

# LLM-backed (requires Puku / OpenAI-compatible key)
python -m evaluation.run_benchmark --use-llm

# Save detailed run
python -m evaluation.run_benchmark --output runs/bench-2026-08-12.json
```

## Metrics

For each question we record:

| Metric              | What it checks                                |
| ------------------- | --------------------------------------------- |
| `intent_understanding` | `analysis.semantic_kind` matches `expected_kind` |
| `sql_safety`        | SQL passes the validator (or skill returns error) |
| `execution`         | Query executed without runtime error          |
| `row_count_ok`      | `row_count >= expected_min_rows`              |

The metrics roughly mirror section 22 of the README:

```
SQL correctness:        18/20
Query execution:       20/20
Visualization:         17/20
Dashboard creation:    20/20
```

The "Visualization" / "Dashboard creation" parts are tracked by the
dashboard skill's own tests (`tools/tests/`).
