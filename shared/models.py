"""Visualization + dashboard planning models.

The Dashboard Skill consumes ``QueryResult`` (from ``schemas.py``), runs the
visualization selection logic, and emits ``VisualizationPlan`` and
``DashboardPlan``. Those plans are then translated into Metabase API calls by
``tools/metabase.py``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class VisualizationKind(str, Enum):
    """Visualization types the Dashboard Skill can emit.

    The names map 1-1 to the strings Metabase uses in its dataset_query
    visualizations, so the Metabase client does not need to translate.
    """
    TABLE     = "table"
    BAR       = "bar"
    LINE      = "line"
    AREA      = "area"
    PIE       = "pie"
    SCATTER   = "scatter"
    NUMBER    = "number"     # KPI / single big number
    GAUGE     = "gauge"
    MAP       = "map"
    FUNNEL    = "funnel"
    PIVOT     = "pivot"


class VisualizationPlan(BaseModel):
    """One card on a dashboard, planned by the Dashboard Skill."""
    title: str
    kind: VisualizationKind
    question_text: str                          # the original natural-language query
    sql: str                                    # exact SQL that produced the result
    columns: list[dict[str, Any]]               # [{name,type}, ...]
    rows: list[list[Any]]
    row_count: int
    description: Optional[str] = None
    settings: dict[str, Any] = Field(default_factory=dict)


class DashboardCardRef(BaseModel):
    """Placement metadata for a card on a dashboard grid."""
    title: str
    row: int = 0
    col: int = 0
    size_x: int = 6
    size_y: int = 4


class DashboardPlan(BaseModel):
    """A bundle of cards to be created/updated on a single dashboard."""
    name: str
    description: Optional[str] = None
    cards: list[DashboardCardRef]
    visualizations: list[VisualizationPlan]


class DashboardHandle(BaseModel):
    """A reference to an existing Metabase dashboard."""
    dashboard_id: int
    name: str
    url: str


class MetabaseQuestionSpec(BaseModel):
    """Payload we POST to Metabase /api/card."""
    name: str
    dataset_query: dict[str, Any]
    display: str                                       # visualization kind string
    visualization_settings: dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    collection_id: Optional[int] = None


class MetabaseDashboardSpec(BaseModel):
    """Payload we POST to Metabase /api/dashboard."""
    name: str
    description: Optional[str] = None
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    collection_id: Optional[int] = None