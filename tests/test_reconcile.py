"""Tests for _reconcile_base_types in tools/dashboard.py.

Regression coverage for the "Which fields do you want to use for the
X and Y axes?" bug. Postgres COUNT(*)/SUM(*) emit BigInteger, but our
Pydantic ColumnType.INTEGER can't tell that apart from a plain INTEGER.
Metabase v0.50 requires the ``base-type`` on a column-ref to match
``result_metadata[].base_type`` *exactly*, so the dashboard skill has
to read result_metadata back and substitute the real base types.
"""

from __future__ import annotations

import json
import unittest

from tools.dashboard import _reconcile_base_types


def _ref(name: str, base: str) -> list:
    return ["field", name, {"base-type": base}]


def _key(name: str, base: str) -> str:
    return json.dumps(_ref(name, base))


class ReconcileBaseTypesTests(unittest.TestCase):
    def test_replaces_dimension_and_metrics(self) -> None:
        settings = {
            "graph.dimension": _ref("hour_of_day", "type/Integer"),
            "graph.metrics": [_ref("ride_count", "type/Integer")],
        }
        meta = [
            {"name": "hour_of_day", "base_type": "type/Integer"},
            {"name": "ride_count",  "base_type": "type/BigInteger"},
        ]
        fixed = _reconcile_base_types(settings, meta)
        self.assertEqual(fixed["graph.dimension"], _ref("hour_of_day", "type/Integer"))
        self.assertEqual(fixed["graph.metrics"], [_ref("ride_count", "type/BigInteger")])

    def test_fixes_scalar_field(self) -> None:
        settings = {"scalar.field": _ref("total", "type/Integer")}
        meta = [{"name": "total", "base_type": "type/BigInteger"}]
        fixed = _reconcile_base_types(settings, meta)
        self.assertEqual(fixed["scalar.field"], _ref("total", "type/BigInteger"))

    def test_rekey_column_settings(self) -> None:
        old_key = _key("ride_count", "type/Integer")
        new_key = _key("ride_count", "type/BigInteger")
        settings = {
            "graph.metrics": [_ref("ride_count", "type/Integer")],
            "column_settings": {old_key: {"number_style": "decimal"}},
        }
        meta = [{"name": "ride_count", "base_type": "type/BigInteger"}]
        fixed = _reconcile_base_types(settings, meta)
        self.assertNotIn(old_key, fixed["column_settings"])
        self.assertIn(new_key, fixed["column_settings"])
        self.assertEqual(fixed["column_settings"][new_key], {"number_style": "decimal"})

    def test_noop_when_metadata_empty(self) -> None:
        settings = {"graph.dimension": _ref("x", "type/Integer")}
        self.assertEqual(_reconcile_base_types(settings, []), settings)

    def test_preserves_unknown_columns(self) -> None:
        # When a column isn't in result_metadata, keep the original base-type.
        settings = {"graph.dimension": _ref("ghost", "type/Text")}
        fixed = _reconcile_base_types(settings, [{"name": "other", "base_type": "type/Integer"}])
        self.assertEqual(fixed["graph.dimension"], _ref("ghost", "type/Text"))


if __name__ == "__main__":
    unittest.main()
