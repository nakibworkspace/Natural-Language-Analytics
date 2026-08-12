"""Postgres read-only client used by the Query Skill.

Two responsibilities:
  1. Inspect the live database schema (tables, columns, types, row counts)
     so the Query Skill never has to hallucinate column names.
  2. Execute validated, read-only SQL with timeouts and row caps, returning
     a structured ``QueryResult``.

This module NEVER accepts raw SQL from the LLM. Callers MUST run their SQL
through ``tools.sql_validator.validate_sql`` first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import psycopg
from dotenv import load_dotenv

from shared.schemas import (
    AnalysisHints,
    Column,
    ColumnType,
    QueryResult,
    pg_type_to_column_type,
)
from tools.sql_validator import SQLValidationError, validate_sql

load_dotenv()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PgConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    timeout_ms: int
    max_rows: int

    @classmethod
    def from_env(cls) -> "PgConfig":
        return cls(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            dbname=os.getenv("POSTGRES_DB", "ride_analytics"),
            user=os.getenv("POSTGRES_READER_USER", "analytics_reader"),
            password=os.getenv("POSTGRES_READER_PASSWORD", "analytics_reader_pw"),
            timeout_ms=int(os.getenv("SQL_QUERY_TIMEOUT_MS", "15000")),
            max_rows=int(os.getenv("SQL_MAX_ROWS", "10000")),
        )


def _dsn(cfg: PgConfig) -> str:
    return (
        f"host={cfg.host} port={cfg.port} dbname={cfg.dbname} "
        f"user={cfg.user} password={cfg.password} "
        f"application_name=ride-analytics-skill"
    )


# ---------------------------------------------------------------------------
# Schema inspection
# ---------------------------------------------------------------------------
def get_schema_snapshot(cfg: PgConfig | None = None) -> dict[str, Any]:
    """Return a JSON-serializable snapshot of the public schema.

    The Query Skill uses this to ground its SQL generation in the real
    database, reducing the chance of hallucinated column/table names.
    """
    cfg = cfg or PgConfig.from_env()
    with psycopg.connect(_dsn(cfg), autocommit=True) as conn:
        with conn.cursor() as cur:
            # Tables + columns
            cur.execute(
                """
                SELECT table_name, column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position;
                """
            )
            tables: dict[str, dict[str, Any]] = {}
            for tname, cname, dtype, nullable in cur.fetchall():
                t = tables.setdefault(tname, {"columns": {}, "row_count": None})
                t["columns"][cname] = {"type": dtype, "nullable": nullable == "YES"}

            # Row counts
            for tname in tables:
                cur.execute(
                    f'SELECT COUNT(*) FROM "{tname}";'
                )
                tables[tname]["row_count"] = cur.fetchone()[0]

            # Foreign keys (helpful for join planning)
            cur.execute(
                """
                SELECT
                    tc.table_name,
                    kcu.column_name,
                    ccu.table_name AS foreign_table,
                    ccu.column_name  AS foreign_column
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name = tc.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public';
                """
            )
            for tname, cname, ftable, fcol in cur.fetchall():
                tables[tname].setdefault("foreign_keys", []).append(
                    {"column": cname, "references": f"{ftable}.{fcol}"}
                )

    return {"database": cfg.dbname, "tables": tables}


def schema_as_text(snapshot: dict[str, Any]) -> str:
    """Render the schema as a compact, LLM-friendly text block."""
    out = [f"Database: {snapshot['database']}"]
    for tname, tinfo in snapshot["tables"].items():
        cols = ", ".join(
            f"{c} {meta['type']}{' NULL' if meta['nullable'] else ''}"
            for c, meta in tinfo["columns"].items()
        )
        out.append(f"- {tname}({cols})  -- ~{tinfo.get('row_count','?')} rows")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------
def execute_sql(
    question: str,
    sql: str,
    cfg: PgConfig | None = None,
    analysis: AnalysisHints | None = None,
) -> QueryResult:
    """Validate + execute SQL and return a structured ``QueryResult``.

    Raises ``SQLValidationError`` for unsafe SQL, ``psycopg.Error`` for
    runtime DB errors.
    """
    cfg = cfg or PgConfig.from_env()
    safe_sql = validate_sql(sql)

    # ``SET LOCAL statement_timeout`` requires a transaction; we open one
    # explicitly, run the query, and ROLLBACK (we never write anything anyway).
    with psycopg.connect(_dsn(cfg), autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {cfg.timeout_ms};")
            cur.execute(safe_sql)
            description = cur.description or []
            rows = cur.fetchmany(cfg.max_rows)
            truncated = cur.fetchone() is not None
        conn.rollback()

    columns = [
        Column(name=col.name, type=pg_type_to_column_type(
            # psycopg types are exposed via type_code; we map to a string
            # by looking up the registry.
            _type_code_to_name(col.type_code)
        ))
        for col in description
    ]
    # Normalize row values: Decimal -> float, datetime -> isoformat, etc.
    norm_rows: list[list[Any]] = []
    for r in rows:
        norm_rows.append([_normalize_value(v) for v in r])

    return QueryResult(
        question=question,
        sql=safe_sql,
        columns=columns,
        rows=norm_rows,
        row_count=len(norm_rows),
        truncated=truncated,
        analysis=analysis or AnalysisHints(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_TYPE_NAMES = {
    16: "bool", 20: "bigint", 21: "smallint", 23: "integer", 25: "text",
    700: "float4", 701: "float8", 1043: "varchar", 1082: "date", 1114: "timestamp",
    1184: "timestamptz", 1700: "numeric", 2950: "uuid", 114: "json", 3802: "jsonb",
}


def _type_code_to_name(code: int) -> str:
    return _TYPE_NAMES.get(code, "unknown")


def _normalize_value(v: Any) -> Any:
    """Make values JSON-friendly for downstream Metabase + JSON payloads."""
    from datetime import date, datetime
    from decimal import Decimal
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


__all__ = [
    "PgConfig",
    "get_schema_snapshot",
    "schema_as_text",
    "execute_sql",
    "SQLValidationError",
]
