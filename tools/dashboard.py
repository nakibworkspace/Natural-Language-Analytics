"""Dashboard skill — turn one or more QueryResults into a Metabase dashboard.

Public entry points:

  * ``build_dashboard(results, name)``         — create or update a dashboard.
  * ``render_answer(question, result)``        — 1-line text answer.
  * ``run(question)``                          — convenience: run the Query
                                                 skill then build a dashboard.

CLI::

    # 1. Pure dashboard from a pre-computed JSON file:
    python3 tools/dashboard.py /tmp/q.json "My dashboard"

    # 2. End-to-end (NL question → answer + dashboard URL):
    python3 tools/dashboard.py --include-answer "traffics in farmgate"

    # 3. Custom name:
    python3 tools/dashboard.py --include-answer "traffic in banani" "Banani trip volume"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.models import DashboardHandle, VisualizationKind
from shared.schemas import Column, ColumnType, QueryResult
from tools.metabase import MetabaseClient, MetabaseError
from tools.query import run as query_run

# Metabase DB that docker-compose provisions via init.
DEFAULT_DATABASE_NAME = os.getenv("POSTGRES_DB", "ride_analytics")


# ---------------------------------------------------------------------------
# Visualization selection (was skills/dashboard_skill/tools/viz.py)
# ---------------------------------------------------------------------------
# Two design axes:
#   1. Pick by **data shape**, not by name, so arbitrary queries work.
#   2. Emit Metabase **column-ref tuples**, not bare names.

_BASE_TYPE = {
    ColumnType.STRING:   "type/Text",
    # Metabase accepts both type/Integer and type/BigInteger, but Postgres
    # COUNT(*)/SUM(*) emit BigInteger which doesn't match type/Integer.
    # Use type/Number for all numeric shapes — Metabase treats that as the
    # numeric fallback for binding X/Y axes.
    ColumnType.INTEGER:  "type/Number",
    ColumnType.FLOAT:    "type/Number",
    ColumnType.DATE:     "type/Date",
    ColumnType.DATETIME: "type/DateTime",
    ColumnType.BOOLEAN:  "type/Boolean",
    ColumnType.UNKNOWN:  "type/*",
}


def _base_type(column_type: ColumnType) -> str:
    return _BASE_TYPE.get(column_type, "type/*")


def _col_ref(col: Column) -> list:
    """Metabase column-ref: ``["field", col.name, {"base-type": ...}]``."""
    return ["field", col.name, {"base-type": _base_type(col.type)}]


def _col_refs(cols: list[Column]) -> list[list]:
    return [_col_ref(c) for c in cols]


# Tier 1: explicit semantic_kind from AnalysisHints.
_TIER1_HINT = {
    "kpi":          VisualizationKind.NUMBER,
    "trend":        VisualizationKind.LINE,
    "top_n":        VisualizationKind.BAR,
    "rating_dist":  VisualizationKind.BAR,
    "distribution": VisualizationKind.BAR,
    "breakdown":    VisualizationKind.TABLE,
    "table":        VisualizationKind.TABLE,
    "raw":          VisualizationKind.TABLE,
}


def _classify_columns(cols: list[Column]) -> tuple[list[Column], list[Column], list[Column], list[Column]]:
    string_cols: list[Column] = []
    numeric_cols: list[Column] = []
    time_cols:    list[Column] = []
    other_cols:   list[Column] = []
    for c in cols:
        if c.type == ColumnType.STRING:
            string_cols.append(c)
        elif c.type in (ColumnType.INTEGER, ColumnType.FLOAT):
            numeric_cols.append(c)
        elif c.type in (ColumnType.DATE, ColumnType.DATETIME):
            time_cols.append(c)
        else:
            other_cols.append(c)
    return string_cols, numeric_cols, time_cols, other_cols


def pick_display(result: QueryResult) -> str:
    """Pick a Metabase ``display`` string for ``result``.

    3-tier decision: explicit single-value → curated semantic_kind →
    data shape. The shape tier makes the layer work for arbitrary queries
    the LLM may emit, not just the curated 11 patterns.
    """
    cols = result.columns
    if not cols:
        return VisualizationKind.TABLE.value

    if result.analysis.is_single_value:
        return VisualizationKind.NUMBER.value

    kind = (result.analysis.semantic_kind or "").lower()
    if kind in _TIER1_HINT:
        return _TIER1_HINT[kind].value

    string_cols, numeric_cols, time_cols, _ = _classify_columns(cols)
    n_rows = result.row_count

    if len(cols) == 1 and numeric_cols and n_rows <= 1:
        return VisualizationKind.NUMBER.value

    if time_cols and numeric_cols:
        return VisualizationKind.LINE.value

    if (string_cols or (numeric_cols and not time_cols)) and numeric_cols:
        if not string_cols and len(numeric_cols) >= 2 and not time_cols:
            return VisualizationKind.SCATTER.value
        return VisualizationKind.BAR.value

    return VisualizationKind.TABLE.value


# Substrings that hint a column is monetary vs percent.
_CURRENCY_HINTS = ("fare", "revenue", "price", "amount", "cost", "income", "earning")
_PERCENT_HINTS  = ("rate", "ratio", "percent", "percentage", "share", "pct")


def _format_hint_for_column(col: Column) -> dict[str, Any]:
    name_l = col.name.lower()
    if col.type in (ColumnType.INTEGER, ColumnType.FLOAT):
        s: dict[str, Any] = {}
        if any(h in name_l for h in _PERCENT_HINTS):
            s["number_style"] = "percent"
            s["decimals"] = 2
        elif any(h in name_l for h in _CURRENCY_HINTS):
            s["number_style"] = "currency"
            s["currency"] = "USD"
            s["decimals"] = 2
        elif col.type == ColumnType.INTEGER:
            s["number_style"] = "decimal"
            s["decimals"] = 0
        else:
            s["number_style"] = "decimal"
            s["decimals"] = 2
        return s
    if col.type in (ColumnType.DATE, ColumnType.DATETIME):
        return {"date_style": "MMM D, YYYY"}
    return {}


def _column_settings(cols: list[Column]) -> dict[str, Any]:
    cs: dict[str, Any] = {}
    for c in cols:
        hint = _format_hint_for_column(c)
        if hint:
            key = json.dumps(_col_ref(c))
            cs[key] = hint
    return cs


def viz_settings(result: QueryResult, display: str) -> dict[str, Any]:
    """Build ``visualization_settings`` for a given display kind.

    Always returns a *non-empty* dict with axis bindings (or table.column
    list) so Metabase never asks the user to pick fields.
    """
    cols = result.columns
    string_cols, numeric_cols, time_cols, _ = _classify_columns(cols)
    col_settings = _column_settings(cols)

    settings: dict[str, Any] = {}
    if col_settings:
        settings["column_settings"] = col_settings

    if display == VisualizationKind.NUMBER.value:
        if numeric_cols:
            settings["scalar.field"] = _col_ref(numeric_cols[0])
        return settings

    if display == VisualizationKind.TABLE.value:
        settings["table.columns"] = [{"name": c.name, "enabled": True} for c in cols]
        return settings

    if not (string_cols or numeric_cols or time_cols):
        return settings

    # Dimension: time > string > numeric (first numeric used as dim).
    dim: Column | None = None
    if time_cols:
        dim = time_cols[0]
    elif string_cols:
        dim = string_cols[0]
    elif numeric_cols:
        dim = numeric_cols[0]
        numeric_cols = numeric_cols[1:]

    if dim is not None:
        settings["graph.dimension"] = _col_ref(dim)

    metrics = [c for c in numeric_cols if not (dim is not None and c.name == dim.name)]
    if metrics:
        settings["graph.metrics"] = _col_refs(metrics[:3])

    if display == VisualizationKind.BAR.value and dim is not None and dim.type == ColumnType.STRING:
        settings["graph.x_axis.scale"] = "ordinal"

    return settings


def plan(result: QueryResult) -> tuple[str, dict[str, Any]]:
    return pick_display(result), viz_settings(result, pick_display(result))


# ---------------------------------------------------------------------------
# Dashboard build (was skills/dashboard_skill/tools/build_dashboard.py)
# ---------------------------------------------------------------------------
def _layout_cards(card_ids: list[int], per_row: int = 3) -> list[dict]:
    """Simple row-major grid layout, 6 wide, 4 tall per card."""
    placed = []
    for i, cid in enumerate(card_ids):
        row, col = divmod(i, per_row)
        placed.append({"cardId": cid, "row": row * 4, "col": col * 6, "sizeX": 6, "sizeY": 4})
    return placed


def _reconcile_base_types(
    settings: dict[str, Any], result_metadata: list[dict[str, Any]]
) -> dict[str, Any]:
    """Rebuild viz_settings using the exact ``base_type`` from Metabase.

    Postgres ``COUNT(*)``/``SUM(*)`` emit BigInteger, but our Pydantic
    ``ColumnType.INTEGER`` can't tell that apart from a plain INTEGER.
    Metabase v0.50 requires the ``base-type`` on a column-ref to match
    ``result_metadata[].base_type`` *exactly* — otherwise the UI shows
    "Which fields do you want to use for the X and Y axes?". So after
    card creation we fetch the real metadata and substitute types.
    """
    if not result_metadata:
        return settings

    name_to_base: dict[str, str] = {}
    for entry in result_metadata:
        name = entry.get("name")
        base = entry.get("base_type")
        if name and base:
            name_to_base[name] = base

    def _fix(ref: Any) -> Any:
        if not isinstance(ref, list) or len(ref) != 3 or ref[0] != "field":
            return ref
        col_name, meta = ref[1], ref[2] if isinstance(ref[2], dict) else {}
        real_base = name_to_base.get(col_name, meta.get("base-type"))
        if not real_base:
            return ref
        new_meta = dict(meta)
        new_meta["base-type"] = real_base
        return ["field", col_name, new_meta]

    fixed = dict(settings)
    for key in ("graph.dimension", "graph.metrics", "scalar.field"):
        if key not in fixed:
            continue
        val = fixed[key]
        if isinstance(val, list) and val and isinstance(val[0], list):
            fixed[key] = [_fix(r) for r in val]
        else:
            fixed[key] = _fix(val)

    cs = fixed.get("column_settings")
    if isinstance(cs, dict):
        new_cs: dict[str, Any] = {}
        for k, v in cs.items():
            try:
                parsed = json.loads(k)
            except (TypeError, ValueError):
                new_cs[k] = v
                continue
            fixed_ref = _fix(parsed)
            new_cs[json.dumps(fixed_ref)] = v
        fixed["column_settings"] = new_cs

    return fixed


def _build_card(client: MetabaseClient, db_id: int, result: QueryResult, title: str) -> dict:
    display = pick_display(result)
    settings = viz_settings(result, display)
    card = client.create_question(
        name=title,
        database_id=db_id,
        sql=result.sql,
        display=display,
        visualization_settings=settings,
        description=result.question,
    )
    print(f"[dashboard] created card id={card['id']} display={display!r} title={title!r}", file=sys.stderr)

    # Reconcile base_types against Metabase's result_metadata. Without
    # this, COUNT(*) (BigInteger) columns get bound as type/Integer and
    # Metabase shows "Which fields?". We MUST run the card once so
    # result_metadata is populated, then PUT the corrected settings.
    try:
        client.run_card(card["id"])
        refreshed = client.get_question(card["id"])
        meta = refreshed.get("result_metadata") or []
        if meta:
            fixed = _reconcile_base_types(settings, meta)
            client.update_question_visualization(
                card_id=card["id"],
                display=display,
                visualization_settings=fixed,
            )
            print(
                f"[dashboard] reconciled base_types for card id={card['id']} "
                f"({[m.get('name') + ':' + str(m.get('base_type')) for m in meta]})",
                file=sys.stderr,
            )
        else:
            print(f"[dashboard] no result_metadata for card id={card['id']}; skipping reconcile", file=sys.stderr)
    except MetabaseError as e:
        print(f"[dashboard] reconcile skipped for card id={card['id']}: {e}", file=sys.stderr)

    return card


def build_dashboard(
    query_results: list[QueryResult],
    dashboard_name: str,
    description: str | None = None,
    database_name: str = DEFAULT_DATABASE_NAME,
    client: MetabaseClient | None = None,
) -> DashboardHandle:
    """Build (or update) a Metabase dashboard from one or more query results.

    Idempotent: if a dashboard with ``dashboard_name`` already exists, the
    new cards are appended (no deletion).
    """
    client = client or MetabaseClient()
    if not client.wait_ready(max_seconds=10):
        raise MetabaseError("Metabase is not reachable; check METABASE_URL and container health.")

    db_id = client.find_database_id(database_name)
    print(f"[dashboard] using Metabase database id={db_id} ({database_name!r})", file=sys.stderr)

    card_ids: list[int] = []
    for i, r in enumerate(query_results, start=1):
        title = r.question if r.question else f"Card {i}"
        card = _build_card(client, db_id, r, title)
        card_ids.append(card["id"])

    existing = client.find_dashboard_by_name(dashboard_name)
    if existing:
        dash = existing
        print(f"[dashboard] reusing existing dashboard id={dash['id']} name={dashboard_name!r}", file=sys.stderr)
    else:
        dash = client.create_dashboard(
            name=dashboard_name,
            description=description or "Auto-generated by the ride-sharing AI analytics lab",
        )
        print(f"[dashboard] created dashboard id={dash['id']} name={dashboard_name!r}", file=sys.stderr)

    placement = _layout_cards(card_ids)
    for p in placement:
        client.add_card_to_dashboard(
            dashboard_id=dash["id"],
            card_id=p["cardId"],
            row=p["row"], col=p["col"],
            size_x=p["sizeX"], size_y=p["sizeY"],
        )

    # Re-layout every dashcard on this dashboard and patch any cards
    # whose viz_settings still bind the wrong base-type. Idempotent:
    # if everything is already correct this is a no-op (the rebuild
    # produces identical positions, and the patcher skips cards whose
    # settings already match result_metadata).
    heal_dashboard(client, dash["id"])

    handle = DashboardHandle(
        dashboard_id=dash["id"], name=dash["name"], url=client.dashboard_url(dash["id"])
    )
    print(f"[dashboard] DONE -> {handle.url}", file=sys.stderr)
    return handle


def _cleanup_dashboard_layout(client: MetabaseClient, dashboard_id: int) -> None:
    """One-shot helper: rebuild dashcard positions in-place.

    Older cards created before ``_reconcile_base_types`` exist may still
    have stale viz_settings. Also, repeated runs of ``build_dashboard``
    used to append a new dashcard at ``row=0 col=0`` each time, leaving
    duplicates stacked on top of each other. This rebuilds the layout
    so each existing dashcard has a unique cell.

    Safe to call multiple times.

    Note: Metabase v0.50 keeps ``visualization_settings`` on the dashcard
    too (not just on the card). When the dashcard's copy is empty, the
    dashboard renderer sometimes shows "Which fields?" even though the
    card itself is fine. We mirror the card's settings onto the dashcard
    so the renderer has explicit bindings to read.
    """
    dash = client._request("GET", f"/dashboard/{dashboard_id}")
    existing = dash.get("dashcards") or []
    if not existing:
        return

    by_card = {}
    for dc in existing:
        cid = dc.get("card_id")
        if cid is not None and cid not in by_card:
            by_card[cid] = dc

    unique_card_ids = list(by_card.keys())
    placement = _layout_cards(unique_card_ids)

    rebuilt = []
    for dc, p in zip(list(by_card.values()), placement):
        cid = dc.get("card_id")
        dashcard_viz = dc.get("visualization_settings") or {}
        card_viz: dict = {}
        if cid is not None:
            try:
                card = client.get_question(cid)
                card_viz = card.get("visualization_settings") or {}
            except MetabaseError:
                pass
        # Mirror card viz onto dashcard if dashcard's is empty (or
        # missing key fields). This forces Metabase's dashboard
        # renderer to use the reconciled settings.
        merged_viz = dict(dashcard_viz) if dashcard_viz else {}
        # Always-required axis bindings (these are what triggers the
        # "Which fields?" prompt if missing).
        for k in ("graph.dimension", "graph.metrics", "scalar.field", "table.columns"):
            if not merged_viz.get(k) and card_viz.get(k):
                merged_viz[k] = card_viz[k]
        # Other viz knobs (x-axis scale, binning, show_values, colors,
        # column_settings) — copy from card if dashcard is missing them.
        # Without these, Metabase silently falls back to defaults that
        # can render empty charts even when the axis bindings are right.
        for k in (
            "graph.x_axis.scale",
            "graph.dimension_binning",
            "graph.show_values",
            "graph.label_value_frequency",
            "graph.colors",
            "column_settings",
        ):
            if merged_viz.get(k) is None and card_viz.get(k) is not None:
                merged_viz[k] = card_viz[k]

        rebuilt.append({
            "id": dc["id"],
            "card_id": dc["card_id"],
            "row": p["row"],
            "col": p["col"],
            "size_x": p["sizeX"],
            "size_y": p["sizeY"],
            "visualization_settings": merged_viz,
        })
    client._request("PUT", f"/dashboard/{dashboard_id}", {"dashcards": rebuilt})
    print(
        f"[dashboard] re-layout {len(rebuilt)} dashcard(s) on dashboard id={dashboard_id}",
        file=sys.stderr,
    )


def _patch_stale_cards(client: MetabaseClient, dashboard_id: int) -> None:
    """Patch viz_settings on every card on this dashboard so it renders.

    Cards created before ``_reconcile_base_types`` landed have
    ``type/Integer`` bindings for BigInteger columns (COUNT(*), SUM(*))
    and Metabase shows "Which fields?" on them. Running ``run_card``
    populates ``result_metadata``; ``_reconcile_base_types`` then
    substitutes the exact base types; the card is PUT back.

    No-op for cards that are already correct.
    """
    dash = client._request("GET", f"/dashboard/{dashboard_id}")
    card_ids = {dc.get("card_id") for dc in dash.get("dashcards") or [] if dc.get("card_id") is not None}
    for cid in card_ids:
        try:
            client.run_card(cid)
            card = client.get_question(cid)
            meta = card.get("result_metadata") or []
            if not meta:
                continue
            current = card.get("visualization_settings") or {}
            fixed = _reconcile_base_types(current, meta)
            if fixed != current:
                client.update_question_visualization(
                    card_id=cid,
                    display=card.get("display") or "table",
                    visualization_settings=fixed,
                )
                print(f"[dashboard] patched card id={cid}", file=sys.stderr)
        except MetabaseError as e:
            print(f"[dashboard] skip card id={cid}: {e}", file=sys.stderr)


def heal_dashboard(client: MetabaseClient, dashboard_id: int) -> None:
    """Public one-shot helper: fix layout + patch stale cards on a dashboard."""
    _cleanup_dashboard_layout(client, dashboard_id)
    _patch_stale_cards(client, dashboard_id)


# ---------------------------------------------------------------------------
# Answer rendering (was .puku-cli/skills/ask/tools/orchestrator.py)
# ---------------------------------------------------------------------------
def _format_value(col_name: str, value) -> str:
    if value is None:
        return "N/A"
    name_l = col_name.lower()
    if isinstance(value, (int, float)):
        if any(h in name_l for h in _PERCENT_HINTS):
            return f"{value * 100:.2f}%"
        if any(h in name_l for h in _CURRENCY_HINTS):
            return f"${value:,.2f}"
        if isinstance(value, float):
            return f"{value:,.2f}"
        return f"{value:,}"
    return str(value)


def _format_row(cols, row) -> str:
    return ", ".join(f"{c.name}={_format_value(c.name, v)}" for c, v in zip(cols, row))


def _coerce_number(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _ymd(v) -> str:
    try:
        return _dt.fromisoformat(str(v).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError):
        return str(v)


def render_answer(question: str, result: QueryResult) -> str:
    """Turn a QueryResult into a 1-sentence natural-language answer."""
    cols = result.columns
    rows = result.rows
    analysis = result.analysis
    kind = (analysis.semantic_kind or "").lower()

    if result.row_count == 1 and len(cols) == 1:
        return f"The {cols[0].name.replace('_', ' ')} is {_format_value(cols[0].name, rows[0][0])}."

    if analysis.is_single_value and rows:
        return f"The {cols[0].name.replace('_', ' ')} is {_format_value(cols[0].name, rows[0][0])}."

    if kind == "top_n" and len(cols) >= 2 and rows:
        dim, metric = cols[0], cols[1]
        top = rows[: min(5, len(rows))]
        items = ", ".join(
            f"{i + 1}. {_format_value(dim.name, r[0])} ({_format_value(metric.name, r[1])})"
            for i, r in enumerate(top)
        )
        more = "" if len(rows) <= 5 else f" (+{len(rows) - 5} more)"
        n = len(rows)
        dim_word = dim.name.replace('_', ' ')
        if not dim_word.endswith('s') and n != 1:
            dim_word += 's'
        return f"Top {n} {dim_word} by {metric.name.replace('_', ' ')}: {items}{more}."

    if kind == "trend" and rows:
        first, last = rows[0], rows[-1]
        total = sum(_coerce_number(r[1]) for r in rows)
        total_str = _format_value(cols[1].name, total)
        latest_str = _format_value(cols[1].name, last[1])
        return (
            f"{total_str} total {cols[1].name.replace('_', ' ')} across {len(rows)} days "
            f"({_ymd(first[0])} to {_ymd(last[0])}). "
            f"Latest day ({_ymd(last[0])}): {latest_str}."
        )

    if rows:
        return f"{result.row_count} rows. First row: {_format_row(cols, rows[0])}."

    return f"No rows returned for: {question!r}."


def _sanitize_dashboard_name(question: str) -> str:
    s = re.sub(r"\s+", " ", question).strip().rstrip("?.!")
    return s[:80]


# ---------------------------------------------------------------------------
# Convenience: end-to-end NL → dashboard
# ---------------------------------------------------------------------------
def run(question: str, *, dashboard_name: str | None = None) -> dict[str, Any]:
    """NL question → QueryResult + DashboardHandle. Returns a dict the
    caller can pretty-print or feed to a chat."""
    result = query_run(question)
    answer = render_answer(question, result)
    dash_name = dashboard_name or f"/ask — {_sanitize_dashboard_name(question)}"
    handle = build_dashboard(
        query_results=[result],
        dashboard_name=dash_name,
        description=f"Auto-generated for: {question!r}",
    )
    return {
        "answer": answer,
        "dashboard_url": handle.url,
        "dashboard_id": handle.dashboard_id,
        "dashboard_name": handle.name,
        "sql": result.sql,
        "row_count": result.row_count,
        "columns": [c.model_dump() for c in result.columns],
        "analysis": result.analysis.model_dump(),
        "sources": [
            "tools/query.py", "tools/dashboard.py",
            "tools/postgres.py", "tools/metabase.py",
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Build a Metabase dashboard from a QueryResult JSON, or end-to-end from a question."
    )
    p.add_argument("input", help="Either a path to a QueryResult JSON file OR a natural-language question (if --include-answer).")
    p.add_argument("name", nargs="?", default=None, help="Dashboard name (required when input is a JSON file).")
    p.add_argument("--include-answer", action="store_true",
                   help="Input is a natural-language question; chain the Query skill, then build a dashboard.")
    p.add_argument("--pretty", action="store_true")
    args = p.parse_args(argv)

    try:
        if args.include_answer:
            question = args.input
            out = run(question)
        else:
            if args.name is None:
                print("[dashboard] error: dashboard name is required when input is a JSON file.", file=sys.stderr)
                return 2
            with open(args.input) as f:
                raw = json.load(f)
            results = [QueryResult.model_validate(r) for r in raw]
            handle = build_dashboard(results, args.name)
            out = handle.model_dump()
    except FileNotFoundError as e:
        print(f"[dashboard] {e}", file=sys.stderr)
        return 2
    except NotImplementedError as e:
        print(f"[dashboard] not implemented: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[dashboard] failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # Always print the dashboard URL on its own line, first.
    # This lets puku-cli surface a clickable link without parsing JSON.
    url = out.get("dashboard_url") or out.get("url")
    if url:
        print(url)

    # Always print the answer (if any) on its own line.
    answer = out.get("answer")
    if answer:
        print(answer)

    if args.pretty:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())