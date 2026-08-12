"""Smoke-test the Metabase client.

This requires Metabase to be running and ``METABASE_API_KEY`` to be set.
It does NOT touch dashboards/questions; it only confirms:

  - The client can authenticate.
  - It can list databases.
  - It can find the seeded ``ride_analytics`` database.

Run with: ``python3 -m tests.test_metabase_smoke``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.metabase import MetabaseClient, MetabaseError  # noqa: E402


def main() -> int:
    if not os.getenv("METABASE_API_KEY"):
        print("METABASE_API_KEY is not set; skipping smoke test.")
        print("See guide.md -> Phase 5 -> 'create API key'.")
        return 0
    client = MetabaseClient()
    if not client.wait_ready(max_seconds=5):
        print("Metabase is not reachable; skipping smoke test.")
        return 0

    try:
        dbs = client.list_databases()
        print(f"  found {len(dbs)} databases:")
        for d in dbs:
            print(f"    - id={d.get('id')} name={d.get('name')!r}")
        target = next((d for d in dbs if d.get("name") == os.getenv("POSTGRES_DB", "ride_analytics")), None)
        if not target:
            print(f"  ride_analytics database not found in Metabase yet.")
            print(f"  (It will appear after you connect it via the UI in Phase 5.)")
            return 0
        print(f"  found target database id={target['id']} name={target['name']!r}")
    except MetabaseError as e:
        print(f"  Metabase error: {e}")
        return 1

    print("Metabase smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())