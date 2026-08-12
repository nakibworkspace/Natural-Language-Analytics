"""Pydantic models describing what the Query Skill produces.

The Query Skill's only job is to take a natural-language question and return a
structured ``QueryResult`` so that downstream components (the Dashboard Skill
in particular) have a predictable contract.

These models intentionally do NOT mention Metabase. The Dashboard Skill is
responsible for mapping ``QueryResult`` -> visualization -> Metabase API.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class ColumnType(str, Enum):
    """Coarse data type categorization, derived from Postgres types."""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    UNKNOWN = "unknown"


def pg_type_to_column_type(pg_type: str) -> ColumnType:
    """Map a Postgres type name to our coarse ``ColumnType``."""
    t = pg_type.lower()
    if t.startswith("int") or t in ("smallint", "bigint", "serial", "bigserial"):
        return ColumnType.INTEGER
    if t.startswith("numeric") or t.startswith("decimal") or t.startswith("float") or t == "double precision" or t == "real":
        return ColumnType.FLOAT
    if t == "date":
        return ColumnType.DATE
    if "timestamp" in t or "time" in t:
        return ColumnType.DATETIME
    if t == "bool" or t == "boolean":
        return ColumnType.BOOLEAN
    if t in ("text", "varchar", "char", "uuid", "json", "jsonb"):
        return ColumnType.STRING
    return ColumnType.UNKNOWN


class Column(BaseModel):
    """One column in a query result."""
    name: str
    type: ColumnType
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _no_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("column name cannot be empty")
        return v


class AnalysisHints(BaseModel):
    """Lightweight semantic hints the Query Skill attaches to a result.

    The Dashboard Skill uses these hints (not the LLM, not the raw SQL) to
    decide which visualization to use.
    """
    dimensions: list[str] = Field(default_factory=list)        # e.g. ["destination_location"]
    measures:   list[str] = Field(default_factory=list)        # e.g. ["ride_count"]
    time_dimensions: list[str] = Field(default_factory=list)   # e.g. ["day"]
    is_single_value: bool = False                              # e.g. SELECT COUNT(*) ...
    semantic_kind: Optional[str] = None                          # e.g. "top_n", "trend", "kpi"


class QueryResult(BaseModel):
    """The structured output of the Query Skill."""
    question: str
    sql: str = Field(..., description="The (validated) SQL that produced this result.")
    columns: list[Column]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    analysis: AnalysisHints = Field(default_factory=AnalysisHints)
    executed_at: datetime = Field(default_factory=datetime.utcnow)
    database: str = "ride_analytics"


class MultiQueryResponse(BaseModel):
    """A bundle of QueryResults, used when the user asks for a multi-card dashboard."""
    question: str
    results: list[QueryResult]