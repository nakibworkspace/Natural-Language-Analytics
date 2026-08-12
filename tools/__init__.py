"""Deterministic, security-bounded tools.

This package owns:

  * ``postgres.py``     — schema inspection + read-only query execution
  * ``sql_validator.py`` — deny-list SQL safety gate
  * ``metabase.py``     — Metabase REST API client (no LLM ever calls this
                          directly; only via the dashboard-skill)

The point of splitting these out of the skills is **security boundaries**:
the LLM proposes, these deterministic functions validate and execute.
"""
