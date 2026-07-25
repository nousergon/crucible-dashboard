"""
Waitlist health loader for the operator console.

Surfaces the beta-waitlist funnel on the console home page so waitlist health +
growth is checkable at a glance (config#2941). Two data sources:

  1. **Cloudflare D1** — ``metron-waitlist`` and ``vires-waitlist`` databases,
     queried via the D1 REST API for total signups + 7-day signups (excluding
     synthetic-probe rows). Requires a D1:Read-scoped API token stored in
     ``st.secrets["cloudflare"]`` (SSM-backed on the production console
     instance, config#2941 §4).

  2. **GitHub Actions** — the ``waitlist-probe.yml`` workflow's latest completed
     run on ``nousergon/crucible-dashboard``, fetched via the unauthenticated
     public API. Fail-soft to "unknown" on API error.

Both fetches are cached at ~15 min TTL so console loads don't hammer external
APIs (config#2941 §4).

Fail-soft contract:
  - Missing Cloudflare credentials → tile renders "not configured" for signups,
    not an error — the probe tile section still shows workflow status.
  - Probe API error → probe status shows "unknown" with the error excerpt.
  - D1 query error → signups render "error" for the affected product —
    silent-zero would hide a regression (feedback_no_silent_fails).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

import streamlit as st

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 900  # 15 min — config#2941 §4

# Workflow ID for waitlist-probe.yml (nousergon/crucible-dashboard).
_WAITLIST_PROBE_WORKFLOW_ID = 315735595

# D1 database IDs (from marketing-{product}/wrangler.toml).
_D1_DATABASES: dict[str, str] = {
    "Metron": "90604c67-5415-4f85-831b-54cdc3667088",
    "Vires": "27de550b-f47d-4b76-89cf-c94b26aa79c6",
}

# Public domain mapping (config#2941 correction 2026-07-18).
_PRODUCT_DOMAINS: dict[str, str] = {
    "Metron": "metron.nousergon.ai",
    "Vires": "vires.nousergon.ai",  # app lives under /app on this same host
}

# ── Credentials ──────────────────────────────────────────────────────────────


def _cf_credentials() -> tuple[str | None, str | None]:
    """Read Cloudflare account ID + API token from ``st.secrets``.

    Returns ``(account_id, api_token)`` or ``(None, None)`` when the
    credentials are not configured — caller renders a "not configured" tile
    rather than crashing. Production credentials live in
    ``~/.streamlit/secrets.toml`` under ``[cloudflare]`` (SSM-backed on the
    console EC2 instance); local dev sets up the same file or relies on the
    ALPHA_ENGINE_SECRETS_SOURCE=env pattern.

    Deliberately NOT cached — ``st.secrets`` is Streamlit's own cached
    secret manager, so this is effectively memoized at the framework layer.
    No ``os.environ`` reads (guarded by ``test_no_secret_environ_reads.py``).
    """
    try:
        cf = st.secrets.get("cloudflare", {})
        if not isinstance(cf, dict):
            return None, None
        return cf.get("account_id"), cf.get("api_token")
    except (KeyError, AttributeError, TypeError):
        return None, None


# ── Cloudflare D1 query ──────────────────────────────────────────────────────


def _query_d1(
    account_id: str,
    database_id: str,
    sql: str,
    token: str,
) -> list[dict[str, Any]] | None:
    """Execute a read-only SQL query against a Cloudflare D1 database.

    Returns the ``result`` array on success (list of row-dicts), or ``None``
    on error (logged, not raised — the tile renders the degraded state).
    """
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/d1/database/{database_id}/query"
    )
    payload = json.dumps({"sql": sql}).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("waitlist: D1 query failed for db=%s: %s", database_id, exc)
        return None

    if not isinstance(body, dict) or not body.get("success"):
        logger.warning("waitlist: D1 API returned error for db=%s: %s",
                       database_id, body.get("errors", body))
        return None

    result = body.get("result")
    if not isinstance(result, list):
        return None
    return result


def _fetch_product_signups(
    account_id: str, token: str, product: str, database_id: str,
) -> dict[str, Any]:
    """Fetch signup counts for one product's waitlist from Cloudflare D1.

    Returns a dict with ``total``, ``last_7d`` (both int or None on error),
    and ``domain`` for display. Synthentic-probe rows excluded from counts.
    """
    # Compound query: total + 7-day in one round trip.
    sql = (
        "SELECT "
        "  (SELECT COUNT(*) FROM waitlist WHERE source != 'synthetic-probe') AS total, "
        "  (SELECT COUNT(*) FROM waitlist "
        "    WHERE source != 'synthetic-probe' "
        "    AND created_at >= unixepoch('now', '-7 days')) AS last_7d"
    )
    rows = _query_d1(account_id, database_id, sql, token)
    if not rows or not isinstance(rows[0], dict):
        return {"product": product, "total": None, "last_7d": None,
                "domain": _PRODUCT_DOMAINS.get(product, ""), "error": True}

    return {
        "product": product,
        "total": rows[0].get("total"),
        "last_7d": rows[0].get("last_7d"),
        "domain": _PRODUCT_DOMAINS.get(product, ""),
        "error": False,
    }


# ── GitHub Actions probe status ──────────────────────────────────────────────


def _fetch_probe_status() -> dict[str, Any]:
    """Fetch the latest completed ``waitlist-probe`` workflow run.

    Returns a dict with ``conclusion`` (str, "unknown" on error),
    ``timestamp`` (ISO str or None), and ``url``. Uses the unauthenticated
    GitHub public API — runs on a public repo.
    """
    url = (
        f"https://api.github.com/repos/nousergon/crucible-dashboard"
        f"/actions/workflows/{_WAITLIST_PROBE_WORKFLOW_ID}/runs"
        f"?per_page=1&status=completed"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "alpha-engine-dashboard-waitlist"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("waitlist: probe-status fetch failed: %s", exc)
        return {"conclusion": "unknown", "timestamp": None, "url": None,
                "error": str(exc)[:200]}

    runs = body.get("workflow_runs") if isinstance(body, dict) else None
    if not runs:
        return {"conclusion": "unknown", "timestamp": None, "url": None}

    latest = runs[0]
    return {
        "conclusion": latest.get("conclusion", "unknown"),
        "timestamp": latest.get("created_at"),
        "url": latest.get("html_url"),
    }


# ── Public API (cached) ──────────────────────────────────────────────────────


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def load_waitlist_signups() -> dict[str, Any]:
    """Per-product waitlist signup counts from Cloudflare D1 (15-min cache).

    Returns::

        {
            "configured": True | False,   # whether CF credentials are present
            "products": [                  # one entry per product
                {"product": "Metron", "total": 42, "last_7d": 5,
                 "domain": "metron.nousergon.ai", "error": False},
                ...
            ],
            "fetched_at": "2026-07-24T12:00:00+00:00",
        }

    When credentials are missing, ``configured=False`` and the caller renders
    a "not configured" chip — never a crash or an error state.
    """
    account_id, token = _cf_credentials()
    if not account_id or not token:
        return {
            "configured": False,
            "products": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    products = []
    for product, db_id in _D1_DATABASES.items():
        products.append(_fetch_product_signups(account_id, token, product, db_id))

    return {
        "configured": True,
        "products": products,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner=False)
def load_waitlist_probe() -> dict[str, Any]:
    """Latest ``waitlist-probe`` workflow conclusion (15-min cache).

    Returns::

        {
            "conclusion": "success" | "failure" | "unknown",
            "timestamp": "2026-07-24T10:15:00Z" | None,
            "url": "https://github.com/..." | None,
        }

    Fail-soft: API errors return ``conclusion="unknown"`` with the error
    excerpt in the ``error`` field (not set on success).
    """
    return _fetch_probe_status()
