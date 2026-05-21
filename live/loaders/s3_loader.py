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


@st.cache_data(ttl=_ttl("research"))
def load_population_json() -> dict | None:
    """Load population/latest.json from the research bucket."""
    return download_s3_json(_research_bucket(), "population/latest.json")


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
    """
    import json

    client = get_s3_client()
    bucket = _trades_bucket()
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
