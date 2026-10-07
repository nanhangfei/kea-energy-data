#!/usr/bin/env python3
"""Provision Metabase and its backend-serving and pipeline data sources.

Metabase stores its database connections in its own application database, not
in environment variables, so a fresh container otherwise needs someone to click
through the setup wizard. This does it over the API instead, and is safe to
re-run: if Metabase is already set up it logs in, and if the connection already
exists it leaves it alone.

Reads credentials from data/.env. Standard library only.

    python3 analysis/metabase/metabase_setup.py           # or: make dashboards, which runs it first
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MB = os.environ.get("METABASE_URL", "http://localhost:3002")
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
SERVING_DB_NAME = "Kea serving"
PIPELINE_DB_NAME = "Kea pipeline"


def load_env() -> dict[str, str]:
    out = {}
    if not ENV_FILE.exists():
        if os.environ.get("METABASE_SERVING_ONLY") == "1":
            return {k: os.environ[k] for k in ENV_OVERRIDES if os.environ.get(k)}
        sys.exit(f"{ENV_FILE} not found — cp .env.example .env first")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out | {k: os.environ[k] for k in ENV_OVERRIDES if os.environ.get(k)}


#: Settable from the environment, so the same setup provisions the AWS demo's
#: Metabase, whose backend database is RDS (SSL, its own read-only login) and
#: which has no pipeline sandbox: METABASE_SERVING_ONLY=1 skips that connection.
ENV_OVERRIDES = ("METABASE_ADMIN_EMAIL", "METABASE_ADMIN_PASSWORD", "KEA_APP_PASSWORD",
                 "KEA_BACKEND_DB_HOST", "KEA_BACKEND_DB_PORT", "KEA_BACKEND_DB_USER",
                 "KEA_BACKEND_DB_SSL", "METABASE_SERVING_ONLY")


def call(path: str, data=None, session: str | None = None, method: str | None = None):
    req = urllib.request.Request(
        f"{MB}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json",
                 **({"X-Metabase-Session": session} if session else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise SystemExit(f"{method or 'GET'} {path} -> HTTP {e.code}\n{detail}") from None


def wait_healthy(timeout=420):
    """Poll /api/health until it reports ok.

    While Metabase migrates its own application database the endpoint answers
    HTTP 503 with {"status":"initializing","progress":N} — an expected state on
    first boot, not a failure, so every error here is treated as "keep waiting".
    """
    print("waiting for Metabase to become healthy (first boot migrates its own DB)...")
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        body = None
        try:
            with urllib.request.urlopen(f"{MB}/api/health", timeout=15) as r:
                body = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:            # 503 while initializing
            try:
                body = json.loads(e.read().decode() or "{}")
            except Exception:
                body = {"status": f"http {e.code}"}
        except Exception as e:                          # not listening yet
            body = {"status": type(e).__name__}
        if body.get("status") == "ok":
            print("  healthy")
            return
        state = f"  {body.get('status')} {body.get('progress', '')}".rstrip()
        if state != last:
            print(state)
            last = state
        time.sleep(5)
    sys.exit("Metabase did not become healthy in time — check `docker compose logs metabase`")


def main() -> None:
    env = load_env()
    email = env.get("METABASE_ADMIN_EMAIL", "admin@example.com")
    password = env.get("METABASE_ADMIN_PASSWORD")
    if not password:
        sys.exit("METABASE_ADMIN_PASSWORD is not set in .env")
    if not env.get("KEA_APP_PASSWORD"):
        sys.exit("KEA_APP_PASSWORD is not set in .env")

    # The serving dashboards read the same PostgreSQL database as the backend.
    # Metabase runs in the Data Docker network, so it reaches the backend's
    # published port through the host rather than relying on a Compose network
    # name owned by another project.
    serving_conn = {
        "engine": "postgres",
        "name": SERVING_DB_NAME,
        "details": {
            "host": env.get("KEA_BACKEND_DB_HOST", "host.docker.internal"),
            "port": int(env.get("KEA_BACKEND_DB_PORT", "5432")),
            "dbname": "kea",
            "user": env.get("KEA_BACKEND_DB_USER", "kea_app"),
            "password": env["KEA_APP_PASSWORD"],
            "ssl": env.get("KEA_BACKEND_DB_SSL") == "1",
            **({"ssl-mode": "require"} if env.get("KEA_BACKEND_DB_SSL") == "1" else {}),
            "tunnel-enabled": False,
        },
    }
    serving_only = env.get("METABASE_SERVING_ONLY") == "1"
    # Stage 2/3 and Stage 5 inspect source/lake internals that deliberately do
    # not belong in the backend database. Keep those dashboards on the local
    # pipeline sandbox instead of breaking them when serving moves.
    pipeline_conn = {
        "engine": "postgres",
        "name": PIPELINE_DB_NAME,
        "details": {
            "host": "kea-source-db",
            "port": 5432,
            "dbname": "kea",
            "user": "kea",
            "password": env.get("KEA_SOURCE_PASSWORD", ""),
            "ssl": False,
            "tunnel-enabled": False,
        },
    }

    wait_healthy()
    token = call("/api/session/properties").get("setup-token")

    if token:
        print("first run — creating the admin user")
        res = call("/api/setup", {
            "token": token,
            "user": {"first_name": "Kea", "last_name": "Data", "email": email,
                     "password": password, "site_name": "Kea Energy"},
            "prefs": {"site_name": "Kea Energy", "allow_tracking": False},
        })
        session = res.get("id") or call(
            "/api/session", {"username": email, "password": password}
        )["id"]
    else:
        print("already set up — logging in")
        session = call("/api/session", {"username": email, "password": password})["id"]

    # Add the connection as its own step rather than inline in /api/setup: that
    # endpoint accepts a `database` key but silently ignores it on v0.63, which
    # leaves a Metabase connected to nothing but its own sample data.
    existing = call("/api/database", session=session)
    existing = existing.get("data") if isinstance(existing, dict) else existing

    # Preserve the existing database id so Stage 2/3 cards keep pointing at
    # the pipeline database. New/rebuilt serving cards resolve the new backend
    # connection by name.
    old_serving = next((d for d in existing if d["name"] == SERVING_DB_NAME), None)
    pipeline = next((d for d in existing if d["name"] == PIPELINE_DB_NAME), None)
    if old_serving and not pipeline:
        details = old_serving.get("details") or {}
        # kea-source-db was called kea-serving-db until 2026-09-12.
        if details.get("host") in ("kea-serving-db", "kea-source-db"):
            call(f"/api/database/{old_serving['id']}", pipeline_conn,
                 session=session, method="PUT")
            print(f"  renamed existing connection to '{PIPELINE_DB_NAME}'")
            old_serving = None
            existing = call("/api/database", session=session)
            existing = existing.get("data") if isinstance(existing, dict) else existing

    def upsert(conn: dict) -> None:
        current = next((d for d in existing if d["name"] == conn["name"]), None)
        if current:
            call(f"/api/database/{current['id']}", conn,
                 session=session, method="PUT")
            print(f"  updated '{conn['name']}'")
        else:
            call("/api/database", conn, session=session)
            print(f"  connected '{conn['name']}'")

    if not serving_only:
        upsert(pipeline_conn)
    upsert(serving_conn)

    # A local day is 46 or 50 half hours across a DST change. Grouping by day in
    # UTC would cut NZ days in the wrong place, so make Metabase agree with the
    # rest of the project.
    call("/api/setting/report-timezone", {"value": "Pacific/Auckland"},
         session=session, method="PUT")
    print("  report-timezone = Pacific/Auckland")

    dbs = call("/api/database", session=session)
    dbs = dbs.get("data") if isinstance(dbs, dict) else dbs
    print("\nconnected databases:")
    for d in dbs:
        print(f"  {d['name']:20} {d['engine']}")
    print(f"\nMetabase: {MB}   login: {email}")


if __name__ == "__main__":
    main()
