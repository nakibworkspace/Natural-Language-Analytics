"""Auto-provisioning for Metabase.

On the first run of the lab, this module:

  1. Logs in to Metabase via ``POST /api/session`` using the admin
     credentials in ``.env`` (``METABASE_ADMIN_EMAIL`` / ``METABASE_ADMIN_PASSWORD``).
  2. Checks if the Postgres database is already connected. If not,
     adds it via ``POST /api/database``.
  3. Checks if an API key named ``ride-analytics-bot`` already exists.
     If yes, reuses it. If not, creates one via ``POST /api/api-key``.
  4. Persists the API key back into ``.env`` so subsequent runs use the
     API key directly.

The dashboard skill's ``MetabaseClient`` will call ``ensure_ready()`` on
first use, which:
  - Verifies the API key works.
  - If not, runs the provisioning flow above.
  - Caches the key in-memory for the rest of the process lifetime.

This makes the first end-to-end run fully automatic — there is no
Metabase setup wizard and no browser interaction required. On a fresh
Metabase (empty H2 DB) we POST /api/setup to create the admin user +
connect Postgres in a single server-side call.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv, set_key


# Refresh env on every import so the latest .env is visible.
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


API_KEY_NAME = "ride-analytics-bot"


class MetabaseProvisionError(RuntimeError):
    """Raised when auto-provisioning fails."""


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def _base_url() -> str:
    return os.getenv("METABASE_URL", "http://localhost:3000").rstrip("/")


def _request(method: str, path: str, *, session: requests.Session | None = None,
             headers: dict | None = None, json_body: dict | None = None,
             timeout: float = 30.0) -> requests.Response:
    sess = session or requests.Session()
    url = f"{_base_url()}/api{path}"
    return sess.request(method, url, headers=headers or {},
                        json=json_body, timeout=timeout)


def _login_as_admin(session: requests.Session) -> None:
    """Open a session as the admin, bootstrapping the account if needed.

    On the very first run (Metabase H2 DB is empty), we call
    ``POST /api/setup`` to create the admin account, which is the
    **only** server-side way to bootstrap without the browser wizard.
    On subsequent runs we just log in via ``POST /api/session``.

    The ``METABASE_ADMIN_EMAIL`` / ``METABASE_ADMIN_PASSWORD`` values in
    ``.env`` are used both for setup and for the session login.
    """
    email = os.getenv("METABASE_ADMIN_EMAIL")
    password = os.getenv("METABASE_ADMIN_PASSWORD")
    if not email or not password:
        raise MetabaseProvisionError(
            "METABASE_ADMIN_EMAIL and METABASE_ADMIN_PASSWORD must be set in .env."
        )

    # 1. Try logging in first — this works on runs after the first one.
    resp = _request("POST", "/session", session=session,
                    json_body={"username": email, "password": password})
    if resp.status_code == 200:
        session.headers["X-Metabase-Session"] = resp.json()["id"]
        print(f"[metabase-provision] logged in as {email!r}")
        return

    # 2. Maybe Metabase is still pristine — bootstrap with /api/setup.
    # NOTE: Metabase v0.50 expects `site_name` (and `site_locale`) inside
    # a `prefs` sub-object, not at the top level. The Malli schema
    # validates them at the top level, but the endpoint destructures
    # `{:keys [site_name site_locale]} :prefs` — so without `prefs`,
    # validation reports a misleading "site_name must be a non-blank string"
    # while the actual cause is that the field is in the wrong place.
    setup_resp = _request("POST", "/setup", session=session, json_body={
        "token": _setup_token(),
        "user": {
            "email": email,
            "first_name": "Admin",
            "last_name":  "User",
            "password":   password,
        },
        "prefs": {
            "site_name":   os.getenv("MB_SITE_NAME", "Ride Analytics"),
            "site_locale": "en",
        },
        # The `database` and `invite` keys are NOT part of the schema, but
        # Metabase's setup handler tolerates them and uses them if present.
        # We pass them so the user doesn't have to add the Postgres DB
        # in the UI after setup completes.
        "database": _initial_database_payload(),
        "invite":   None,
    })
    if setup_resp.status_code in (200, 204):
        # /api/setup returns 204 + Set-Cookie: metabase.SESSION. The cookie
        # is captured by `requests` automatically when we share a Session,
        # but Metabase's API clients expect the bearer-style X-Metabase-Session
        # header on later calls. The reliable thing is to immediately POST
        # /api/session with the credentials we just used to create the user.
        login_resp = _request(
            "POST", "/session", session=session,
            json_body={"username": email, "password": password},
        )
        if login_resp.status_code != 200:
            raise MetabaseProvisionError(
                f"/api/setup succeeded but /api/session immediately after "
                f"failed: {login_resp.status_code} {login_resp.text[:200]}"
            )
        session.headers["X-Metabase-Session"] = login_resp.json()["id"]
        print(f"[metabase-provision] bootstrapped Metabase via /api/setup as {email!r}")
        return

    # 3. Last resort — surface the error.
    raise MetabaseProvisionError(
        f"Could not log in as {email!r} and /api/setup refused. "
        f"Login: {resp.status_code} {resp.text[:200]}\n"
        f"Setup: {setup_resp.status_code} {setup_resp.text[:200]}\n"
        "If you've already completed the Metabase setup wizard in the browser, "
        "set METABASE_ADMIN_EMAIL / METABASE_ADMIN_PASSWORD in .env to the same "
        "credentials you used there."
    )


def _setup_token() -> str:
    """Return the one-time setup token. Metabase v0.42+ requires it.

    The JSON key has been historically spelled both ``setup_token`` and
    ``setup-token`` across versions; we try both and prefer the hyphenated
    form which is what current Metabase (v0.50.x) actually returns.
    """
    resp = _request("GET", "/session/properties")
    if resp.status_code != 200:
        return ""
    body = resp.json()
    # Current spelling (v0.50+): "setup-token".
    token = body.get("setup-token") or body.get("setup_token") or ""
    return token or ""


def _initial_database_payload() -> dict:
    """Payload for adding the Postgres DB during /api/setup."""
    return {
        "engine": "postgres",
        "name": os.getenv("METABASE_PG_DB", "ride_analytics"),
        "details": {
            "host":     os.getenv("METABASE_PG_HOST", "postgres"),
            "port":     int(os.getenv("METABASE_PG_PORT", "5432")),
            "dbname":   os.getenv("METABASE_PG_DB", "ride_analytics"),
            "user":     os.getenv("METABASE_PG_USER", "postgres"),
            "password": os.getenv("METABASE_PG_PASSWORD", "postgres"),
            "ssl":      False,
        },
        "auto_run_queries": True,
    }


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------
def _list_databases(session: requests.Session) -> list[dict[str, Any]]:
    resp = _request("GET", "/database", session=session)
    if resp.status_code != 200:
        raise MetabaseProvisionError(f"GET /database failed: {resp.status_code} {resp.text[:200]}")
    # Metabase v0.49+ wraps list endpoints in {"data": [...]} on success.
    # Older versions returned a bare array. Handle both.
    body = resp.json()
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    if isinstance(body, list):
        return body
    raise MetabaseProvisionError(
        f"GET /database returned unexpected shape: {type(body).__name__}"
    )


def _ensure_database_connected(session: requests.Session) -> dict[str, Any]:
    """Make sure the Postgres database (METABASE_PG_DB) is connected.

    Returns the database metadata dict. Idempotent: if it's already there,
    returns the existing one.
    """
    target_name = os.getenv("METABASE_PG_DB", "ride_analytics")
    existing = _list_databases(session)
    for db in existing:
        if db.get("name") == target_name:
            print(f"[metabase-provision] postgres already connected: id={db['id']} name={target_name!r}")
            return db

    # Connect via the "candidate" endpoint with auto-detection.
    payload = {
        "engine": "postgres",
        "name": target_name,
        "details": {
            "host":     os.getenv("METABASE_PG_HOST", "postgres"),
            "port":     int(os.getenv("METABASE_PG_PORT", "5432")),
            "dbname":   os.getenv("METABASE_PG_DB", "ride_analytics"),
            "user":     os.getenv("METABASE_PG_USER", "postgres"),
            "password": os.getenv("METABASE_PG_PASSWORD", "postgres"),
            "ssl":      False,
        },
        "auto_run_queries": True,
        "is_full_sync": True,
    }
    resp = _request("POST", "/database", session=session, json_body=payload)
    if resp.status_code not in (200, 201):
        raise MetabaseProvisionError(
            f"POST /database failed: {resp.status_code} {resp.text[:300]}"
        )
    db = resp.json()
    print(f"[metabase-provision] connected postgres: id={db['id']} name={target_name!r}")
    return db


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------
def _list_api_keys(session: requests.Session) -> list[dict[str, Any]]:
    """List existing API keys. (Metabase v0.49+ endpoint)."""
    resp = _request("GET", "/api-key", session=session)
    if resp.status_code == 404:
        # Older Metabase versions don't have this endpoint; treat as empty.
        return []
    if resp.status_code != 200:
        raise MetabaseProvisionError(f"GET /api-key failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()


def _create_api_key(session: requests.Session, name: str, group_id: int) -> str:
    """Create a new API key; returns the new key string.

    Metabase v0.49+ returns the secret in ``unmasked_key`` on creation.
    The ``key`` field was the historical name (and is what some older
    API clients still expect). We try both for forward compatibility.
    """
    payload = {"name": name, "group_id": group_id}
    resp = _request("POST", "/api-key", session=session, json_body=payload)
    if resp.status_code not in (200, 201):
        raise MetabaseProvisionError(
            f"POST /api-key failed: {resp.status_code} {resp.text[:300]}"
        )
    body = resp.json()
    key = body.get("unmasked_key") or body.get("key")
    if not key:
        raise MetabaseProvisionError(
            f"POST /api-key returned 200 but no key in body: {body}"
        )
    return key


def _ensure_api_key(session: requests.Session) -> str:
    """Reuse ``ride-analytics-bot`` if it exists, otherwise create it.

    Returns the raw API key string.
    """
    existing = _list_api_keys(session)
    for k in existing:
        if k.get("name") == API_KEY_NAME:
            # Older API responses don't include the key (only metadata). We
            # have to delete + recreate to recover the secret. The dashboard
            # skill will write the new key back to .env.
            kid = k.get("id")
            print(f"[metabase-provision] {API_KEY_NAME!r} already exists (id={kid}); recreating to retrieve secret")
            del_resp = _request("DELETE", f"/api-key/{kid}", session=session)
            if del_resp.status_code not in (200, 204):
                raise MetabaseProvisionError(
                    f"DELETE /api-key/{kid} failed: {del_resp.status_code} {del_resp.text[:200]}"
                )
            break

    # Use the "Administrators" group id (Metabase default = 2). On a fresh
    # install the "All Users" pseudo-group (id 1) is NOT a valid group for
    # API keys — Metabase rejects it with 400. We prefer the real admin
    # group because that gives the dashboard skill full access to Postgres
    # and the dashboards it creates.
    group_id = 2  # Metabase default for "Administrators" on a fresh install
    resp = _request("GET", "/permissions/group", session=session)
    if resp.status_code == 200:
        groups = resp.json()
        admins = [g for g in groups if g.get("is_admin_group")] or \
                 [g for g in groups if g.get("name") == "Administrators"]
        if admins:
            group_id = admins[0]["id"]

    key = _create_api_key(session, API_KEY_NAME, group_id)
    print(f"[metabase-provision] created API key {API_KEY_NAME!r} (group_id={group_id})")
    return key


# ---------------------------------------------------------------------------
# .env persistence
# ---------------------------------------------------------------------------
def _persist_api_key(key: str) -> None:
    """Write the API key back to .env so future runs can reuse it."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path.touch()
    set_key(str(env_path), "METABASE_API_KEY", key, quote_mode="never")
    # dotenv can leave an extra blank line; that's harmless.
    print(f"[metabase-provision] persisted METABASE_API_KEY to {env_path}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _wait_for_metabase(max_seconds: int = 180) -> None:
    """Block until Metabase's /api/health returns 200."""
    import time
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        try:
            resp = _request("GET", "/health", timeout=2.0)
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(2)
    raise MetabaseProvisionError(
        f"Metabase is not reachable at {_base_url()}/ after {max_seconds}s. "
        "Check that the container is healthy: docker compose ps"
    )


def ensure_ready() -> str:
    """Make sure Metabase is connected to Postgres AND we have a valid API key.

    Returns the API key (in-memory). Persists it to .env if it was created.
    """
    # 0. Make sure Metabase is up at all.
    _wait_for_metabase()

    # 1. Try the existing key first.
    existing = os.getenv("METABASE_API_KEY", "").strip()
    if existing:
        # Verify it actually works; if not, fall through to provisioning.
        resp = _request("GET", "/database", headers={"x-api-key": existing})
        if resp.status_code == 200:
            print("[metabase-provision] existing METABASE_API_KEY is valid")
            return existing
        print(f"[metabase-provision] existing METABASE_API_KEY failed: {resp.status_code}, re-provisioning")

    # 2. Log in as admin.
    session = requests.Session()
    _login_as_admin(session)

    # 3. Connect Postgres.
    _ensure_database_connected(session)

    # 4. Create / refresh API key.
    key = _ensure_api_key(session)

    # 5. Persist for next run.
    _persist_api_key(key)

    # Refresh in-process env so downstream code picks it up.
    os.environ["METABASE_API_KEY"] = key
    return key


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m tools.metabase_provision``."""
    print("[metabase-provision] starting...")
    try:
        key = ensure_ready()
        print(f"[metabase-provision] DONE. METABASE_API_KEY={key}")
        return 0
    except MetabaseProvisionError as e:
        print(f"[metabase-provision] FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())