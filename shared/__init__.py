"""Shared data contracts for the ride-sharing analytics lab.

This package is intentionally small and dependency-light. It defines:

  * Pydantic models describing the structured output of the Query Skill
    (see ``QueryResult`` in ``schemas.py``)
  * Pydantic models describing visualization plans produced by the
    Dashboard Skill (see ``VisualizationPlan`` in ``models.py``)
  * Pydantic models for Metabase API payloads (see ``models.py``)

Both skills import from here so that the contract between them is enforced
statically and via runtime validation.
"""

from .schemas import (
    ColumnType,
    Column,
    QueryResult,
    AnalysisHints,
    MultiQueryResponse,
)
from .models import (
    VisualizationKind,
    VisualizationPlan,
    DashboardCardRef,
    DashboardPlan,
    DashboardHandle,
    MetabaseQuestionSpec,
    MetabaseDashboardSpec,
)

__all__ = [
    "ColumnType",
    "Column",
    "QueryResult",
    "AnalysisHints",
    "MultiQueryResponse",
    "VisualizationKind",
    "VisualizationPlan",
    "DashboardCardRef",
    "DashboardPlan",
    "DashboardHandle",
    "MetabaseQuestionSpec",
    "MetabaseDashboardSpec",
]
