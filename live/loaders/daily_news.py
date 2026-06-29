"""Reader for the producer's ``news_aggregates_daily`` artifact (config#664).

The nousergon-data standalone runner (data#432) publishes a per-ticker daily
news-aggregates parquet under ``data/news_aggregates_daily/`` in the research
bucket, with a ``latest.json`` sidecar pointing at the most-recent run (see
nousergon-data ``collectors/daily_news.py::read_daily_news`` and
``data/derived/news_aggregates.py::read_news_aggregates_parquet`` — the
canonical write/read pair this mirrors).

This reader resolves that sidecar and returns the per-ticker rows as a plain
list of dicts (the morning-brief consumer doesn't need pandas here, and keeping
the loader pandas-free keeps the brief module light). It is fail-soft: a missing
sidecar / artifact (producer hasn't run yet, or an S3 hiccup) yields an empty
list, never an exception — the brief degrades to the macro-only lead.

Schema (canonical ``NewsTickerDailyAggregate`` columns the brief uses):
    ticker, aggregate_date, n_articles, n_articles_trusted_weighted,
    lm_sentiment_mean, lm_sentiment_trusted_mean, event_count,
    event_severity_max, event_categories, top_event_descriptions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import streamlit as st

from loaders.s3_loader import _research_bucket, _ttl, get_s3_client

logger = logging.getLogger(__name__)

# Must match nousergon-data ``collectors/daily_news.py::DAILY_PREFIX``.
DAILY_NEWS_PREFIX = "data/news_aggregates_daily"


def _latest_key(prefix: str) -> str:
    """``{prefix}/latest.json`` — mirror of ``nousergon_lib.eval_artifacts``.

    Imported lazily-by-reimplementation so this loader doesn't hard-depend on
    the lib's eval_artifacts symbol being importable in the Streamlit venv; the
    convention (``{prefix}/latest.json``) is stable and shared across repos.
    """
    return f"{prefix.strip('/')}/latest.json"


@st.cache_data(ttl=_ttl("research"))
def load_daily_news_rows(prefix: str = DAILY_NEWS_PREFIX) -> list[dict[str, Any]]:
    """Read the latest per-ticker daily news aggregates as a list of dicts.

    Resolves ``{prefix}/latest.json`` → its ``artifact_key`` → the parquet body
    in the research bucket. Returns ``[]`` on any failure (no sidecar yet,
    missing artifact, parse error) so the caller can fall back to a macro-only
    brief. Cached at the 1h research TTL — the producer publishes once per
    trading morning, so a longer TTL than the cadence's own throttle is fine.
    """
    bucket = _research_bucket()
    latest_key = _latest_key(prefix)
    try:
        client = get_s3_client()
        sidecar_obj = client.get_object(Bucket=bucket, Key=latest_key)
        sidecar = json.loads(sidecar_obj["Body"].read())
    except Exception as e:  # noqa: BLE001 — fail-soft empty state (pre-publish / S3 hiccup)
        code = _err_code(e)
        if code != "NoSuchKey":
            logger.warning(
                "[daily_news] sidecar read failed s3://%s/%s (%s) — empty brief news",
                bucket, latest_key, type(e).__name__,
            )
        return []

    artifact_key = sidecar.get("artifact_key")
    if not artifact_key:
        logger.warning("[daily_news] sidecar at %s lacks artifact_key", latest_key)
        return []

    try:
        import io

        import pandas as pd

        body = client.get_object(Bucket=bucket, Key=artifact_key)
        df = pd.read_parquet(io.BytesIO(body["Body"].read()), engine="pyarrow")
    except Exception as e:  # noqa: BLE001 — fail-soft empty state
        logger.warning(
            "[daily_news] parquet read failed s3://%s/%s (%s) — empty brief news",
            bucket, artifact_key, type(e).__name__,
        )
        return []

    # to_dict("records") gives plain JSON-ish dicts; NaN -> None for cleanliness.
    return df.where(df.notna(), None).to_dict("records")


def _err_code(e: Exception) -> str:
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        return resp.get("Error", {}).get("Code", "")
    return ""


def top_holdings_news(
    rows: list[dict[str, Any]],
    held_tickers: set[str] | None = None,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Rank per-ticker news rows for the brief's holdings section.

    Filters to ``held_tickers`` when provided (the brief leads with macro then
    HOLDINGS news), and ranks by a simple newsworthiness score: event severity
    first, then article volume, then |sentiment|. Returns at most ``limit``
    rows. Pure — easy to unit-test without S3.
    """
    if held_tickers is not None:
        held_upper = {t.upper() for t in held_tickers}
        rows = [r for r in rows if str(r.get("ticker", "")).upper() in held_upper]

    def _score(r: dict[str, Any]) -> tuple:
        sev = float(r.get("event_severity_max") or 0.0)
        n = int(r.get("n_articles") or 0)
        sent = abs(float(r.get("lm_sentiment_trusted_mean") or r.get("lm_sentiment_mean") or 0.0))
        return (sev, n, sent)

    ranked = sorted(rows, key=_score, reverse=True)
    # Drop rows with no news signal at all (no articles and no events).
    ranked = [
        r for r in ranked
        if (int(r.get("n_articles") or 0) > 0) or (int(r.get("event_count") or 0) > 0)
    ]
    return ranked[:limit]
