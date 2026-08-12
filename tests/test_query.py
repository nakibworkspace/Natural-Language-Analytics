"""Smoke-test: run the 10 manual ground-truth queries from the README and
print the first row + row count. Asserts that read-only role can execute
each one.

Run with: ``python3 -m tests.test_query``
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.postgres import execute_sql  # noqa: E402


QUERIES = [
    ("Top destinations by ride count (last 30 days, top 10)",
     "SELECT l.name AS destination, COUNT(*) AS ride_count "
     "FROM rides r JOIN locations l ON l.id = r.destination_location_id "
     "WHERE r.requested_at >= NOW() - INTERVAL '30 days' "
     "GROUP BY l.name ORDER BY ride_count DESC LIMIT 10;"),

    ("Average fare by destination",
     "SELECT l.name, ROUND(AVG(r.fare)::numeric, 2) AS avg_fare "
     "FROM rides r JOIN locations l ON l.id = r.destination_location_id "
     "WHERE r.status = 'completed' GROUP BY l.name ORDER BY avg_fare DESC;"),

    ("Rides per day (last 30 days)",
     "SELECT date_trunc('day', requested_at) AS day, COUNT(*) AS ride_count "
     "FROM rides WHERE requested_at >= NOW() - INTERVAL '30 days' "
     "GROUP BY day ORDER BY day;"),

    ("Cancellation rate",
     "SELECT (COUNT(*) FILTER (WHERE status='cancelled'))::float / COUNT(*) AS cancellation_rate "
     "FROM rides;"),

    ("Average driver rating",
     "SELECT ROUND(AVG(rating)::numeric, 2) AS avg_rating FROM drivers;"),

    ("Revenue by destination",
     "SELECT l.name, SUM(r.fare)::numeric(12,2) AS revenue "
     "FROM rides r JOIN locations l ON l.id = r.destination_location_id "
     "WHERE r.status = 'completed' GROUP BY l.name ORDER BY revenue DESC;"),

    ("Busiest hours",
     "SELECT EXTRACT(HOUR FROM requested_at)::int AS hour, COUNT(*) AS ride_count "
     "FROM rides GROUP BY 1 ORDER BY 1;"),

    ("Top drivers by ride count",
     "SELECT d.code, COUNT(*) AS rides, ROUND(AVG(r.fare)::numeric, 2) AS avg_fare "
     "FROM rides r JOIN drivers d ON d.id = r.driver_id "
     "WHERE r.status='completed' GROUP BY d.code ORDER BY rides DESC LIMIT 10;"),

    ("Rating distribution",
     "SELECT rating, COUNT(*) AS count FROM reviews GROUP BY rating ORDER BY rating;"),

    ("Review counts over time",
     "SELECT date_trunc('day', created_at) AS day, COUNT(*) AS review_count "
     "FROM reviews GROUP BY day ORDER BY day;"),
]


def main() -> int:
    print("=== ground truth queries (read-only role) ===")
    failures = 0
    for title, sql in QUERIES:
        try:
            result = execute_sql(question=title, sql=sql)
            first = result.rows[0] if result.rows else None
            print(f"  [{','.join(c.name for c in result.columns)}] -> {result.row_count} rows; first={first}")
        except Exception as e:
            failures += 1
            print(f"  FAIL: {title}: {e}")
    if failures:
        print(f"\n{failures} queries failed.")
        return 1
    print("\nAll ground-truth queries executed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())