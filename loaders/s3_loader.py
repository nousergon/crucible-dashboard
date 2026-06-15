"""
S3 data loading utilities for the Alpha Engine Dashboard.
All data-fetching functions use @st.cache_data with TTLs from config.yaml.
Credentials come from the EC2 IAM role (no explicit creds needed).

Naming conventions:
  - load_*()  — fetch data from S3 and return parsed objects
  - get_*()   — return local/computed values (no S3 I/O)
  - _fetch_*  — internal helpers that combine S3 I/O + parsing
"""

import functools
import io
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

import boto3
import pandas as pd
import streamlit as st
import yaml

from shared.constants import DEFAULT_CACHE_TTL_SECONDS, ISO_DATE_PATTERN

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# S3 error tracking
# ---------------------------------------------------------------------------

_recent_s3_errors: list[dict] = []
_MAX_S3_ERRORS = 50


def _record_s3_error(bucket: str, key: str, error_type: str, message: str) -> None:
    """Append an error record (capped at _MAX_S3_ERRORS)."""
    _recent_s3_errors.append({
        "timestamp": datetime.utcnow().isoformat(),
        "bucket": bucket,
        "key": key,
        "error_type": error_type,
        "message": str(message)[:200],
    })
    if len(_recent_s3_errors) > _MAX_S3_ERRORS:
        _recent_s3_errors.pop(0)


def get_recent_s3_errors() -> list[dict]:
    """Return the recent S3 error log (up to 50 entries)."""
    return list(_recent_s3_errors)

# ---------------------------------------------------------------------------
# Config loading (module-level, cached forever via lru_cache-style singleton)
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
)

_config_cache: dict | None = None
_config_mtime: float = 0.0


def load_config() -> dict:
    """Load and return the parsed config.yaml.

    Honors DASHBOARD_CONFIG_PATH env var for overriding the default path.
    Automatically reloads if the file has been modified since last read.
    """
    global _config_cache, _config_mtime
    config_path = os.environ.get("DASHBOARD_CONFIG_PATH", _DEFAULT_CONFIG_PATH)
    try:
        current_mtime = os.path.getmtime(config_path)
    except OSError:
        current_mtime = 0.0
    if _config_cache is None or current_mtime > _config_mtime:
        try:
            with open(config_path) as f:
                _config_cache = yaml.safe_load(f)
            _config_mtime = current_mtime
        except FileNotFoundError:
            logger.error(
                "Config file not found: %s — using defaults. "
                "Copy config.yaml.example to config.yaml to configure.",
                config_path,
            )
            _config_cache = {
                "s3": {"research_bucket": "alpha-engine-research"},
                "paths": {"research_db": "research.db"},
                "cache_ttl": {},
            }
        except yaml.YAMLError as e:
            logger.error("Config file parse error: %s — using defaults", e)
            _config_cache = {
                "s3": {"research_bucket": "alpha-engine-research"},
                "paths": {"research_db": "research.db"},
                "cache_ttl": {},
            }
    return _config_cache


# Convenience accessors used by cached functions below
def _ttl(key: str) -> int:
    return load_config()["cache_ttl"].get(key, DEFAULT_CACHE_TTL_SECONDS)


def _research_bucket() -> str:
    return load_config()["s3"]["research_bucket"]


def _trades_bucket() -> str:
    return load_config()["s3"]["trades_bucket"]


# ---------------------------------------------------------------------------
# S3 client helper
# ---------------------------------------------------------------------------


def get_s3_client() -> Any:
    """Return a boto3 S3 client. Uses EC2 IAM role automatically."""
    return boto3.client("s3")


# ---------------------------------------------------------------------------
# Low-level S3 helpers (not cached — called by cached wrappers below)
# ---------------------------------------------------------------------------


_S3_MAX_RETRIES = 3
_S3_RETRY_BACKOFF_BASE = 1.0  # seconds


def _s3_get_object(bucket: str, key: str) -> bytes | None:
    """Raw GetObject call with retry for transient errors.

    Returns the response body bytes or None on error.
    Retries on ConnectionError, TimeoutError, and throttling (503/SlowDown).
    Does NOT retry on NoSuchKey or AccessDenied (non-transient).
    """
    import time as _time

    client = get_s3_client()
    last_error = None

    for attempt in range(1, _S3_MAX_RETRIES + 1):
        try:
            response = client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read()
        except client.exceptions.NoSuchKey:
            return None  # not an error — key simply doesn't exist
        except client.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            # Retry on throttling; fail fast on permission/not-found errors
            if error_code in ("SlowDown", "ServiceUnavailable", "InternalError"):
                last_error = e
                if attempt < _S3_MAX_RETRIES:
                    wait = _S3_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        "S3 throttled for %s/%s (attempt %d/%d, %s) — retrying in %.1fs",
                        bucket, key, attempt, _S3_MAX_RETRIES, error_code, wait,
                    )
                    _time.sleep(wait)
                    continue
            logger.error(
                "S3 ClientError for %s/%s: %s (non-retryable)", bucket, key, error_code,
            )
            _record_s3_error(bucket, key, f"ClientError:{error_code}", str(e))
            return None
        except (ConnectionError, TimeoutError) as e:
            last_error = e
            if attempt < _S3_MAX_RETRIES:
                wait = _S3_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "S3 %s for %s/%s (attempt %d/%d) — retrying in %.1fs",
                    type(e).__name__, bucket, key, attempt, _S3_MAX_RETRIES, wait,
                )
                _time.sleep(wait)
                continue
            logger.error(
                "S3 %s for %s/%s after %d attempts",
                type(e).__name__, bucket, key, _S3_MAX_RETRIES,
            )
            _record_s3_error(bucket, key, type(e).__name__, str(e))
            return None
        except Exception as e:
            logger.error("S3 unexpected error for %s/%s", bucket, key, exc_info=True)
            _record_s3_error(bucket, key, type(e).__name__, str(e))
            return None

    # Exhausted retries
    logger.error("S3 request for %s/%s failed after %d retries: %s", bucket, key, _S3_MAX_RETRIES, last_error)
    _record_s3_error(bucket, key, "RetriesExhausted", str(last_error))
    return None


