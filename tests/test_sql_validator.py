"""Tests for the deterministic SQL validator.

Run with: ``python3 -m tests.test_sql_validator``
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.sql_validator import SQLValidationError, validate_sql


CASES_OK = [
    "SELECT 1;",
    "SELECT * FROM rides;",
    "  SELECT id, name FROM locations;",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x;",
    "SELECT COUNT(*) FROM rides;",
    "SELECT l.name, COUNT(*) AS c FROM rides r JOIN locations l ON l.id = r.destination_location_id GROUP BY 1;",
]

CASES_REJECT = [
    ("", "Empty SQL"),
    ("   ", "Empty SQL"),
    ("DROP TABLE rides;", "DDL"),
    ("DELETE FROM rides;", "DML"),
    ("UPDATE rides SET fare = 0;", "DML"),
    ("INSERT INTO riders (code) VALUES ('x');", "DML"),
    ("TRUNCATE rides;", "DDL"),
    ("ALTER TABLE rides ADD COLUMN x INT;", "DDL"),
    ("GRANT ALL ON rides TO public;", "DDL"),
    ("CREATE TABLE foo (id int);", "DDL"),
    ("SELECT 1; DROP TABLE rides;", "multiple"),
    ("EXPLAIN SELECT 1;", "EXPLAIN forbidden"),
    ("COPY rides FROM '/etc/passwd';", "COPY forbidden"),
    ("VACUUM;", "DDL"),
    ("SELECT 1 -- inline", "comment-only"),
]


def main() -> int:
    print("=== test_sql_validator: positive cases ===")
    for sql in CASES_OK:
        out = validate_sql(sql)
        assert out, f"validator returned empty for {sql!r}"
        print(f"  OK: {sql[:60]!r}")

    print("=== test_sql_validator: negative cases ===")
    for sql, why in CASES_REJECT:
        try:
            validate_sql(sql)
        except SQLValidationError as e:
            print(f"  REJECTED ({why}): {e!s}  (input: {sql[:40]!r})")
            continue
        print(f"  !!! NOT REJECTED: {sql!r} ({why})")
        return 1

    print("\nAll validator tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())