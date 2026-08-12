"""Tests for ``tools.query.find_sql`` regex pattern coverage.

The PATTERNS list in ``tools/query.py`` is the offline NL→SQL stub. These
tests pin down the canonical question phrasings (and a few common
variations) so that future refactors don't silently regress a phrasing
that a user has come to rely on.

Run with: ``python3 -m tests.test_pattern_matching``
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.query import find_sql  # noqa: E402


# (phrasing, expected substring in the rendered SQL, optional analysis
# semantic_kind check). ``find_sql`` returns (sql, AnalysisHints).
CASES: list[tuple[str, str, str | None]] = [
    # Top destinations — must accept multiple natural phrasings
    (
        "Show me the top 10 destinations by number of rides in the last 30 days.",
        "GROUP BY l.name",
        "top_n",
    ),
    (
        "Top 10 destinations by rides in last 30 days",
        "ORDER BY ride_count DESC",
        "top_n",
    ),
    (
        "Top destinations by count",
        "GROUP BY l.name",
        "top_n",
    ),
    (
        "Most popular destinations",
        "ORDER BY ride_count DESC",
        "top_n",
    ),
    # Busiest hours
    (
        "What are the busiest hours?",
        "EXTRACT(HOUR FROM requested_at)",
        "distribution",
    ),
    (
        "rides by hour",
        "EXTRACT(HOUR FROM requested_at)",
        "distribution",
    ),
    # Rating distribution
    (
        "Show me the rating distribution",
        "FROM reviews GROUP BY rating",
        "rating_dist",
    ),
    # Top drivers
    (
        "Top 5 drivers",
        "GROUP BY d.code",
        "top_n",
    ),
    # Cancellation rate (KPI)
    (
        "What is the cancellation rate?",
        "cancellation_rate",
        "kpi",
    ),
    # Average driver rating
    (
        "average driver rating",
        "AVG(rating)",
        "kpi",
    ),
]


def main() -> int:
    failures = 0
    for phrasing, expected_sql_substr, expected_kind in CASES:
        result = find_sql(phrasing)
        if result is None:
            print(f"  [FAIL] {phrasing!r}: not matched")
            failures += 1
            continue
        sql, hints = result
        if expected_sql_substr not in sql:
            print(f"  [FAIL] {phrasing!r}: SQL does not contain {expected_sql_substr!r}")
            print(f"         got: {sql!r}")
            failures += 1
            continue
        if expected_kind and hints.semantic_kind != expected_kind:
            print(f"  [FAIL] {phrasing!r}: semantic_kind={hints.semantic_kind!r} expected {expected_kind!r}")
            failures += 1
            continue
        print(f"  [OK]   {phrasing!r:65s} -> {hints.semantic_kind!r}")
    if failures:
        print(f"\n{failures} pattern-matching test(s) failed.")
        return 1
    print(f"\nAll {len(CASES)} pattern-matching tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
