"""Tests for visualization selection rules.

Run with: ``python3 -m tests.test_viz``
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.schemas import AnalysisHints, Column, ColumnType, QueryResult
from tools.dashboard import pick_display, viz_settings


def make_result(*, columns, rows, hints: AnalysisHints) -> QueryResult:
    return QueryResult(
        question="dummy",
        sql="SELECT 1;",
        columns=[Column(name=n, type=t) for n, t in columns],
        rows=rows,
        row_count=len(rows),
        analysis=hints,
    )


def settings_for_bar(result: QueryResult) -> Any:
    return viz_settings(result, "bar")["graph.dimension"]


def settings_for_number(result: QueryResult) -> Any:
    return viz_settings(result, "number")["scalar.field"]


def main() -> int:
    cases = [
        # KPI: total revenue
        (
            make_result(
                columns=[("revenue", ColumnType.FLOAT)],
                rows=[[12345.67]],
                hints=AnalysisHints(measures=["revenue"], is_single_value=True, semantic_kind="kpi"),
            ),
            "number",
        ),
        # Trend: rides per day
        (
            make_result(
                columns=[("day", ColumnType.DATETIME), ("ride_count", ColumnType.INTEGER)],
                rows=[["2026-01-01", 100]],
                hints=AnalysisHints(time_dimensions=["day"], measures=["ride_count"], semantic_kind="trend"),
            ),
            "line",
        ),
        # Top N: top destinations
        (
            make_result(
                columns=[("destination", ColumnType.STRING), ("ride_count", ColumnType.INTEGER)],
                rows=[["Gulshan", 18234], ["Dhanmondi", 16321]],
                hints=AnalysisHints(dimensions=["destination"], measures=["ride_count"], semantic_kind="top_n"),
            ),
            "bar",
        ),
        # Rating distribution
        (
            make_result(
                columns=[("rating", ColumnType.INTEGER), ("count", ColumnType.INTEGER)],
                rows=[[1, 5], [2, 23], [3, 50]],
                hints=AnalysisHints(dimensions=["rating"], measures=["count"], semantic_kind="rating_dist"),
            ),
            "bar",
        ),
        # Breakdown table
        (
            make_result(
                columns=[("a", ColumnType.STRING), ("b", ColumnType.STRING), ("c", ColumnType.FLOAT)],
                rows=[["x", "y", 1.0]],
                hints=AnalysisHints(dimensions=["a","b"], measures=["c"], semantic_kind="breakdown"),
            ),
            "table",
        ),
        # Cancellation rate KPI
        (
            make_result(
                columns=[("cancellation_rate", ColumnType.FLOAT)],
                rows=[[0.12]],
                hints=AnalysisHints(measures=["cancellation_rate"], is_single_value=True, semantic_kind="kpi"),
            ),
            "number",
        ),
    ]

    failures = 0
    for result, expected in cases:
        got = pick_display(result)
        ok = got == expected
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] expected={expected} got={got} question={result.question!r}")
        if not ok:
            failures += 1

    # Also confirm viz_settings produces non-empty settings for non-table views
    bar_result = cases[2][0]
    settings = viz_settings(bar_result, "bar")
    assert "graph.dimension" in settings, settings
    assert "graph.metrics" in settings, settings
    print(f"  [OK] viz_settings(bar) -> {settings}")

    number_result = cases[0][0]
    settings = viz_settings(number_result, "number")
    assert "scalar.field" in settings, settings
    print(f"  [OK] viz_settings(number) -> {settings}")

    # ----------------------------------------------------------------------
    # New: column-ref shape (Metabase v0.50+ requires tuples, not strings)
    # ----------------------------------------------------------------------
    # Bar on a string dim + int metric should be a tuple.
    bar_dim = settings_for_bar(bar_result)
    assert isinstance(bar_dim, list) and len(bar_dim) == 3, bar_dim
    assert bar_dim[0] == "field" and bar_dim[1] == "destination", bar_dim
    assert bar_dim[2] == {"base-type": "type/Text"}, bar_dim
    print(f"  [OK] bar graph.dimension is a column-ref tuple: {bar_dim}")

    # Bar on int dim (rating distribution) — this is the previously broken case.
    rating = cases[3][0]
    rs = viz_settings(rating, "bar")
    assert "graph.dimension" in rs, rs
    assert isinstance(rs["graph.dimension"], list), rs
    # _BASE_TYPE maps INTEGER/FLOAT both to type/Number (per the
    # _BASE_TYPE comment, this is the numeric fallback Metabase
    # accepts on all numeric X/Y axis bindings).
    assert rs["graph.dimension"][2] == {"base-type": "type/Number"}, rs
    assert "graph.metrics" in rs and rs["graph.metrics"], rs
    print(f"  [OK] rating-distribution bar binds int dim as column-ref: {rs['graph.dimension']}")

    # Trend (line) on datetime dim — column-ref must have type/DateTime.
    trend = cases[1][0]
    ts = viz_settings(trend, "line")
    assert ts["graph.dimension"][2] == {"base-type": "type/DateTime"}, ts
    print(f"  [OK] trend graph.dimension is type/DateTime: {ts['graph.dimension']}")

    # KPI scalar.field is a column-ref.
    ns = settings_for_number(number_result)
    assert isinstance(ns, list) and ns[2] == {"base-type": "type/Number"}, ns
    assert ns[0] == "field", ns
    print(f"  [OK] scalar.field is a column-ref tuple: {ns}")

    # Cancellation rate KPI gets percent formatting.
    cancel = cases[5][0]
    cs = viz_settings(cancel, "number")
    # column_settings keys are JSON-encoded MBQL clauses, not bare names.
    import json as _json
    expected_key = _json.dumps(["field", "cancellation_rate", {"base-type": "type/Number"}])
    assert expected_key in cs["column_settings"], cs
    assert cs["column_settings"][expected_key]["number_style"] == "percent", cs
    print(f"  [OK] cancellation_rate gets percent formatting")

    # ----------------------------------------------------------------------
    # New: arbitrary-query shape decisions (no curated hints)
    # ----------------------------------------------------------------------
    # Two string cols + one numeric → still bar (the first string is dim).
    arbitrary_two_str = make_result(
        columns=[("region", ColumnType.STRING), ("category", ColumnType.STRING),
                 ("revenue", ColumnType.FLOAT)],
        rows=[["Asia", "retail", 1200.0]],
        hints=AnalysisHints(),  # no semantic_kind
    )
    d = pick_display(arbitrary_two_str)
    assert d == "bar", d
    s = viz_settings(arbitrary_two_str, d)
    assert s["graph.dimension"][1] == "region", s
    print(f"  [OK] arbitrary (2-string + numeric) -> bar, dim={s['graph.dimension'][1]}")

    # Two numeric cols + no categorical → scatter.
    arbitrary_two_num = make_result(
        columns=[("fare", ColumnType.FLOAT), ("tip", ColumnType.FLOAT)],
        rows=[[10.0, 1.5]],
        hints=AnalysisHints(),
    )
    d = pick_display(arbitrary_two_num)
    assert d == "scatter", d
    s = viz_settings(arbitrary_two_num, d)
    assert "graph.metrics" in s and len(s["graph.metrics"]) >= 1, s
    print(f"  [OK] arbitrary (2 numeric) -> scatter, metrics={s['graph.metrics']}")

    # Datetime + numeric, no kind → line via shape.
    arbitrary_trend = make_result(
        columns=[("created_at", ColumnType.DATETIME), ("amount", ColumnType.FLOAT)],
        rows=[["2026-01-01", 100.0]],
        hints=AnalysisHints(),
    )
    d = pick_display(arbitrary_trend)
    assert d == "line", d
    print(f"  [OK] arbitrary (datetime + numeric) -> line")

    # Single integer column, single row, no hints → number (KPI).
    arbitrary_kpi = make_result(
        columns=[("total", ColumnType.INTEGER)],
        rows=[[42]],
        hints=AnalysisHints(),
    )
    d = pick_display(arbitrary_kpi)
    assert d == "number", d
    s = viz_settings(arbitrary_kpi, d)
    assert isinstance(s["scalar.field"], list), s
    print(f"  [OK] arbitrary (single int, single row) -> number: {s['scalar.field']}")

    if failures:
        print(f"\n{failures} tests failed.")
        return 1
    print("\nAll viz tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())