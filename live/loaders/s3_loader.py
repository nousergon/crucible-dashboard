"""
S3 data loading for the Nous Ergon public site.
Minimal subset — only loads eod_pnl.csv (portfolio performance data).
Credentials come from the EC2 IAM role (no explicit creds needed).
"""

import io
import logging
import os

import boto3
import pandas as pd
import streamlit as st
import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
)

_config_cache: dict | None = None


def load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        with open(_CONFIG_PATH) as f:
            _config_cache = yaml.safe_load(f)
    return _config_cache


def _ttl(key: str) -> int:
    return load_config()["cache_ttl"].get(key, 900)


def _trades_bucket() -> str:
    return load_config()["s3"]["trades_bucket"]


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------


def get_s3_client():
    # On Streamlit Cloud, credentials come from st.secrets["aws"]
    # On EC2, boto3 uses the IAM role automatically
    try:
        aws_secrets = st.secrets["aws"]
        return boto3.client(
            "s3",
            aws_access_key_id=aws_secrets["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=aws_secrets["AWS_SECRET_ACCESS_KEY"],
            region_name=aws_secrets.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    except (KeyError, FileNotFoundError):
        return boto3.client("s3")


@st.cache_data(ttl=_ttl("trades"))
def download_s3_csv(bucket: str, key: str) -> pd.DataFrame | None:
    try:
        client = get_s3_client()
        response = client.get_object(Bucket=bucket, Key=key)
        raw = response["Body"].read()
    except Exception as e:
        logger.error("Failed to download CSV %s/%s: %s", bucket, key, e)
        return None
    try:
        return pd.read_csv(io.BytesIO(raw))
    except Exception as e:
        logger.warning("CSV parse failed for %s/%s: %s", bucket, key, e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _research_bucket() -> str:
    return load_config()["s3"]["research_bucket"]


@st.cache_data(ttl=_ttl("research"))
def download_s3_json(bucket: str, key: str) -> dict | None:
    """Download and parse a JSON file from S3. Returns None on failure."""
    import json
    try:
        client = get_s3_client()
        response = client.get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read())
    except Exception as e:
        code = getattr(getattr(e, "response", {}), "get", lambda *a: {})("Error", {}).get("Code", "")
        if code != "NoSuchKey":
            logger.error("Failed to download JSON %s/%s: %s", bucket, key, e)
        return None


def load_eod_pnl() -> pd.DataFrame | None:
    """Load eod_pnl.csv from the executor bucket."""
    cfg = load_config()
    key = cfg["paths"]["eod_pnl"]
    return download_s3_csv(_trades_bucket(), key)


def load_trades_full() -> pd.DataFrame | None:
    """Load trades_full.csv from the executor bucket."""
    cfg = load_config()
    key = cfg["paths"]["trades_full"]
    return download_s3_csv(_trades_bucket(), key)


@st.cache_data(ttl=86400)
def load_company_names() -> dict[str, str]:
    """Return ``{TICKER: company_name}`` from SEC ``company_tickers.json``.

    Same institutional source the research RAG pipeline uses
    (`alpha-engine-research` ``run_news_pipeline._load_ticker_name_map``).
    Names are near-static so a 24h TTL is plenty. `requests` is always
    present (streamlit depends on it).

    Fail-soft: any fetch error returns ``{}`` and the caller renders the
    bare ticker — the failure is WARN-logged, never a silent swallow that
    masquerades as "no names exist" (feedback_no_silent_fails: the recording
    surface for this best-effort display lookup is the WARN log).
    """
    import requests

    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "AlphaEngine dashboard@nousergon.ai"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:  # best-effort display label — WARN + fall back to ticker
        logger.warning("load_company_names: SEC company_tickers fetch failed: %s", e)
        return {}

    out: dict[str, str] = {}
    try:
        for entry in resp.json().values():
            ticker = (entry.get("ticker") or "").upper()
            name = entry.get("title") or ""
            if ticker and name:
                out[ticker] = name
    except (ValueError, AttributeError) as e:
        logger.warning("load_company_names: malformed SEC payload: %s", e)
        return {}
    return out


@st.cache_data(ttl=900)
def load_live_day_return(ticker: str) -> float | None:
    """Today's % change (in percent points) for ``ticker`` from a 15-min
    delayed yfinance quote: ``(last_price / previous_close - 1) * 100``.

    The live site is otherwise EOD-sourced (`positions_snapshot`), whose
    `daily_return_pct` lags a full session until tonight's reconcile runs —
    so the per-ticker modal's "Day return" showed the LAST CLOSED session's
    return, not today's. This gives the modal today's number (yfinance's own
    `previous_close` is the prior-session close, so no snapshot-date
    reconciliation is needed). 15-min TTL matches the delayed feed.

    Fail-soft: missing yfinance, a bad symbol, or any missing field returns
    None and the caller falls back to the snapshot's stored
    `daily_return_pct` (feedback_no_silent_fails — the recording surface for
    this best-effort display lookup is the WARN log)."""
    if not ticker or ticker == "CASH":
        return None
    try:
        import yfinance as yf

        fi = yf.Ticker(ticker).fast_info
        last = getattr(fi, "last_price", None)
        prev = getattr(fi, "previous_close", None)
        if last and prev and prev > 0:
            return (last / prev - 1) * 100
        logger.warning("load_live_day_return(%s): missing last/prev (last=%s prev=%s)", ticker, last, prev)
    except Exception as e:  # best-effort live overlay — WARN + fall back to EOD snapshot
        logger.warning("load_live_day_return(%s) failed: %s", ticker, e)
    return None


@st.cache_data(ttl=_ttl("research"))
def load_population_json() -> dict | None:
    """Load population/latest.json from the research bucket."""
    return download_s3_json(_research_bucket(), "population/latest.json")


@st.cache_data(ttl=_ttl("research"))
def load_latest_signals() -> dict | None:
    """Return the newest `signals/{date}/signals.json` from the research bucket.

    Scans the `signals/` prefix for date directories, finds the most recent
    one with a `signals.json` blob, and returns the parsed dict. Returns
    None if nothing is found.
    """
    import json
    import re

    client = get_s3_client()
    bucket = _research_bucket()
    date_re = re.compile(r"^signals/(\d{4}-\d{2}-\d{2})/")

    date_keys: set[str] = set()
    continuation: str | None = None
    try:
        while True:
            kwargs = {"Bucket": bucket, "Prefix": "signals/", "Delimiter": "/"}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            resp = client.list_objects_v2(**kwargs)
            for cp in resp.get("CommonPrefixes", []) or []:
                m = date_re.match(cp["Prefix"])
                if m:
                    date_keys.add(m.group(1))
            if not resp.get("IsTruncated"):
                break
            continuation = resp.get("NextContinuationToken")
    except Exception as e:
        logger.warning("list signals/ failed: %s", e)
        return None

    for d in sorted(date_keys, reverse=True):
        key = f"signals/{d}/signals.json"
        try:
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            return json.loads(body)
        except Exception as e:
            code = getattr(getattr(e, "response", {}), "get", lambda *a: {})("Error", {}).get("Code", "")
            if code == "NoSuchKey":
                continue
            logger.warning("load %s failed: %s", key, e)
            continue
    return None


@st.cache_data(ttl=_ttl("research"))
def load_thesis_summaries(n_recent: int = 5) -> dict[str, str]:
    """Return {ticker: thesis_summary} aggregated across recent signals.json.

    Walks the last `n_recent` `signals/{date}/signals.json` files in reverse
    date order, taking the first non-empty `thesis_summary` per ticker. The
    walkback handles HOLD positions that weren't re-analyzed in the most
    recent cycle — their thesis still lives in an earlier signals.json.
    Empty dict if no signals.json is available.
    """
    import json
    import re

    client = get_s3_client()
    bucket = _research_bucket()
    date_re = re.compile(r"^signals/(\d{4}-\d{2}-\d{2})/")

    date_keys: set[str] = set()
    continuation: str | None = None
    try:
        while True:
            kwargs = {"Bucket": bucket, "Prefix": "signals/", "Delimiter": "/"}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            resp = client.list_objects_v2(**kwargs)
            for cp in resp.get("CommonPrefixes", []) or []:
                m = date_re.match(cp["Prefix"])
                if m:
                    date_keys.add(m.group(1))
            if not resp.get("IsTruncated"):
                break
            continuation = resp.get("NextContinuationToken")
    except Exception as e:
        logger.warning("list signals/ failed: %s", e)
        return {}

    out: dict[str, str] = {}
    for d in sorted(date_keys, reverse=True)[:n_recent]:
        key = f"signals/{d}/signals.json"
        try:
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            data = json.loads(body)
        except Exception as e:
            logger.warning("load %s failed: %s", key, e)
            continue
        universe = data.get("universe") or []
        for entry in universe:
            if not isinstance(entry, dict):
                continue
            ticker = entry.get("ticker")
            summary = entry.get("thesis_summary")
            if ticker and summary and ticker not in out:
                out[ticker] = summary
    return out


@st.cache_data(ttl=_ttl("trades"))
def load_predictions_json() -> dict | None:
    """Load predictor/predictions/latest.json. Returns dict keyed by ticker."""
    import json
    try:
        client = get_s3_client()
        response = client.get_object(Bucket=_research_bucket(), Key="predictor/predictions/latest.json")
        data = json.loads(response["Body"].read())
        pred_list = data.get("predictions", [])
        return {p["ticker"]: p for p in pred_list if "ticker" in p}
    except Exception:
        return None


@st.cache_data(ttl=_ttl("trades"))
def load_order_book_summary(date_str: str) -> dict | None:
    """Load order_book_summary.json for a given date."""
    return download_s3_json(_research_bucket(), f"order_books/{date_str}/summary.json")


@st.cache_data(ttl=_ttl("research"))
def load_universe_archive(ticker: str) -> dict | None:
    """Latest persisted rolling thesis for a ticker.

    Reads ``archive/universe/{TICKER}/thesis.json`` (the rolling per-ticker
    thesis the research arc overwrites each cycle — richer than the
    ``thesis_summary`` snippet on signals.json). Returns the parsed dict, or
    None if the ticker has no persisted archive yet (graceful per
    feedback_no_silent_fails — the modal renders a "no archive yet" fallback
    rather than an empty panel). TTL-cached (research TTL ~1 hr) — the
    archive/universe/ prefix is one of the larger per-ticker stores.
    """
    if not ticker:
        return None
    return download_s3_json(
        _research_bucket(), f"archive/universe/{ticker}/thesis.json"
    )


@st.cache_data(ttl=_ttl("trades"))
def load_order_book_rationale() -> dict | None:
    """Load the latest Order-Book Rationale (OBR) decision-chain artifact.

    ``trades/order_book_rationale/latest.json``. The per-ticker decision
    blocks live under ``considered`` (a list of records keyed by ``ticker``).
    Returns the full dict; the modal indexes ``considered`` for the ticker
    and renders the block only if present (OBR is an optional tie-in — the
    list is empty on days with no order-book activity)."""
    return download_s3_json(
        _research_bucket(), "trades/order_book_rationale/latest.json"
    )


@st.cache_data(ttl=_ttl("trades"))
def load_predictor_metrics() -> dict | None:
    """Load predictor/metrics/latest.json."""
    return download_s3_json(_research_bucket(), "predictor/metrics/latest.json")


@st.cache_data(ttl=_ttl("research"))
def load_latest_grading() -> dict | None:
    """Return the newest `backtest/{date}/grading.json` from the research bucket.

    Scans the `backtest/` prefix for date directories, finds the most recent
    one that actually contains a `grading.json`, and returns the parsed dict.
    Returns None if nothing is found.
    """
    import json
    import re

    client = get_s3_client()
    bucket = _research_bucket()
    date_re = re.compile(r"^backtest/(\d{4}-\d{2}-\d{2})/")

    date_keys: set[str] = set()
    continuation: str | None = None
    try:
        while True:
            kwargs = {"Bucket": bucket, "Prefix": "backtest/", "Delimiter": "/"}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            resp = client.list_objects_v2(**kwargs)
            for cp in resp.get("CommonPrefixes", []) or []:
                m = date_re.match(cp["Prefix"])
                if m:
                    date_keys.add(m.group(1))
            if not resp.get("IsTruncated"):
                break
            continuation = resp.get("NextContinuationToken")
    except Exception as e:
        logger.warning("list backtest/ failed: %s", e)
        return None

    for d in sorted(date_keys, reverse=True):
        key = f"backtest/{d}/grading.json"
        try:
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            data = json.loads(body)
            data["_run_date"] = d
            return data
        except Exception as e:
            code = getattr(getattr(e, "response", {}), "get", lambda *a: {})("Error", {}).get("Code", "")
            if code == "NoSuchKey":
                continue
            logger.warning("load %s failed: %s", key, e)
            continue
    return None


@st.cache_data(ttl=_ttl("research"))
def load_grade_history() -> list[dict]:
    """Return `backtest/grade_history.json` as a chronological list.

    Appended weekly by the evaluator. Empty list if the file is missing or
    malformed.
    """
    data = download_s3_json(_research_bucket(), "backtest/grade_history.json")
    if not isinstance(data, list):
        return []
    return data


@st.cache_data(ttl=_ttl("trades"))
def load_uptime_history(max_sessions: int = 20) -> list[dict]:
    """List recent uptime/*.json files and load the most recent `max_sessions`.

    Returns list sorted oldest → newest. Each dict matches the schema written
    by alpha-engine/executor/uptime_tracker.py.

    Bucket fix (L4570e, 2026-06-09): the tracker writes to the RESEARCH
    bucket (s3://alpha-engine-research/uptime/ — current through today);
    this loader read the executor bucket, whose uptime/ prefix is empty,
    so the public Uptime page rendered its empty state in production.
    """
    import json

    client = get_s3_client()
    bucket = _research_bucket()
    try:
        resp = client.list_objects_v2(Bucket=bucket, Prefix="uptime/")
    except Exception as e:
        logger.warning("list uptime/ failed: %s", e)
        return []

    keys = sorted(
        (obj["Key"] for obj in resp.get("Contents", []) if obj["Key"].endswith(".json")),
        reverse=True,
    )[:max_sessions]

    records: list[dict] = []
    for key in keys:
        try:
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            records.append(json.loads(body))
        except Exception as e:
            logger.warning("load %s failed: %s", key, e)
    # Non-trading-day sentinel records have only {"date","skipped"} — drop them.
    records = [r for r in records if "connected_minutes" in r]
    records.sort(key=lambda r: r.get("date", ""))
    return records