def _fetch_s3_json(bucket: str, key: str) -> dict | list | None:
    """Fetch an S3 object and parse as JSON with unified error tracking.

    Returns None on missing key or any failure (errors are logged and recorded).
    Delegates S3 I/O to _s3_get_object so error handling is not duplicated.
    """
    raw = _s3_get_object(bucket, key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("JSON parse failed for %s/%s: %s", bucket, key, e)
        _record_s3_error(bucket, key, "JSONParseError", str(e))
        return None


def with_s3_error_tracking(fallback: Any = None):
    """Decorator that wraps a function with S3 error logging and tracking.

    On any exception, logs the error, records it via _record_s3_error
    (using the function name as context), and returns *fallback*.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                context = fn.__name__
                logger.error("%s failed: %s", context, e, exc_info=True)
                _record_s3_error("unknown", context, type(e).__name__, str(e))
                return fallback
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Cached public API
# ---------------------------------------------------------------------------


@st.cache_data(ttl=_ttl("signals"))
def list_s3_prefixes(bucket: str, prefix: str) -> list[str]:
    """
    Return a sorted list of date-like sub-prefixes under *prefix*.
    E.g., for prefix='signals/' returns ['2024-01-15', '2024-01-16', ...].
    """
    try:
        client = get_s3_client()
        paginator = client.get_paginator("list_objects_v2")
        prefixes: set[str] = set()
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                p = cp.get("Prefix", "")
                stripped = p[len(prefix):].strip("/")
                if ISO_DATE_PATTERN.match(stripped):
                    prefixes.add(stripped)
            # Also handle keys directly (no trailing slash)
            for obj in page.get("Contents", []):
                k = obj.get("Key", "")
                rel = k[len(prefix):]
                seg = rel.split("/")[0]
                if ISO_DATE_PATTERN.match(seg):
                    prefixes.add(seg)
        return sorted(prefixes)
    except Exception as e:
        logger.error("Failed to list S3 prefixes %s/%s: %s", bucket, prefix, e)
        _record_s3_error(bucket, prefix, type(e).__name__, str(e))
        return []


@st.cache_data(ttl=_ttl("signals"))
def download_s3_json(bucket: str, key: str) -> dict | list | None:
    """Download and parse a JSON file from S3. Returns None on failure."""
    return _fetch_s3_json(bucket, key)


@st.cache_data(ttl=_ttl("trades"))
def download_s3_csv(bucket: str, key: str) -> pd.DataFrame | None:
    """Download a CSV from S3 and return a DataFrame. Returns None on failure."""
    raw = _s3_get_object(bucket, key)
    if raw is None:
        return None
    try:
        return pd.read_csv(io.BytesIO(raw))
    except Exception as e:
        logger.warning("CSV parse failed for %s/%s: %s", bucket, key, e)
        _record_s3_error(bucket, key, "CSVParseError", str(e))
        return None


@st.cache_data(ttl=_ttl("research"))
def download_s3_text(bucket: str, key: str) -> str | None:
    """Download a text file from S3 and return its content. Returns None on failure."""
    raw = _s3_get_object(bucket, key)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError) as e:
        logger.warning("Text decode failed for %s/%s: %s", bucket, key, e)
        _record_s3_error(bucket, key, "DecodeError", str(e))
        return None


@with_s3_error_tracking(fallback=False)
def download_s3_binary(bucket: str, key: str, local_path: str) -> bool:
    """Download a binary file from S3 to *local_path*. Returns True on success."""
    client = get_s3_client()
    client.download_file(bucket, key, local_path)
    return True


@st.cache_data(ttl=_ttl("signals"))
def get_latest_prefix(bucket: str, prefix: str) -> str | None:
    """
    List all keys under *prefix*, extract YYYY-MM-DD date segments,
    and return the most recent one (sorted descending). Returns None if none found.
    """
    dates = list_s3_prefixes(bucket, prefix)
    if not dates:
        return None
    return sorted(dates, reverse=True)[0]


@st.cache_data(ttl=_ttl("signals"))
def check_key_exists(bucket: str, key: str) -> bool:
    """Return True if the given S3 key exists."""
    try:
        client = get_s3_client()
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception as e:
        logger.debug("check_key_exists %s/%s: %s", bucket, key, e)
        return False


# ---------------------------------------------------------------------------
# S3 path builders (centralize hardcoded prefixes)
# ---------------------------------------------------------------------------

_PREDICTOR_PREDICTIONS_PREFIX = "predictor/predictions"
_PREDICTOR_METRICS_PREFIX = "predictor/metrics"
_CONFIG_PREFIX = "config"
_POPULATION_KEY = "population/latest.json"


def _predictions_key(date_str: str | None = None) -> str:
    if date_str:
        return f"{_PREDICTOR_PREDICTIONS_PREFIX}/{date_str}.json"
    return f"{_PREDICTOR_PREDICTIONS_PREFIX}/latest.json"


def _order_book_key(date_str: str) -> str:
    return f"order_books/{date_str}/summary.json"


# ---------------------------------------------------------------------------
# Convenience wrappers bound to configured buckets / paths
# ---------------------------------------------------------------------------


def load_signals_json(date_str: str) -> dict | None:
    """Load signals.json for a given date from the research bucket."""
    cfg = load_config()
    key = cfg["paths"]["signals"].format(date=date_str)
    return download_s3_json(_research_bucket(), key)


@st.cache_data(ttl=_ttl("signals"))
def load_report_card(date_str: str | None = None) -> dict | None:
    """Load the evaluator Report Card v2 (the 7-tile MetricRecord substrate).

    Reads ``evaluator/{date}/report_card.json`` written by the
    ``alpha-engine-evaluator`` grading Lambda. ``date_str=None`` resolves the
    most recent available cycle. Returns the parsed card (which carries its own
    ``_provenance.run_date``) or None when no card has been published yet.
    """
    bucket = _research_bucket()
    if date_str is None:
        date_str = get_latest_prefix(bucket, "evaluator/")
        if date_str is None:
            return None
    return download_s3_json(bucket, f"evaluator/{date_str}/report_card.json")


@st.cache_data(ttl=_ttl("signals"))
def load_action_plan(date_str: str | None = None) -> dict | None:
    """Load the Director's weekly action plan (Layer C advisory output).

    Reads ``director/{date}/action_plan.json`` written by the
    ``alpha-engine-evaluator-director`` Lambda (the final Saturday-pipeline
    task, once ``DIRECTOR_ENABLED`` is on). ``date_str=None`` resolves the most
    recent available plan. Returns the parsed ``DirectorWeeklyActionPlan`` dict
    or None when no plan has been published yet (e.g. the Director is still
    dormant pre-flip).
    """
    bucket = _research_bucket()
    if date_str is None:
        date_str = get_latest_prefix(bucket, "director/")
        if date_str is None:
            return None
    return download_s3_json(bucket, f"director/{date_str}/action_plan.json")


@st.cache_data(ttl=_ttl("signals"))
def load_carryover_ledger() -> dict | None:
    """Load the Director's carry-over ledger.

    Reads the single, non-date-scoped object ``director/carryover_ledger.json``
    — the upsert-by-id ledger the Director merges each week (the system-level
    "reminders must be written down" surface). Returns ``{"updated": str,
    "items": [...]}`` or None when no plan has ever run.
    """
    return download_s3_json(_research_bucket(), "director/carryover_ledger.json")


def load_trades_full() -> pd.DataFrame | None:
    """Load trades_full.csv from the executor bucket."""
    cfg = load_config()
    key = cfg["paths"]["trades_full"]
    return download_s3_csv(_trades_bucket(), key)


def load_eod_pnl() -> pd.DataFrame | None:
    """Load eod_pnl.csv from the executor bucket."""
    cfg = load_config()
    key = cfg["paths"]["eod_pnl"]
    return download_s3_csv(_trades_bucket(), key)


@st.cache_data(ttl=_ttl("trades"))
def load_uptime_history(max_sessions: int = 20) -> list[dict]:
    """List recent uptime/*.json files and load the most recent `max_sessions`.

    Returns a list sorted oldest → newest. Each dict matches the schema
    written by `alpha-engine/executor/uptime_tracker.py`. Non-trading-day
    sentinel records (`{date, skipped}`) are dropped.
    """
    bucket = _trades_bucket()
    client = get_s3_client()
    try:
        resp = client.list_objects_v2(Bucket=bucket, Prefix="uptime/")
    except Exception as e:
        logger.warning("list uptime/ failed: %s", e)
        _record_s3_error(bucket, "uptime/", type(e).__name__, str(e))
        return []

    keys = sorted(
        (obj["Key"] for obj in resp.get("Contents", []) if obj["Key"].endswith(".json")),
        reverse=True,
    )[:max_sessions]

    records: list[dict] = []
    for key in keys:
        data = _fetch_s3_json(bucket, key)
        if isinstance(data, dict):
            records.append(data)

    records = [r for r in records if "connected_minutes" in r]
    records.sort(key=lambda r: r.get("date", ""))
    return records


@st.cache_data(ttl=_ttl("research"))
def load_latest_grading() -> dict | None:
    """Return the newest `backtest/{date}/grading.json` from the research bucket.

    Scans `backtest/` for date-stamped directories, finds the most recent
    one that actually contains a `grading.json`, and returns the parsed
    dict with a `_run_date` field added. Returns None if nothing found.
    """
    bucket = _research_bucket()
    date_re = re.compile(r"^backtest/(\d{4}-\d{2}-\d{2})/")

    date_keys: set[str] = set()
    continuation: str | None = None
    client = get_s3_client()
    try:
        while True:
            kwargs: dict = {"Bucket": bucket, "Prefix": "backtest/", "Delimiter": "/"}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            resp = client.list_objects_v2(**kwargs)
            for cp in resp.get("CommonPrefixes") or []:
                m = date_re.match(cp["Prefix"])
                if m:
                    date_keys.add(m.group(1))
            if not resp.get("IsTruncated"):
                break
            continuation = resp.get("NextContinuationToken")
    except Exception as e:
        logger.warning("list backtest/ failed: %s", e)
        _record_s3_error(bucket, "backtest/", type(e).__name__, str(e))
        return None

    for d in sorted(date_keys, reverse=True):
        key = f"backtest/{d}/grading.json"
        data = _fetch_s3_json(bucket, key)
        if isinstance(data, dict):
            data["_run_date"] = d
            return data
    return None


@st.cache_data(ttl=_ttl("research"))
def load_latest_provenance_grounding() -> dict | None:
    """Return the newest `backtest/{date}/provenance_grounding.json` from
    the research bucket.

    Per-agent tool-call + input-trace metrics emitted by the backtester
    evaluator on Saturday SF runs (alpha-engine-backtester#148). Companion
    to ``load_latest_grading`` — same scan-for-date-stamped-dir pattern,
    different filename.
    """
    bucket = _research_bucket()
    date_re = re.compile(r"^backtest/(\d{4}-\d{2}-\d{2})/")

    date_keys: set[str] = set()
    continuation: str | None = None
    client = get_s3_client()
    try:
        while True:
            kwargs: dict = {"Bucket": bucket, "Prefix": "backtest/", "Delimiter": "/"}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            resp = client.list_objects_v2(**kwargs)
            for cp in resp.get("CommonPrefixes") or []:
                m = date_re.match(cp["Prefix"])
                if m:
                    date_keys.add(m.group(1))
            if not resp.get("IsTruncated"):
                break
            continuation = resp.get("NextContinuationToken")
    except Exception as e:
        logger.warning("list backtest/ for provenance_grounding failed: %s", e)
        _record_s3_error(bucket, "backtest/", type(e).__name__, str(e))
        return None

    for d in sorted(date_keys, reverse=True):
        key = f"backtest/{d}/provenance_grounding.json"
        data = _fetch_s3_json(bucket, key)
        if isinstance(data, dict):
            data["_run_date"] = d
            return data
    return None


def load_scoring_weights() -> dict | None:
    """Load current scoring_weights.json from the research bucket."""
    cfg = load_config()
    key = cfg["paths"]["scoring_weights"]
    return download_s3_json(_research_bucket(), key)


def load_scoring_weights_history() -> list[dict]:
    """
    Load all scoring weight history files and return as a list of dicts,
    each containing the date and weight values, sorted ascending by date.
    """
    cfg = load_config()
    prefix = cfg["paths"]["scoring_weights_history_prefix"]
    dates = list_s3_prefixes(_research_bucket(), prefix)
    history = []
    for date_str in sorted(dates):
        key = f"{prefix}{date_str}.json"
        data = download_s3_json(_research_bucket(), key)
        if data and isinstance(data, dict):
            data["updated_at"] = date_str
            history.append(data)
    return history


@st.cache_data(ttl=_ttl("research"))
def load_executor_params() -> dict | None:
    """Load the LIVE `config/executor_params.json` from the research
    bucket — the auto-tuned params the executor's
    `_load_executor_params_from_s3` reads at cold-start.

    Closes ROADMAP L234 — operator can now see effective
    `min_score_to_enter` / `max_position_pct` / `atr_multiplier` on
    the dashboard rather than having to `tail /var/log/executor.log
    | grep "Loaded executor params from S3"`. Companion to the
    existing `load_executor_params_history()` (audit trail of past
    promotions) and `load_scoring_weights()` (research's analogous
    artifact).

    Schema (matches `optimizer/executor_optimizer.py::apply()` output):
      min_score / max_position_pct / atr_multiplier /
      time_decay_reduce_days / time_decay_exit_days / profit_take_pct
      + metadata: updated_at, best_sharpe, best_alpha,
      improvement_pct, n_combos_tested, manual_override.
    """
    return download_s3_json(_research_bucket(), "config/executor_params.json")


@st.cache_data(ttl=_ttl("research"))
def load_executor_params_history() -> list[dict]:
    """Return executor_params_history records sorted oldest → newest.

    Reads `config/executor_params_history/{YYYY-MM-DD}.json` — written by
    the backtester executor optimizer (`alpha-engine-backtester/optimizer/
    executor_optimizer.py`) on each promotion. Each record carries the
    parameter values plus best_sharpe / improvement_pct / n_combos_tested
    metadata that explains the optimizer's choice.
    """
    bucket = _research_bucket()
    prefix = f"{_CONFIG_PREFIX}/executor_params_history/"
    client = get_s3_client()
    try:
        resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    except Exception as e:
        logger.warning("list executor_params_history failed: %s", e)
        _record_s3_error(bucket, prefix, type(e).__name__, str(e))
        return []

    keys = sorted(
        obj["Key"] for obj in resp.get("Contents", []) if obj["Key"].endswith(".json")
    )
    history: list[dict] = []
    for key in keys:
        data = _fetch_s3_json(bucket, key)
        if isinstance(data, dict):
            history.append(data)
    return history


@st.cache_data(ttl=_ttl("research"))
def load_optimizer_risk_history() -> list[dict]:
    """Return optimizer_risk_history records sorted oldest → newest.

    Reads `config/optimizer_risk_history/{run_id}.json` — written by the
    backtester (`alpha-engine-backtester/optimizer/optimizer_risk_history.py`)
    on each run that produces a covariance-estimator sweep. Each record is a
    flat snapshot of the portfolio optimizer's risk-tolerance levers
    (risk_aversion, tcost_bps, covariance_shrinkage, sigma_horizon_days,
    alpha_uncertainty_penalty, turnover/sector caps, …) plus the selected
    cell's risk metrics (sortino / psr / cvar_95 / max_drawdown /
    tracking_error_ann / mean_active_share / turnover_one_way_ann / …).

    The `latest.json` sidecar is skipped (it duplicates the newest dated
    record). Excludes the empty/absent case gracefully (returns []).
    """
    bucket = _research_bucket()
    prefix = f"{_CONFIG_PREFIX}/optimizer_risk_history/"
    client = get_s3_client()
    try:
        resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    except Exception as e:
        logger.warning("list optimizer_risk_history failed: %s", e)
        _record_s3_error(bucket, prefix, type(e).__name__, str(e))
        return []

    keys = sorted(
        obj["Key"]
        for obj in resp.get("Contents", [])
        if obj["Key"].endswith(".json") and not obj["Key"].endswith("/latest.json")
    )
    history: list[dict] = []
    for key in keys:
        data = _fetch_s3_json(bucket, key)
        if isinstance(data, dict):
            history.append(data)
    return history


@st.cache_data(ttl=_ttl("research"))
def load_rag_manifest() -> dict | None:
    """Load `rag/manifest/latest.json` — RAG corpus inventory snapshot.

    Producer: `alpha-engine-data` `rag/pipelines/emit_manifest.py`,
    runs as step 6/6 of `run_weekly_ingestion.sh` (Saturday SF). Carries:

      - `totals`: documents · chunks · tickers
      - `by_source`: per `doc_type` rollup (10-K · 10-Q · 8-K · earnings
        · thesis): document count · ticker count · chunk count
      - `by_ticker_coverage`: tickers_with_any_doc + p25/p50/p75
        docs/ticker
      - `embedding`: model name + dimension
      - `ingestion`: overall + per-source `last_run_ts`

    Returns None until the first weekly run produces a manifest (next
    Saturday SF, or via manual `python -m rag.pipelines.emit_manifest
    --output-s3` invocation).
    """
    return _fetch_s3_json(_research_bucket(), "rag/manifest/latest.json")


@st.cache_data(ttl=_ttl("trades"))
def load_daily_data_health() -> dict | None:
    """Load `health/daily_data.json` — runtime ingestion attribution.

    Producer: `alpha-engine-data` daily_data run (Saturday Data Phase 1
    + weekday Morning Enrich). Carries per-source row counts from the
    most recent successful write — `summary.polygon`, `.yfinance`,
    `.fred`, `.tickers_captured`. The yfinance EOD pass writes first
    (~1:05 PT same-day); the polygon morning pass overwrites the close
    (~5:30 AM PT next trading day, with VWAP added). This file reflects
    the LAST write — so the polygon/yfinance ratio depends on which
    pass ran most recently.
    """
    return _fetch_s3_json(_research_bucket(), "health/daily_data.json")


@st.cache_data(ttl=_ttl("research"))
def load_regime_substrate_latest() -> dict | None:
    """Load the most recent regime substrate artifact.

    Producer: ``alpha-engine-predictor-regime-substrate`` Lambda, runs
    weekly in the Saturday SF ``RegimeSubstrate`` state (between
    RAGIngestion and Research). Carries HMM posteriors + composite
    intensity_z + BOCPD change_signal + guardrail flags + raw macro
    features + model_metadata.

    Delegates to ``alpha_engine_lib.eval_artifacts.load_latest_eval_artifact``
    for canonical sidecar→artifact resolution (the lib's helper
    returns None on any failure mode — missing sidecar, malformed
    pointer, missing artifact body). When None, the dashboard's
    Regime page renders a graceful "no substrate yet" warning.
    """
    from alpha_engine_lib.eval_artifacts import load_latest_eval_artifact

    return load_latest_eval_artifact(
        get_s3_client(), bucket=_research_bucket(), prefix="regime",
    )


@st.cache_data(ttl=_ttl("research"))
def load_fast_signal_latest() -> dict | None:
    """Load the most recent daily fast-signal artifact (Stage F2).

    Producer: alpha-engine-predictor's ``regime_fast_signal`` inference
    stage (daily, regime-fast-signal-260515.md) — the online BOCPD
    circuit-breaker. Carries ``forced_bear`` + ``change_confidence`` +
    ``intensity_z`` + ``warmup`` + ``consecutive_change_days``.
    Resolves ``regime/fast_signal/latest.json`` via the canonical
    sidecar helper (None on any failure → page renders "no fast signal
    yet"). Distinct cadence from the weekly substrate above.
    """
    from alpha_engine_lib.eval_artifacts import load_latest_eval_artifact

    return load_latest_eval_artifact(
        get_s3_client(), bucket=_research_bucket(), prefix="regime/fast_signal",
    )


@st.cache_data(ttl=_ttl("research"))
def load_drawdown_leg_latest() -> dict | None:
    """Load the most recent daily drawdown-leg artifact (3rd ensemble leg).

    Producer: alpha-engine-predictor's ``regime_fast_signal`` inference
    stage ``_advance_drawdown`` (daily, regime-drawdown-hysteresis-
    260518.md) — the deterministic pure-level hysteresis de-risk leg.
    Carries ``spy`` + ``excess`` sub-legs + the composed
    ``effective_regime`` (most-protective over the ensemble) +
    ``observed``/``cold_start``. Resolves ``regime/drawdown/latest.json``
    via the canonical sidecar helper (None on any failure → page
    renders "no drawdown leg yet"). Observe-mode only — no consumer
    acts on it until ``drawdown_regime_enabled`` is flipped.
    """
    from alpha_engine_lib.eval_artifacts import load_latest_eval_artifact

    return load_latest_eval_artifact(
        get_s3_client(), bucket=_research_bucket(), prefix="regime/drawdown",
    )


@st.cache_data(ttl=_ttl("research"))
def load_drawdown_leg_history(n_days: int = 14) -> list[dict]:
    """List recent daily drawdown-leg artifacts, oldest → newest.

    Used by the regime page's drawdown observe panel to render the
    2-week parallel-observe counterfactual history. Delegates to
    ``alpha_engine_lib.eval_artifacts.list_eval_artifacts`` (canonical
    YYMMDDHHMM sort + n_recent cap + skip-non-conforming + partial
    progress on body-fetch failure).
    """
    from alpha_engine_lib.eval_artifacts import list_eval_artifacts

    return list_eval_artifacts(
        get_s3_client(),
        bucket=_research_bucket(),
        prefix="regime/drawdown",
        n_recent=n_days,
    )


@st.cache_data(ttl=_ttl("research"))
def load_regime_substrate_history(n_weeks: int = 26) -> list[dict]:
    """List recent regime substrate artifacts, oldest → newest.

    Used by the regime page to render HMM-probability + composite-
    intensity trends over the observation window.

    Delegates to ``alpha_engine_lib.eval_artifacts.list_eval_artifacts``
    for canonical YYMMDDHHMM chronological sort + n_recent capping +
    skip-non-conforming-keys filtering + partial-progress on body
    fetch failures.
    """
    from alpha_engine_lib.eval_artifacts import list_eval_artifacts

    return list_eval_artifacts(
        get_s3_client(),
        bucket=_research_bucket(),
        prefix="regime",
        n_recent=n_weeks,
    )


@st.cache_data(ttl=_ttl("research"))
def load_regime_retrospective_eval_latest() -> dict | None:
    """Load the most recent T1 retrospective HMM smoothing eval artifact.

    Producer: ``alpha-engine-predictor-regime-retrospective-eval``
    Lambda, runs weekly in the Saturday SF ``RegimeRetrospectiveEval``
    state. Pairs each historical macro-agent regime call with the
    HMM smoother's retrospective label (8-week lag) and scores with
    an asymmetric loss (bear-miss weighted 2× per regime-v3-260514.md
    §5.3.3).

    Returns the assembled payload dict or ``None`` if the artifact is
    unavailable (cold-start window — the Lambda is created but hasn't
    written yet, OR fewer than lag_weeks of agent calls in the
    signals/ archive). The dashboard's Regime page renders a graceful
    "no T1 eval yet" warning under that path.
    """
    from alpha_engine_lib.eval_artifacts import load_latest_eval_artifact

    return load_latest_eval_artifact(
        get_s3_client(),
        bucket=_research_bucket(),
        prefix="regime/retrospective",
    )


@st.cache_data(ttl=_ttl("research"))
def load_regime_stratified_sortino_latest() -> dict | None:
    """Load the most recent T2 downstream-stratified Sortino eval artifact.

    Producer: ``alpha-engine-backtester`` spot EC2, runs weekly during
    the Saturday SF ``Backtester`` state via the new
    ``regime_stratified_sortino_runner`` (wired into ``evaluate.py``).
    Groups ``score_performance`` picks by ``market_regime`` and
    computes Sortino + Sharpe + log-alpha + hit-rate per (regime,
    horizon) stratum; surfaces the bull-bear Sortino spread as the
    headline T2 metric.

    Returns the assembled payload or ``None`` if the artifact is
    unavailable. The Regime page handles None gracefully.
    """
    from alpha_engine_lib.eval_artifacts import load_latest_eval_artifact

    return load_latest_eval_artifact(
        get_s3_client(),
        bucket=_research_bucket(),
        prefix="regime/stratified_sortino",
    )


@st.cache_data(ttl=_ttl("research"))
def load_regime_retrospective_eval_history(n_weeks: int = 26) -> list[dict]:
    """List recent T1 retrospective eval artifacts, oldest → newest.

    Used by the dashboard to render the rolling
    ``asymmetric_weighted_agreement_rate`` timeseries.
    """
    from alpha_engine_lib.eval_artifacts import list_eval_artifacts

    return list_eval_artifacts(
        get_s3_client(),
        bucket=_research_bucket(),
        prefix="regime/retrospective",
        n_recent=n_weeks,
    )


@st.cache_data(ttl=_ttl("research"))
def load_regime_stratified_sortino_history(n_weeks: int = 26) -> list[dict]:
    """List recent T2 stratified-Sortino eval artifacts, oldest → newest.

    Used by the dashboard to render the rolling bull-bear Sortino spread
    timeseries — the headline T2 metric per regime-v3-260514.md §5.3.3.
    """
    from alpha_engine_lib.eval_artifacts import list_eval_artifacts

    return list_eval_artifacts(
        get_s3_client(),
        bucket=_research_bucket(),
        prefix="regime/stratified_sortino",
        n_recent=n_weeks,
    )


@st.cache_data(ttl=_ttl("research"))
def load_research_params() -> dict | None:
    """Load `config/research_params.json` (CIO mode flag + reason).

    Backtester writeback: when CIO ranking lift drops below baseline, the
    weight optimizer flips `cio_mode` to `deterministic`; when it
    recovers, it flips back to `rubric`. Captures one channel of the
    autonomous feedback loop separate from executor parameters.
    """
    return _fetch_s3_json(_research_bucket(), f"{_CONFIG_PREFIX}/research_params.json")


def list_backtest_dates() -> list[str]:
    """Return sorted list of available backtest dates (descending)."""
    cfg = load_config()
    prefix = cfg["paths"]["backtest_prefix"]
    dates = list_s3_prefixes(_research_bucket(), prefix)
    return sorted(dates, reverse=True)


def load_backtest_file(date_str: str, filename: str) -> dict | list | pd.DataFrame | str | None:
    """Load a file from backtest/{date}/{filename} in the research bucket.

    Supports .json, .csv, .md extensions.
    """
    cfg = load_config()
    prefix = cfg["paths"]["backtest_prefix"]
    key = f"{prefix}{date_str}/{filename}"
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".json":
        return download_s3_json(_research_bucket(), key)
    elif ext == ".csv":
        return download_s3_csv(_research_bucket(), key)
    elif ext in (".md", ".txt"):
        return download_s3_text(_research_bucket(), key)
    else:
        return download_s3_text(_research_bucket(), key)


@st.cache_data(ttl=_ttl("signals"), show_spinner=False)
def load_predictions_json(date_str: str | None = None) -> dict:
    """Load predictor predictions from S3. Returns {} on any failure."""
    key = _predictions_key(date_str)
    data = _fetch_s3_json(_research_bucket(), key)
    if not isinstance(data, dict):
        return {}
    pred_list = data.get("predictions", [])
    return {p["ticker"]: p for p in pred_list if "ticker" in p}


def load_predictor_metrics() -> dict:
    """Load predictor metrics from S3. Returns {} on any failure."""
    data = _fetch_s3_json(_research_bucket(), f"{_PREDICTOR_METRICS_PREFIX}/latest.json")
    return data if isinstance(data, dict) else {}


_MODEL_ZOO_LEADERBOARD_PREFIX = "predictor/model_zoo/leaderboard/"


@st.cache_data(ttl=_ttl("research"), show_spinner=False)
def load_model_zoo_leaderboard(date_str: str | None = None) -> dict:
    """Load the weekly model-zoo selection leaderboard (L4544/L4571) from S3.

    Schema: ``{date, mode: "observe"|"cutover", champion: {forward_days,
    cpcv_mean_ic}, margin, candidates: [{spec_id, model_version, forward_days,
    cpcv_mean_ic, passes_gate, eligible, reason}], winner_version_id, promoted}``.
    ``date_str=None`` → ``latest.json``. Returns {} on any failure (none exists
    until the first Saturday rotation writes one).
    """
    key = f"{_MODEL_ZOO_LEADERBOARD_PREFIX}{date_str or 'latest'}.json"
    data = _fetch_s3_json(_research_bucket(), key)
    return data if isinstance(data, dict) else {}


@st.cache_data(ttl=_ttl("research"), show_spinner=False)
def list_model_zoo_leaderboard_dates() -> list[str]:
    """Sorted ISO dates that have a model-zoo leaderboard (for the history picker)."""
    return list_s3_prefixes(_research_bucket(), _MODEL_ZOO_LEADERBOARD_PREFIX)


def load_hold_book_flag() -> dict:
    """Load the executor hold-book flag (`executor/hold_book_flags/latest.json`).

    Written by the morning planner when the predictor output_distribution_gate
    flagged the batch "strongly biased" and the optimizer rebalance was
    suppressed (current book held). Returns {} when absent — the safeguard has
    not fired (the common case). The reader compares ``run_date`` /
    ``predictions_date`` to decide whether the flag is for the current cycle.
    """
    data = _fetch_s3_json(_research_bucket(), "executor/hold_book_flags/latest.json")
    return data if isinstance(data, dict) else {}


def load_production_health() -> dict:
    """Load the backtester-written predictor production-health metrics
    (`predictor/metrics/production_health.json`) — rolling 30d IC, hit
    rate, per-L1 + L2 IC decomposition (ROADMAP L135), regime IC, mode
    collapse flags. Returns {} on any failure.
    """
    data = _fetch_s3_json(_research_bucket(), f"{_PREDICTOR_METRICS_PREFIX}/production_health.json")
    return data if isinstance(data, dict) else {}


def load_predictor_manifest() -> dict:
    """Load predictor weights manifest from S3 (`predictor/weights/meta/manifest.json`).

    Source of truth for the predictor's training-time horizon + label
    domain. Per the predictor-21d-migration plan: dashboard display
    strings should read `forward_days` + `label_domain` from this
    manifest rather than hardcoding "5d" / "21d" / "arithmetic" /
    "log-domain" throughout the page code. Falls back to {} on any
    failure; callers should provide a sensible default.
    """
    data = _fetch_s3_json(_research_bucket(), "predictor/weights/meta/manifest.json")
    return data if isinstance(data, dict) else {}


def load_predictor_training_state() -> dict:
    """Authoritative predictor TRAINING state from the manifest (SSOT — L4468).

    The manifest (`predictor/weights/meta/manifest.json`) is written by EVERY
    Saturday training run, so it is always fresh. `latest.json`'s training-mirror
    fields (promoted / last_trained / meta IC) are only refreshed by the WEEKDAY
    inference path, so they lag all weekend (no weekend inference) — the cause
    of the 2026-05-30 false "skill drought" read, where latest.json still showed
    the Friday pre-training snapshot while the manifest was correct. Read
    training state from HERE, never from latest.json. Returns normalized keys
    (incl. the W1/L4469 leak-free OOS metrics); {} on failure.
    """
    m = load_predictor_manifest()
    if not isinstance(m, dict) or not m:
        return {}
    models = m.get("models") if isinstance(m.get("models"), dict) else {}
    wf = m.get("walk_forward") if isinstance(m.get("walk_forward"), dict) else {}
    meta = models.get("meta_model") if isinstance(models.get("meta_model"), dict) else {}
    mom = models.get("momentum") if isinstance(models.get("momentum"), dict) else {}
    vol = models.get("volatility") if isinstance(models.get("volatility"), dict) else {}
    return {
        "last_trained": m.get("date"),
        "promoted": m.get("promoted"),
        "version": m.get("version"),
        # In-sample meta IC (the legacy headline; W1.0 showed it is inflated).
        "meta_ic_in_sample": meta.get("ic"),
        "momentum_test_ic": mom.get("test_ic"),
        "volatility_test_ic": vol.get("test_ic"),
        "momentum_median_ic": wf.get("momentum_median_ic"),
        "volatility_median_ic": wf.get("volatility_median_ic"),
        # W1 (L4469, observe) leak-free honest metrics — the trustworthy lens.
        "oos_ic_leakfree": m.get("meta_model_oos_ic_leakfree"),
        "oos_ic_cpcv": m.get("meta_model_oos_ic_cpcv"),
        "promotion_stats": m.get("meta_model_promotion_stats"),
    }


def predictor_horizon_days(default: int = 21) -> int:
    """Convenience: read the predictor's current training horizon from
    the manifest. Default reflects the active production state post
    Track A cutover (2026-05-09); kept as a fallback for early-cutover
    cycles where the manifest hasn't yet emitted `forward_days` (PRs
    ≤ #114, written before alpha-engine-predictor #115 added the field).
    """
    manifest = load_predictor_manifest()
    h = manifest.get("forward_days")
    try:
        return int(h) if h is not None else default
    except (TypeError, ValueError):
        return default


def predictor_label_domain(default: str = "canonical_log") -> str:
    """Convenience: read the predictor's current label domain
    (canonical_log vs arithmetic_legacy) from the manifest. Same
    default-fallback rationale as predictor_horizon_days.
    """
    manifest = load_predictor_manifest()
    d = manifest.get("label_domain")
    return str(d) if isinstance(d, str) else default


def load_mode_history() -> list[dict]:
    """Load predictor mode selection history from S3. Returns [] on failure."""
    data = _fetch_s3_json(_research_bucket(), f"{_PREDICTOR_METRICS_PREFIX}/mode_history.json")
    return data if isinstance(data, list) else []


def load_predictor_params() -> dict:
    """Load predictor_params.json from S3 config. Returns {} on any failure."""
    data = _fetch_s3_json(_research_bucket(), f"{_CONFIG_PREFIX}/predictor_params.json")
    return data if isinstance(data, dict) else {}


def load_feature_importance() -> dict:
    """Load latest feature importance (SHAP + gain + IC) from S3. Returns {} on failure."""
    data = _fetch_s3_json(_research_bucket(), f"{_PREDICTOR_METRICS_PREFIX}/feature_importance_latest.json")
    return data if isinstance(data, dict) else {}


@st.cache_data(ttl=_ttl("research"))
def load_population_json() -> dict | None:
    """Load population/latest.json from the research bucket.

    Returns the full dict with 'population', 'date', 'market_regime', etc.
    Returns None if the file does not exist.
    """
    return download_s3_json(_research_bucket(), _POPULATION_KEY)


@st.cache_data(ttl=_ttl("signals"))
def load_order_book_summary(date_str: str) -> dict | None:
    """Load order_book_summary.json for a given date from the research bucket.

    Returns None if the file does not exist (backward compatible).
    """
    return download_s3_json(_research_bucket(), _order_book_key(date_str))


@st.cache_data(ttl=_ttl("signals"))
def load_intraday_heartbeat() -> dict | None:
    """Daemon liveness/surveillance heartbeat (intraday/heartbeat.json).

    Daemon-published snapshot the intraday alerts Lambda consumes. The
    alerts process itself persists NO per-run artifact (Telegram-only
    surveillance) — this is the closest persisted surveillance state.
    """
    return _fetch_s3_json(_research_bucket(), "intraday/heartbeat.json")


@st.cache_data(ttl=_ttl("signals"))
def load_intraday_latest_prices() -> dict | None:
    """Daemon-published latest IB snapshot prices (intraday/latest_prices.json)."""
    return _fetch_s3_json(_research_bucket(), "intraday/latest_prices.json")


@st.cache_data(ttl=_ttl("signals"))
def load_open_orders_latest() -> dict | None:
    """Daemon-published open-IB-orders snapshot (trades/open_orders/latest.json).

    Producer: ``executor/open_orders_artifact.py::OpenOrdersSnapshotWriter``
    invoked each daemon tick. Consumed by the order-book rationale
    reconciliation view to render the "Working $" column alongside the
    optimizer's "Planned $".
    """
    return _fetch_s3_json(_research_bucket(), "trades/open_orders/latest.json")


@st.cache_data(ttl=_ttl("signals"))
def list_dated_artifact_keys(
    prefix: str,
    *,
    basename: str | None = None,
    suffix: str | None = None,
    n_recent: int = 14,
) -> list[tuple[str, str]]:
    """List dated S3 keys under *prefix*, newest → oldest.

    Generic lister behind the per-process artifact-archive pages
    (ROADMAP Observability Item 5). Handles both layout families:

    - ``{prefix}/{YYYY-MM-DD}/{basename}`` (research ``consolidated/``,
      backtester ``backtest/``)
    - ``{prefix}/...{YYYY-MM-DD}.json`` (predictor ``predictions/``,
      ``metrics/training_summary_*``)

    A key qualifies only if it contains a ``YYYY-MM-DD`` token AND
    (when given) ends with ``basename`` / ``suffix``. The date-token
    requirement naturally excludes the non-dated ``latest.json`` /
    ``training_summary_latest.json`` sidecars.

    Returns ``[(date_str, key), ...]`` capped to the ``n_recent`` most
    recent (one artifact/day cadence assumed; weekly producers yield
    fewer). Empty list on any failure / pre-deploy — pages render the
    graceful empty notice. Single research bucket.
    """
    bucket = _research_bucket()
    try:
        client = get_s3_client()
        paginator = client.get_paginator("list_objects_v2")
        found: dict[str, str] = {}  # date_str → key (last wins)
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if basename is not None and not key.endswith(basename):
                    continue
                if suffix is not None and not key.endswith(suffix):
                    continue
                m = ISO_DATE_PATTERN.search(key)
                if not m:
                    continue
                found[m.group(0)] = key
        ordered = sorted(found.items(), key=lambda kv: kv[0], reverse=True)
        return ordered[:n_recent]
    except Exception as e:
        logger.error(
            "Failed to list dated artifacts %s/%s: %s", bucket, prefix, e
        )
        _record_s3_error(bucket, prefix, type(e).__name__, str(e))
        return []


@st.cache_data(ttl=_ttl("signals"))
def load_order_book_rationale_history(n_recent: int = 14) -> list[dict]:
    """List recent per-ticker order-book rationale artifacts, oldest → newest.

    Producer: alpha-engine executor ``order_book_rationale`` write at
    morning-planner finalize (alpha-engine #189). Each artifact answers
    "why is ticker X in state S today" for the whole considered
    universe (approved entry / urgent exit / reduce / held /
    risk-blocked / predictor-vetoed) in canonical
    ``alpha_engine_lib.eval_artifacts`` shape.

    Delegates to ``list_eval_artifacts`` for canonical YYMMDDHHMM
    chronological sort + n_recent capping + partial-progress on body
    fetch failures. Empty list pre-deploy (until the executor next runs
    post-merge) — the page renders a graceful "no artifacts yet" notice.
    """
    from alpha_engine_lib.eval_artifacts import list_eval_artifacts

    return list_eval_artifacts(
        get_s3_client(),
        bucket=_research_bucket(),
        prefix="trades/order_book_rationale",
        n_recent=n_recent,
    )


# Production run_id format in the cost-tracker is ISO date
# (``YYYY-MM-DD``, sometimes with a suffix). Test fixtures use
# strings like ``run-x`` / ``run-budget-test`` / ``run-1``. The
# anchor regex is the strong structural discriminator.
_COST_RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(\b|[-_])")

# Claude Opus 4.7 max context is 1M tokens. 5M is 5x that ceiling so
# no real API call can produce a per-row count above it. The 2026-05-13
# pollution had input_tokens=1e9 — 200x the ceiling.
_COST_MAX_PLAUSIBLE_TOKENS = 5_000_000


def _drop_implausible_cost_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Defensive consumer-side filter for test-pollution in cost parquets.

    Mirrors the producer-side guard in alpha-engine-research's
    ``scripts/aggregate_costs._is_plausible_cost_row``. Belt-and-suspenders
    so historical pollution (the 2026-05-13 ~$1014 spike from a unit-test
    run with real AWS creds) doesn't render on the LLM Cost page until
    the producer-side rewrite of that day's parquet lands.

    Drops rows where ``run_id`` doesn't start with an ISO date, or any
    token-count column exceeds the Claude API ceiling.
    """
    if df.empty or "run_id" not in df.columns:
        return df
    run_id_str = df["run_id"].astype(str).fillna("")
    ok_run_id = run_id_str.str.match(_COST_RUN_ID_RE)
    ok = ok_run_id.fillna(False)
    for col in ("input_tokens", "output_tokens",
                "cache_read_tokens", "cache_create_tokens"):
        if col in df.columns:
            ok = ok & (df[col].fillna(0) <= _COST_MAX_PLAUSIBLE_TOKENS)
    dropped = int((~ok).sum())
    if dropped:
        logger.warning(
            "load_llm_cost_parquets: dropped %d implausible row(s) "
            "(test-fixture pollution defense)", dropped,
        )
    return df[ok].copy()


@st.cache_data(ttl=_ttl("research"))
def load_llm_cost_parquets(n_recent: int = 12) -> pd.DataFrame:
    """Return a concatenated DataFrame of per-call LLM cost rows from the
    `decision_artifacts/_cost/{date}/cost.parquet` archive. Loads up to the
    *n_recent* most recent date partitions; empty DataFrame if the archive
    is empty or every parquet fails to parse.

    Applies a defensive implausibility filter (run_id ISO-date prefix +
    per-row token ceiling) so test-pollution rows like the 2026-05-13
    incident (~$1014 fake spend from a unit-test run with real AWS creds)
    don't reach the page renderer.
    """
    bucket = _research_bucket()
    dates = list_s3_prefixes(bucket, "decision_artifacts/_cost/")
    if not dates:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for d in dates[-n_recent:]:
        raw = _s3_get_object(bucket, f"decision_artifacts/_cost/{d}/cost.parquet")
        if raw is None:
            continue
        try:
            df = pd.read_parquet(io.BytesIO(raw))
            df["capture_date"] = d
            frames.append(df)
        except Exception as e:
            logger.warning("cost parquet parse failed for %s: %s", d, e)
            _record_s3_error(bucket, f"decision_artifacts/_cost/{d}/cost.parquet", "ParquetParseError", str(e))
    if not frames:
        return pd.DataFrame()
    return _drop_implausible_cost_rows(pd.concat(frames, ignore_index=True))


# ---------------------------------------------------------------------------
# Daily News — raw per-article feed (data/news_articles_daily/)
# ---------------------------------------------------------------------------

_NEWS_ARTICLES_PREFIX = "data/news_articles_daily/"


@st.cache_data(ttl=_ttl("signals"))
def list_news_article_runs(n_recent: int = 45) -> list[dict]:
    """List available daily raw-article runs, newest date first.

    Producer: ``alpha-engine-data`` weekday ``daily_news`` step, which
    writes ``data/news_articles_daily/{run_id}_articles.parquet`` (run_id =
    ``YYMMDDHHMM``) for the held + tracked universe. The run_id encodes the
    UTC run date, which matches the parquet's ``aggregate_date``.

    Returns ``[{"date": "YYYY-MM-DD", "run_id": str, "key": str}, ...]`` with
    ONE entry per date (the latest run that day), capped to ``n_recent``.
    Empty list on any failure / pre-deploy → the page renders a graceful
    empty notice.
    """
    bucket = _research_bucket()
    by_date: dict[str, tuple[str, str]] = {}  # date → (run_id, key); newest run wins
    try:
        client = get_s3_client()
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=_NEWS_ARTICLES_PREFIX):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if not key.endswith("_articles.parquet"):
                    continue
                run_id = key.rsplit("/", 1)[-1].replace("_articles.parquet", "")
                if len(run_id) != 10 or not run_id.isdigit():
                    continue
                date_str = f"20{run_id[0:2]}-{run_id[2:4]}-{run_id[4:6]}"
                prev = by_date.get(date_str)
                if prev is None or run_id > prev[0]:
                    by_date[date_str] = (run_id, key)
    except Exception as e:
        logger.error("Failed to list news article runs %s/%s: %s",
                     bucket, _NEWS_ARTICLES_PREFIX, e)
        _record_s3_error(bucket, _NEWS_ARTICLES_PREFIX, type(e).__name__, str(e))
        return []

    runs = [{"date": d, "run_id": v[0], "key": v[1]} for d, v in by_date.items()]
    runs.sort(key=lambda r: r["date"], reverse=True)
    return runs[:n_recent]


@st.cache_data(ttl=_ttl("signals"))
def load_news_articles(key: str) -> pd.DataFrame:
    """Load one daily raw-article parquet by S3 key. Returns an empty
    DataFrame on missing key / parse failure (page renders empty notice)."""
    raw = _s3_get_object(_research_bucket(), key)
    if raw is None:
        return pd.DataFrame()
    try:
        return pd.read_parquet(io.BytesIO(raw))
    except Exception as e:
        logger.warning("news articles parquet parse failed for %s: %s", key, e)
        _record_s3_error(_research_bucket(), key, "ParquetParseError", str(e))
        return pd.DataFrame()
