"""Metabase REST API client.

This module owns **all** HTTP calls to Metabase. The dashboard skill is the
only consumer; the LLM never gets raw access to ``requests.Session``.

Exposed functions are deliberate and audited. Anything beyond this surface
must be added explicitly.

Required env:
    METABASE_URL          e.g. http://localhost:3000
    METABASE_API_KEY      see guide.md, step "create API key"
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


class MetabaseError(RuntimeError):
    """Wraps any non-2xx response from the Metabase API."""


class MetabaseClient:
    """Thin client over the public Metabase REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        auto_provision: bool = True,
    ) -> None:
        self.base_url = (base_url or os.getenv("METABASE_URL", "http://localhost:3000")).rstrip("/")
        self.api_key  = api_key  or os.getenv("METABASE_API_KEY", "")
        self.timeout  = timeout
        self.session  = session or requests.Session()
        if not self.api_key and auto_provision:
            # First-time run: auto-provision an API key via the admin session.
            # ``tools/metabase_provision`` will log in, connect Postgres, and
            # create/persist a key into .env.
            from tools.metabase_provision import ensure_ready
            self.api_key = ensure_ready()
        if not self.api_key:
            raise MetabaseError(
                "METABASE_API_KEY is not set and auto-provisioning failed. "
                "Check METABASE_ADMIN_EMAIL / METABASE_ADMIN_PASSWORD in .env "
                "and that Metabase is reachable."
            )

    # ---------- low level ----------
    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api{path}"

    def _request(self, method: str, path: str, json_body: dict | None = None) -> Any:
        url = self._url(path)
        resp = self.session.request(
            method, url, headers=self._headers(),
            data=json.dumps(json_body) if json_body is not None else None,
            timeout=self.timeout,
        )
        if not (200 <= resp.status_code < 300):
            raise MetabaseError(
                f"{method} {path} -> {resp.status_code}: {resp.text[:500]}"
            )
        if resp.status_code == 204:
            return None
        return resp.json()

    # ---------- databases ----------
    def list_databases(self) -> list[dict[str, Any]]:
        body = self._request("GET", "/database")
        # Metabase v0.49+ wraps list endpoints in {"data": [...]}; older
        # versions returned a bare array. Normalize here so callers always
        # get a list of dicts.
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        if isinstance(body, list):
            return body
        raise MetabaseError(
            f"GET /database returned unexpected shape: {type(body).__name__}"
        )

    def find_database_by_name(self, name: str) -> dict[str, Any]:
        for db in self.list_databases():
            if db.get("name") == name:
                return db
        raise MetabaseError(f"Database '{name}' not found in Metabase")

    def find_database_id(self, name: str) -> int:
        return self.find_database_by_name(name)["id"]

    # ---------- questions / cards ----------
    def build_native_dataset_query(self, database_id: int, sql: str) -> dict[str, Any]:
        """Build the ``dataset_query`` payload for a native (raw SQL) question.

        In Metabase v0.49+ the ``native.collection`` field, if present,
        must be a non-blank string (the name of a snippet collection).
        Passing ``null`` causes a 400 when the card is later queried:
        ``Invalid query: {:stages [{:collection ["should be a string"
        "non-blank string"]}]}``. Omitting the field entirely is the
        documented way to say "no collection" and works across versions.
        """
        return {
            "database": database_id,
            "type": "native",
            "native": {"query": sql},
        }

    def create_question(
        self,
        name: str,
        database_id: int,
        sql: str,
        display: str,
        visualization_settings: dict | None = None,
        description: str | None = None,
        collection_id: int | None = None,
    ) -> dict[str, Any]:
        body = {
            "name": name,
            "dataset_query": self.build_native_dataset_query(database_id, sql),
            "display": display,
            "visualization_settings": visualization_settings or {},
            "description": description,
            "collection_id": collection_id,
        }
        return self._request("POST", "/card", body)

    def update_question_visualization(
        self,
        card_id: int,
        display: str,
        visualization_settings: dict | None = None,
    ) -> dict[str, Any]:
        # Metabase requires PUT /api/card/:id for visualization updates
        body = {
            "display": display,
            "visualization_settings": visualization_settings or {},
        }
        return self._request("PUT", f"/card/{card_id}", body)

    def get_question(self, card_id: int) -> dict[str, Any]:
        return self._request("GET", f"/card/{card_id}")

    # ---------- dashboards ----------
    def list_dashboards(self, name: str | None = None) -> list[dict[str, Any]]:
        results = self._request("GET", "/dashboard")
        if name:
            return [d for d in results if d.get("name") == name]
        return results

    def find_dashboard_by_name(self, name: str) -> dict[str, Any] | None:
        for d in self.list_dashboards():
            if d.get("name") == name:
                return d
        return None

    def create_dashboard(
        self,
        name: str,
        description: str | None = None,
        collection_id: int | None = None,
    ) -> dict[str, Any]:
        body = {"name": name, "description": description, "collection_id": collection_id}
        return self._request("POST", "/dashboard", body)

    def delete_dashboard(self, dashboard_id: int) -> None:
        return self._request("DELETE", f"/dashboard/{dashboard_id}")

    def dashboard_url(self, dashboard_id: int) -> str:
        return f"{self.base_url}/dashboard/{dashboard_id}"

    def add_card_to_dashboard(
        self,
        dashboard_id: int,
        card_id: int,
        row: int = 0,
        col: int = 0,
        size_x: int = 6,
        size_y: int = 4,
    ) -> dict[str, Any]:
        """Place an existing card onto a dashboard grid.

        Metabase v0.50 removed ``POST /api/dashboard/:id/cards``. The
        only supported way to mutate a dashboard's cards is
        ``PUT /api/dashboard/:id`` with the full ``dashcards`` list.
        We fetch the existing dashcards, append the new one, and PUT
        the result back.

        New dashcards MUST carry an ``id`` field. The Metabase schema
        for ``PUT /api/dashboard/:id`` (``UpdatedDashboardCard`` in
        ``src/metabase/api/dashboard.clj``) says:

            ;; id can be negative, it indicates a new card and BE should
            ;; create them

        Existing dashcards always have a positive integer ``id``.
        For new ones the frontend uses a monotonically-decreasing
        negative counter starting at ``-1`` (``generateTemporaryDashcardId``
        in ``frontend/src/metabase/dashboard/utils.ts``). We mirror that
        here so the dashboard diff (``u/row-diff``) recognises the new
        entry as "to-create" rather than "to-update".

        The ``dashboard_id`` is filled in by Metabase from the URL.
        """
        dash = self._request("GET", f"/dashboard/{dashboard_id}")
        existing = dash.get("dashcards") or []

        # Pick a negative id that's unique among the dashcards being
        # sent (existing + new). The actual value doesn't matter as
        # long as it's < 0 and not already in use, so the row-diff
        # sees it as a new entry rather than an update.
        taken = {d.get("id") for d in existing if d.get("id") is not None}
        new_id = -1
        while new_id in taken:
            new_id -= 1

        new_dashcard = {
            "id": new_id,
            "card_id": card_id,
            "row": row,
            "col": col,
            "size_x": size_x,
            "size_y": size_y,
        }
        dashcards = list(existing) + [new_dashcard]
        return self._request("PUT", f"/dashboard/{dashboard_id}", {"dashcards": dashcards})

    def replace_dashboard_cards(
        self, dashboard_id: int,
        cards: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Replace all cards on a dashboard. ``cards`` is a list of the same
        shape that Metabase uses for /api/dashboard/:id/cards."""
        return self._request("PUT", f"/dashboard/{dashboard_id}/cards", {"cards": cards})

    def run_card(self, card_id: int) -> dict[str, Any]:
        """Execute the query behind a card. Useful to confirm the SQL is valid."""
        return self._request("POST", f"/card/{card_id}/query", {})

    # ---------- idempotency ----------
    def wait_ready(self, max_seconds: int = 120) -> bool:
        """Poll /api/health until 200."""
        deadline = time.time() + max_seconds
        url = f"{self.base_url}/api/health"
        while time.time() < deadline:
            try:
                r = self.session.get(url, timeout=2)
                if r.status_code == 200:
                    return True
            except requests.RequestException:
                pass
            time.sleep(1)
        return False


__all__ = ["MetabaseClient", "MetabaseError"]
