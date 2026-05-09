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
