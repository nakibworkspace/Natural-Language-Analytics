"""Query skill — translate natural-language to SQL and run it.

Public entry points:

  * ``run(question)``                 — offline (no LLM). Default.
  * ``run_with_llm(question)``        — uses an LLM to generate the SQL.

CLI::

    python3 tools/query.py [--use-llm] [--pretty] "<question>"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.schemas import AnalysisHints, Column, ColumnType, QueryResult, pg_type_to_column_type
from tools.postgres import execute_sql, get_schema_snapshot, schema_as_text
from tools.sql_validator import SQLValidationError

# ---------------------------------------------------------------------------
# LLM client (was skills/query_skill/tools/llm.py)
# ---------------------------------------------------------------------------
class LLMClient(ABC):
    @abstractmethod
    def complete(self, system: str, user: str, *, max_tokens: int = 800, temperature: float = 0.0) -> str:
        raise NotImplementedError


class PukuLLMClient(LLMClient):
    """Call Puku CLI as a subprocess. Default in this lab."""

    def __init__(self, puku_bin: str = "puku") -> None:
        self.puku_bin = puku_bin

    def complete(self, system: str, user: str, *, max_tokens: int = 800, temperature: float = 0.0) -> str:
        prompt = f"{system}\n\n{user}"
        proc = subprocess.run(
            [self.puku_bin, "prompt", prompt, "--max-tokens", str(max_tokens)],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"puku prompt failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout.strip()


class HTTPCompletionsClient(LLMClient):
    """Generic OpenAI-compatible /v1/chat/completions client."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key  = api_key  or os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
        self.model    = model    or os.getenv("LLM_MODEL", "gpt-4o-mini")
        if not self.api_key:
            raise RuntimeError("LLM_API_KEY / OPENAI_API_KEY not set for HTTPCompletionsClient")

    def complete(self, system: str, user: str, *, max_tokens: int = 800, temperature: float = 0.0) -> str:
        body = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        r = requests.post(f"{self.base_url}/chat/completions", headers=headers, json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"].strip()


def default_client() -> LLMClient:
    if os.getenv("LLM_BASE_URL") or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"):
        return HTTPCompletionsClient()
    return PukuLLMClient()


# ---------------------------------------------------------------------------
# Prompt strings (inlined; was skills/query_skill/prompts/*.md)
# ---------------------------------------------------------------------------
SQL_GEN_SYSTEM = """\
You are the SQL generation component of an analytics query skill.

You translate a single natural-language question into ONE PostgreSQL SELECT
statement (CTEs / WITH are allowed).

You will be given:

  1. The live schema of the `ride_analytics` database (table and column
     names with their Postgres types).
  2. The user's natural-language question.

Rules (NON-NEGOTIABLE):

  - Generate ONE SELECT or WITH ... SELECT statement only.
  - Never emit INSERT/UPDATE/DELETE/DDL/COPY/EXPLAIN/... anything.
  - Only reference tables/columns that exist in the schema.
  - Add a LIMIT for unbounded queries (default LIMIT 1000 unless the user
    specifies "top N").
  - For "last X days" questions, use `requested_at >= NOW() - INTERVAL 'X days'`
    against `rides.requested_at`.
  - When aggregating by a foreign-key column, JOIN with the corresponding
    dimension table and prefer the human-readable column (e.g.
    `l.name AS destination_location` from `locations l`).
  - Never invent column names. If a name is ambiguous, JOIN the dimension
    table and use the obvious alias.
  - Use snake_case aliases.

Output format (respond ONLY with this block, no commentary):

===SQL===
<the SQL statement, terminated with a single semicolon>
===END===

Do not include any commentary, code fences, or markdown. Just the block.
"""

ANALYZE_SYSTEM = """\
You annotate a query result so that a downstream dashboard skill can pick
the right visualization.

You will be given:
  1. The user's original question.
  2. The SQL statement that produced the result.
  3. The column names and coarse types (string|integer|float|date|datetime|
     boolean|unknown).
  4. Up to 5 sample rows.
  5. The total row count.

Based on this, output ONLY the following JSON object, no commentary, no
markdown fencing:

{
  "dimensions":        [<column names that act as categorical axes>],
  "measures":          [<column names that act as numeric values>],
  "time_dimensions":   [<column names that act as time axes>],
  "is_single_value":   <true if the result is one numeric value>,
  "semantic_kind":     <one of "top_n" | "trend" | "kpi" | "distribution"
                               | "breakdown" | "raw" | "rating_dist" | "table">
}

Heuristics:
  - "kpi"      : single row, single numeric measure, no dimensions.
  - "trend"    : one time_dimension, one or more measures.
  - "top_n"    : one string dimension, one numeric measure, ordered desc.
  - "distribution": one string dimension, one numeric measure, no rank.
  - "breakdown": multiple string dimensions or many measures; treat as table.
  - "rating_dist": special case of distribution when dim is "rating".
  - "raw"      : fallback when nothing else matches.

Reply only with JSON, no commentary or markdown.
"""


# ---------------------------------------------------------------------------
# Offline stub (was skills/query_skill/tools/offline_stub.py)
# ---------------------------------------------------------------------------
def _q(text: str, *alternatives) -> re.Pattern[str]:
    return re.compile("|".join([text] + list(alternatives)), re.IGNORECASE)


def _q_loc(*patterns: str) -> re.Pattern[str]:
    """Each alternative uses an unnamed group ``(...)`` that captures the
    location name; we pull whichever group matched out of the match."""
    return re.compile("|".join(patterns), re.IGNORECASE)


# Pattern -> (SQL template, analysis)
PATTERNS: list[tuple[re.Pattern[str], str, AnalysisHints]] = [
    (
        # "traffic in Mirpur" / "how many rides to Dhanmondi" /
        # "rides in Gulshan last week" / "the traffics in Banani".
        # ``traffics?`` covers both singular ("traffic") and the
        # colloquial plural ("traffics") users sometimes type.
        # The SQL includes pickup OR destination so a pickup-only
        # location like Farmgate still returns rows.
        _q_loc(
            r"(?:traffics?|rides?)\s+(?:in|to|from|at)\s+([A-Za-z][A-Za-z \-]+?)(?:\s+last\s+\d+\s+days?)?\s*\??$",
            r"how\s+many\s+rides?\s+(?:in|to|from|at)\s+([A-Za-z][A-Za-z \-]+?)\s*\??$",
            r"ride\s+(?:count|volume)\s+(?:in|to|from|at)\s+([A-Za-z][A-Za-z \-]+?)\s*\??$",
        ),
        """\
SELECT date_trunc('day', r.requested_at) AS day, COUNT(*) AS ride_count
FROM rides r
WHERE r.requested_at >= NOW() - INTERVAL '30 days'
  AND (
        r.pickup_location_id      IN (SELECT id FROM locations WHERE name = '{loc}')
     OR r.destination_location_id IN (SELECT id FROM locations WHERE name = '{loc}')
  )
GROUP BY day
ORDER BY day;""",
        AnalysisHints(time_dimensions=["day"], measures=["ride_count"], semantic_kind="trend"),
    ),
    (
        _q(
            r"top\s+(\d+)?\s*destinations?\s+by\s+(?:rides|count)",
            r"destinations?\s+with\s+the\s+most\s+rides",
            r"most\s+popular\s+destinations?",
        ),
        """\
SELECT l.name AS destination, COUNT(*) AS ride_count
FROM rides r JOIN locations l ON l.id = r.destination_location_id
WHERE r.requested_at >= NOW() - INTERVAL '30 days'
GROUP BY l.name
ORDER BY ride_count DESC
LIMIT {n};""",
        AnalysisHints(dimensions=["destination"], measures=["ride_count"], semantic_kind="top_n"),
    ),
    (
        _q(r"average\s+fare(?:\s+by\s+destinations?)?", r"avg\s+fare"),
        "SELECT AVG(fare)::numeric(10,2) AS avg_fare FROM rides WHERE status = 'completed';",
        AnalysisHints(measures=["avg_fare"], is_single_value=True, semantic_kind="kpi"),
    ),
    (
        _q(r"rides?\s+per\s+day", r"ride\s+volume\s+over\s+time", r"rides?\s+by\s+day"),
        """\
SELECT date_trunc('day', requested_at) AS day, COUNT(*) AS ride_count
FROM rides
WHERE requested_at >= NOW() - INTERVAL '30 days'
GROUP BY day ORDER BY day;""",
        AnalysisHints(time_dimensions=["day"], measures=["ride_count"], semantic_kind="trend"),
    ),
    (
        _q(r"cancellation\s+rate"),
        "SELECT (COUNT(*) FILTER (WHERE status='cancelled'))::float / COUNT(*) AS cancellation_rate FROM rides;",
        AnalysisHints(measures=["cancellation_rate"], is_single_value=True, semantic_kind="kpi"),
    ),
    (
        _q(r"average\s+driver\s+rating", r"avg\s+driver\s+rating"),
        "SELECT AVG(rating)::numeric(3,2) AS avg_driver_rating FROM drivers;",
        AnalysisHints(measures=["avg_driver_rating"], is_single_value=True, semantic_kind="kpi"),
    ),
    (
        _q(r"revenue\s+by\s+destinations?"),
        """\
SELECT l.name AS destination, SUM(r.fare)::numeric(12,2) AS revenue
FROM rides r JOIN locations l ON l.id = r.destination_location_id
WHERE r.status = 'completed'
GROUP BY l.name ORDER BY revenue DESC;""",
        AnalysisHints(dimensions=["destination"], measures=["revenue"], semantic_kind="breakdown"),
    ),
    (
        _q(r"busiest\s+hours?", r"rides?\s+by\s+hour"),
        """\
SELECT EXTRACT(HOUR FROM requested_at)::int AS hour_of_day, COUNT(*) AS ride_count
FROM rides GROUP BY 1 ORDER BY 1;""",
        AnalysisHints(dimensions=["hour_of_day"], measures=["ride_count"], semantic_kind="distribution"),
    ),
    (
        _q(r"top\s+drivers?", r"best\s+drivers?", r"top\s+(\d+)?\s*drivers?"),
        """\
SELECT d.code AS driver, AVG(r.fare)::numeric(10,2) AS avg_fare, COUNT(*) AS rides
FROM rides r JOIN drivers d ON d.id = r.driver_id
WHERE r.status = 'completed'
GROUP BY d.code ORDER BY rides DESC LIMIT 10;""",
        AnalysisHints(dimensions=["driver"], measures=["avg_fare", "rides"], semantic_kind="top_n"),
    ),
    (
        _q(r"rating\s+distribution"),
        "SELECT rating, COUNT(*) AS count FROM reviews GROUP BY rating ORDER BY rating;",
        AnalysisHints(dimensions=["rating"], measures=["count"], semantic_kind="rating_dist"),
    ),
    (
        _q(r"review\s+counts?\s+over\s+time"),
        """\
SELECT date_trunc('day', created_at) AS day, COUNT(*) AS review_count
FROM reviews GROUP BY day ORDER BY day;""",
        AnalysisHints(time_dimensions=["day"], measures=["review_count"], semantic_kind="trend"),
    ),
]


def find_sql(question: str) -> tuple[str, AnalysisHints] | None:
    for pat, sql_template, analysis in PATTERNS:
        m = pat.search(question)
        if not m:
            continue
        n_match = re.search(r"top\s+(\d+)", question, re.IGNORECASE)
        n = n_match.group(1) if n_match else "10"

        loc = None
        if "{loc}" in sql_template:
            for gi in (1, 2, 3):
                try:
                    if m.group(gi) is not None:
                        loc = m.group(gi).strip()
                        break
                except (IndexError, AttributeError):
                    pass
            if loc is not None and not re.fullmatch(r"[A-Za-z \-]+", loc):
                return None

        # Title-case multi-word location names so a user typing
        # "traffic in banani" still matches the seeded row ``Banani``.
        if loc is not None:
            loc = " ".join(p.capitalize() for p in loc.split())
            rendered = sql_template.format(n=n, loc=loc)
        else:
            rendered = sql_template.format(n=n)
        return rendered, analysis.copy(deep=True)
    return None


def annotate_from_question(question: str, columns: list[str]) -> AnalysisHints:
    ql = question.lower()
    if "top" in ql and ("destination" in ql or "driver" in ql):
        return AnalysisHints(
            dimensions=[columns[0]],
            measures=[columns[1]] if len(columns) > 1 else [],
            semantic_kind="top_n",
        )
    if "average" in ql or "avg" in ql:
        return AnalysisHints(measures=[columns[0]], is_single_value=True, semantic_kind="kpi")
    if "per day" in ql or "over time" in ql or "trend" in ql:
        return AnalysisHints(
            time_dimensions=[columns[0]],
            measures=[columns[1]] if len(columns) > 1 else [],
            semantic_kind="trend",
        )
    if "rating distribution" in ql:
        return AnalysisHints(
            dimensions=[columns[0]],
            measures=[columns[1]] if len(columns) > 1 else [],
            semantic_kind="rating_dist",
        )
    return AnalysisHints(dimensions=columns[:1], measures=columns[1:2], semantic_kind="table")


# ---------------------------------------------------------------------------
# Public API: run + run_with_llm
# ---------------------------------------------------------------------------
def run(question: str) -> QueryResult:
    """Translate NL to SQL using the offline stub, run it, return QueryResult."""
    print(f"[query] question: {question}", file=sys.stderr)
    match = find_sql(question)
    if not match:
        raise NotImplementedError(
            f"Offline stub does not recognize the question: {question!r}. "
            "Try `python3 tools/query.py --use-llm <question>` or extend PATTERNS."
        )
    sql, analysis = match
    print(f"[query] SQL:\n{sql}", file=sys.stderr)
    result = execute_sql(question=question, sql=sql)
    if analysis is not None:
        result.analysis = analysis
    else:
        result.analysis = annotate_from_question(question, [c.name for c in result.columns])
    print(f"[query] rows: {result.row_count}, columns: {[c.name for c in result.columns]}", file=sys.stderr)
    return result


def _extract_sql(text: str) -> str:
    m = re.search(r"===SQL===\s*(.+?)\s*===END===", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```(?:sql)?\s*(.+?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text.strip()


def _extract_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _generate_sql_llm(question: str, schema_text: str, *, client: LLMClient | None = None) -> str:
    client = client or default_client()
    raw = client.complete(
        system=SQL_GEN_SYSTEM.strip(),
        user=f"Schema:\n{schema_text}\n\nQuestion:\n{question}".strip(),
    )
    return _extract_sql(raw)


def _annotate_llm(question: str, sql: str, result: QueryResult, *, client: LLMClient | None = None) -> AnalysisHints:
    client = client or default_client()
    columns_json = json.dumps([{"name": c.name, "type": c.type.value} for c in result.columns])
    sample = json.dumps(result.rows[:5], default=str)
    raw = client.complete(
        system=ANALYZE_SYSTEM.strip(),
        user=(
            f"Original question:\n{question}\n\n"
            f"SQL:\n{sql}\n\n"
            f"Columns (name, type):\n{columns_json}\n\n"
            f"Sample rows:\n{sample}\n\n"
            f"Row count: {result.row_count}\n\n"
            "Output JSON now."
        ),
    )
    data = _extract_json(raw)
    return AnalysisHints(
        dimensions=data.get("dimensions", []),
        measures=data.get("measures", []),
        time_dimensions=data.get("time_dimensions", []),
        is_single_value=bool(data.get("is_single_value", False)),
        semantic_kind=data.get("semantic_kind"),
    )


def run_with_llm(question: str, *, client: LLMClient | None = None) -> QueryResult:
    """LLM-driven NL→SQL. Reads schema, calls LLM, validates, executes."""
    print(f"[query/llm] question: {question}", file=sys.stderr)
    snapshot = get_schema_snapshot()
    schema_text = schema_as_text(snapshot)
    sql = _generate_sql_llm(question, schema_text, client=client)
    print(f"[query/llm] SQL:\n{sql}", file=sys.stderr)

    try:
        result = execute_sql(question=question, sql=sql)
    except (SQLValidationError, Exception) as e:
        print(f"[query/llm] error: {e}", file=sys.stderr)
        return QueryResult(
            question=question, sql=sql, columns=[], rows=[], row_count=0,
            analysis=AnalysisHints(semantic_kind="error"),
        )

    result.analysis = _annotate_llm(question, sql, result, client=client)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Translate NL → SQL, run, return QueryResult JSON.")
    p.add_argument("question", help="Natural-language question")
    p.add_argument("--use-llm", action="store_true", help="Use an LLM instead of the offline stub")
    p.add_argument("--pretty", action="store_true", help="Pretty-print the JSON output")
    args = p.parse_args(argv)

    try:
        result = run_with_llm(args.question) if args.use_llm else run(args.question)
    except NotImplementedError as e:
        print(f"[query] not implemented: {e}", file=sys.stderr)
        return 2

    if args.pretty:
        print(json.dumps(result.model_dump(), indent=2, default=str))
    else:
        print(json.dumps(result.model_dump(), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())