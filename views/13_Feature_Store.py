"""
Alpha Engine — Feature Store (private console)

Pre-computed feature snapshots for GBM inference: freshness, coverage,
catalog (per-family), distributions, drift detection, recent snapshots,
production-vs-research delta.

Closes Workstream 3.4 of the presentation revamp plan. Promoted from a
tab on /System_Health into its own sidebar page on 2026-05-05 for a
cleaner screenshare URL during interview demos.

Phase-2 framing: the catalog is the *substrate available* for Phase 3
alpha tuning. Production inference currently consumes a subset
(~21 features per `meta_model.py:META_FEATURES` + each L1 GBM's
feature list); expansion is gated on per-component IC discipline.

Lives on console.nousergon.ai (Cloudflare Access-gated). Sources from
existing system outputs only (Decision 11): `features/{date}/*.parquet`,
`features/registry.json`, `predictor/weights/meta/manifest.json`,
`predictor/metrics/training_feature_stats.json`,
`predictor/metrics/drift_{date}.json`,
`predictor/predictions/latest.json`.
"""

from __future__ import annotations

import io
import os
import sys
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.express as px
import streamlit as st

from loaders.s3_loader import (
    _fetch_s3_json,
    _research_bucket,
    _s3_get_object,
    get_s3_client,
    load_daily_data_health,
)
from loaders.utils import production_feature_set, research_feature_set



st.divider()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@st.cache_data(ttl=900)
def _load_parquet(bucket: str, key: str) -> pd.DataFrame | None:
    raw = _s3_get_object(bucket, key)
    if raw is None:
        return None
    try:
        return pd.read_parquet(io.BytesIO(raw))
    except Exception:
        return None


@st.cache_data(ttl=900)
def _find_latest_feature_date(bucket: str, max_lookback: int = 10) -> str | None:
    for offset in range(max_lookback):
        d = (date.today() - timedelta(days=offset)).isoformat()
        raw = _s3_get_object(bucket, f"features/{d}/technical.parquet")
        if raw is not None:
            return d
    return None


@st.cache_data(ttl=900)
def _load_drift_report(bucket: str, date_str: str) -> dict | None:
    return _fetch_s3_json(bucket, f"predictor/metrics/drift_{date_str}.json")


@st.cache_data(ttl=900)
def _load_registry(bucket: str) -> list[dict] | None:
    data = _fetch_s3_json(bucket, "features/registry.json")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "features" in data:
        return data["features"]
    return data


@st.cache_data(ttl=900)
def _load_training_feature_stats(bucket: str) -> dict | None:
    return _fetch_s3_json(bucket, "predictor/metrics/training_feature_stats.json")


@st.cache_data(ttl=900)
def _get_s3_last_modified(bucket: str, key: str) -> str | None:
    try:
        client = get_s3_client()
        resp = client.head_object(Bucket=bucket, Key=key)
        return resp["LastModified"].strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return None


@st.cache_data(ttl=900)
def _load_predictions_meta(bucket: str) -> dict | None:
    return _fetch_s3_json(bucket, "predictor/predictions/latest.json")


@st.cache_data(ttl=900)
def _load_feature_list_meta(bucket: str) -> dict | None:
    return _fetch_s3_json(bucket, "predictor/weights/meta/feature_list.json")


# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------

st.title("Feature Store")
st.caption(
    "Pre-computed feature snapshots for GBM inference — freshness, coverage, and drift monitoring. "
    "**Phase-2 framing:** the catalog below is the *substrate available* for Phase 3 alpha tuning. "
    "Production inference currently consumes a subset (~21 features per `meta_model.py:META_FEATURES` "
    "+ each L1 GBM's feature list); expansion is gated on per-component IC discipline."
)

bucket = _research_bucket()

# ─── Active model snapshot (manifest) ───────────────────────────────────────
st.subheader("Active inference model snapshot")
_meta_manifest = _fetch_s3_json(bucket, "predictor/weights/meta/manifest.json")
if _meta_manifest and isinstance(_meta_manifest, dict):
    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("Trained on", str(_meta_manifest.get("date", "?")))
    mc2.metric("Version", str(_meta_manifest.get("version", "?")))
    mc3.metric(
        "Promoted",
        "✓ yes" if _meta_manifest.get("promoted") else "✗ no",
        delta_color="off",
    )
    meta_ic = ((_meta_manifest.get("models") or {}).get("meta_model") or {}).get("ic")
    mc4.metric("L2 meta IC", f"{float(meta_ic):.3f}" if meta_ic is not None else "—")
    if _meta_manifest.get("note"):
        st.caption(_meta_manifest["note"])
else:
    st.caption("`predictor/weights/meta/manifest.json` not yet present — falls back to legacy weight selection.")

st.markdown("---")

with st.spinner("Finding latest feature snapshot..."):
    latest_date = _find_latest_feature_date(bucket)

if latest_date is None:
    st.error("No feature store snapshots found in the last 10 days.")
    st.stop()

schema = _fetch_s3_json(bucket, f"features/{latest_date}/schema_version.json")
tech_df = _load_parquet(bucket, f"features/{latest_date}/technical.parquet")
interaction_df = _load_parquet(bucket, f"features/{latest_date}/interaction.parquet")
macro_df = _load_parquet(bucket, f"features/{latest_date}/macro.parquet")
alt_df = _load_parquet(bucket, f"features/{latest_date}/alternative.parquet")
fundamental_df = _load_parquet(bucket, f"features/{latest_date}/fundamental.parquet")

# ─── Freshness ──────────────────────────────────────────────────────────────
st.subheader("Freshness")
age_days = (date.today() - date.fromisoformat(latest_date)).days

fc1, fc2, fc3, fc4 = st.columns(4)
fc1.metric("Latest Snapshot", latest_date)
fc2.metric(
    "Age", f"{age_days}d",
    delta=None if age_days <= 1 else f"{age_days}d old",
    delta_color="off" if age_days <= 1 else "inverse",
)
if schema:
    fc3.metric("Schema Version", schema.get("schema_version", "?"))
    fc4.metric("Schema Hash", (schema.get("schema_hash", "?")[:8] + "..."))
else:
    n_feat = len([c for c in (tech_df.columns if tech_df is not None else []) if c not in ("ticker", "date")])
    fc3.metric("Features", n_feat)
    fc4.metric("Schema", "not versioned yet")

if age_days > 2:
    st.warning(f"Feature store is {age_days} days old. Check that DailyData pipeline ran successfully.")
elif age_days == 0:
    st.success("Feature store is up to date (today).")

# ─── Latest ingestion attribution (runtime truth) ───────────────────────────
st.subheader("Latest ingestion attribution")
st.caption(
    "What actually got fetched on the most recent successful daily-data run "
    "— sourced from `s3://alpha-engine-research/health/daily_data.json`. "
    "Two passes write here: the **EOD yfinance** pass at ~1:05 PT same-day, "
    "then the **morning polygon** pass at ~5:30 AM PT next trading day "
    "(overwrites the close + adds VWAP). This snapshot reflects whichever "
    "pass ran last."
)

dd_health = load_daily_data_health() or {}
dd_summary = dd_health.get("summary") or {}
if dd_summary:
    ic1, ic2, ic3, ic4, ic5 = st.columns(5)
    ic1.metric("Tickers captured", f"{int(dd_summary.get('tickers_captured', 0)):,}")
    polygon_n = int(dd_summary.get("polygon", 0) or 0)
    yfinance_n = int(dd_summary.get("yfinance", 0) or 0)
    fred_n = int(dd_summary.get("fred", 0) or 0)
    ic2.metric("Polygon", f"{polygon_n:,}")
    ic3.metric("yfinance", f"{yfinance_n:,}")
    ic4.metric("FRED", f"{fred_n:,}")

    last_success = dd_health.get("last_success", "")
    duration = dd_health.get("duration_seconds")
    status = dd_health.get("status", "?")
    ic5.metric("Run status", status)

    if last_success:
        st.caption(
            f"Last successful write: **{last_success}** "
            f"{'(' + str(duration) + 's)' if duration else ''}"
        )

    # Determine which pass this snapshot reflects
    if polygon_n > yfinance_n:
        st.success(
            "**Polygon-dominant** — most recent write was the morning polygon "
            "overwrite (canonical close + VWAP)."
        )
    elif yfinance_n > polygon_n and yfinance_n > 0:
        st.info(
            "**yfinance-dominant** — most recent write was the EOD pass. "
            "Polygon morning overwrite expected ~5:30 AM PT next trading day."
        )

    warnings_list = dd_health.get("warnings") or []
    if warnings_list:
        with st.expander(f"Warnings ({len(warnings_list)})"):
            for w in warnings_list:
                st.warning(w)
else:
    st.caption("`health/daily_data.json` not yet present.")

# ─── Coverage ───────────────────────────────────────────────────────────────
st.subheader("Coverage")

_group_filenames = {
    "Technical": "technical.parquet",
    "Interaction": "interaction.parquet",
    "Macro": "macro.parquet",
    "Alternative": "alternative.parquet",
    "Fundamental": "fundamental.parquet",
}
groups = {
    "Technical": tech_df,
    "Interaction": interaction_df,
    "Macro": macro_df,
    "Alternative": alt_df,
    "Fundamental": fundamental_df,
}

coverage_data = []
for name, df in groups.items():
    last_modified = _get_s3_last_modified(bucket, f"features/{latest_date}/{_group_filenames[name]}")
    if df is not None and not df.empty:
        n_tickers = df["ticker"].nunique() if "ticker" in df.columns else 1
        n_features = len([c for c in df.columns if c not in ("ticker", "date")])
        n_nulls = int(df.select_dtypes(include="number").isna().sum().sum())
        coverage_data.append({
            "Group": name,
            "Tickers": n_tickers,
            "Features": n_features,
            "Last Updated": last_modified or "?",
            "Null Values": n_nulls,
            "Status": "OK" if n_nulls == 0 else f"{n_nulls} nulls",
        })
    else:
        coverage_data.append({
            "Group": name,
            "Tickers": 0,
            "Features": 0,
            "Last Updated": last_modified or "MISSING",
            "Null Values": 0,
            "Status": "MISSING",
        })

coverage_df = pd.DataFrame(coverage_data)
st.dataframe(coverage_df, use_container_width=True, hide_index=True)

total_tickers = tech_df["ticker"].nunique() if tech_df is not None and "ticker" in tech_df.columns else 0
total_features = schema.get("n_features", "?") if schema else "?"
st.caption(
    f"Total: {total_tickers} tickers, {total_features} features across "
    f"{len([g for g in groups.values() if g is not None])} groups"
)

# ─── Feature Catalog ────────────────────────────────────────────────────────
st.subheader("Feature Catalog")
st.caption(
    "Per-feature catalog. **Source column is resolved from runtime ingestion** — "
    "for technical features it flips polygon ↔ yfinance based on which "
    "daily-data pass ran most recently (see *Latest ingestion attribution* "
    "above). Macro / alternative / fundamental sources reflect feature-level "
    "domain knowledge of which raw input each feature consumes."
)

# Group-level canonical provider descriptions (stable; safe to show)
_group_provenance = {
    "Technical": "polygon canonical (morning T+1, with VWAP) + yfinance EOD fallback",
    "Interaction": "computed from technical + macro features at write time",
    "Macro": "FRED canonical (treasuries, VIX) + yfinance for index ETFs (GLD, USO, VIX3M)",
    "Alternative": "FMP for analyst/revisions/earnings; yfinance for options chains (OI, IV)",
    "Fundamental": "FMP quarterly financials",
}

# Runtime source resolution: technical features flip with the latest pass.
# Read the `source` column on the freshest daily_closes parquet (definitive
# label written by alpha-engine-data PR #159). For older parquets that
# pre-date the schema bump, fall back to VWAP-presence as a heuristic
# (polygon writes VWAP, yfinance doesn't) — once a few labeled passes
# accumulate, the fallback becomes the never-fires path.
def _detect_technical_source() -> str:
    for offset in range(5):
        d = (date.today() - timedelta(days=offset)).isoformat()
        for prefix in ("staging/daily_closes", "predictor/daily_closes"):
            df = _load_parquet(bucket, f"{prefix}/{d}.parquet")
            if df is None or df.empty:
                continue
            if "source" in df.columns:
                # Definitive label — take the dominant value for stock rows.
                src_counts = df["source"].dropna().value_counts()
                if not src_counts.empty:
                    return str(src_counts.idxmax())
            # Fallback for pre-schema parquets
            if "VWAP" in df.columns and df["VWAP"].notna().any():
                return "polygon"
            return "yfinance"
    return "yfinance"


_technical_runtime_source = _detect_technical_source()

# Macro features split by raw input (FRED canonical vs yfinance ETF momentum)
_macro_fred_features = {"vix_level", "yield_10y", "yield_curve_slope"}
_macro_yfinance_features = {"gold_mom_5d", "oil_mom_5d", "vix_term_slope"}
# Alternative split by raw input (FMP analyst/earnings vs yfinance options chains)
_alt_fmp_features = {"earnings_surprise_pct", "days_since_earnings", "eps_revision_4w", "revision_streak"}
_alt_yfinance_features = {"put_call_ratio", "iv_rank", "iv_vs_rv"}


def _resolve_runtime_source(group: str, feature: str) -> str:
    """Per-feature source, resolved from runtime ingestion + domain knowledge."""
    if group == "Technical":
        return _technical_runtime_source
    if group == "Interaction":
        return "computed"
    if group == "Macro":
        if feature in _macro_fred_features:
            return "FRED"
        if feature in _macro_yfinance_features:
            return "yfinance"
        if feature == "xsect_dispersion":
            return "computed"
        return "?"
    if group == "Alternative":
        if feature in _alt_fmp_features:
            return "FMP"
        if feature in _alt_yfinance_features:
            return "yfinance"
        return "?"
    if group == "Fundamental":
        return "FMP"
    return "?"


registry = _load_registry(bucket)
_registry_lookup: dict[str, dict] = {}
if registry:
    for entry in registry:
        name = entry.get("name", "")
        if name:
            _registry_lookup[name] = entry

_meta_cols = {"ticker", "date"}
catalog_rows = []
for group_name, df in groups.items():
    if df is not None and not df.empty:
        for col in df.columns:
            if col in _meta_cols:
                continue
            series = df[col]
            reg = _registry_lookup.get(col, {})
            catalog_rows.append({
                "Group": group_name,
                "Feature": col,
                "Description": reg.get("description", ""),
                "Source": _resolve_runtime_source(group_name, col),
                "Refresh": reg.get("refresh", ""),
                "Mean": round(float(series.mean()), 4) if pd.api.types.is_numeric_dtype(series) else None,
                "Std": round(float(series.std()), 4) if pd.api.types.is_numeric_dtype(series) else None,
                "Nulls": int(series.isna().sum()),
            })

if catalog_rows:
    catalog_df = pd.DataFrame(catalog_rows)
    st.caption(f"{len(catalog_rows)} features across {catalog_df['Group'].nunique()} groups")

    for group_name in ["Technical", "Interaction", "Macro", "Alternative", "Fundamental"]:
        group_slice = catalog_df[catalog_df["Group"] == group_name]
        if group_slice.empty:
            continue
        provenance = _group_provenance.get(group_name, "")
        with st.expander(f"{group_name} ({len(group_slice)} features)", expanded=False):
            if provenance:
                st.caption(f"**Provider:** {provenance}")
            display_cols = ["Feature", "Description", "Source", "Refresh", "Mean", "Std", "Nulls"]
            st.dataframe(
                group_slice[display_cols].reset_index(drop=True),
                use_container_width=True,
                hide_index=True,
            )

# ─── Feature Distributions ──────────────────────────────────────────────────
st.subheader("Feature Distributions")

if tech_df is not None and not tech_df.empty:
    numeric_cols = [c for c in tech_df.columns if c not in ("ticker", "date")]

    with st.expander("Summary Statistics (Technical Features)", expanded=False):
        stats = tech_df[numeric_cols].describe().T
        stats = stats[["mean", "std", "min", "25%", "50%", "75%", "max"]]
        st.dataframe(stats.round(4), use_container_width=True)

    selected = st.selectbox("Feature to visualize", numeric_cols, index=0)
    if selected:
        fig = px.histogram(
            tech_df, x=selected, nbins=50,
            title=f"Distribution of {selected} ({latest_date})",
            labels={selected: selected, "count": "Tickers"},
        )
        fig.update_layout(height=350, margin=dict(t=40, b=30))
        st.plotly_chart(fig, use_container_width=True)

    training_stats = _load_training_feature_stats(bucket)
    if training_stats and selected in training_stats.get("features", []):
        idx = training_stats["features"].index(selected)
        train_mean = training_stats["mean"][idx]
        train_std = training_stats["std"][idx]
        today_mean = float(tech_df[selected].mean())
        today_std = float(tech_df[selected].std())

        tc1, tc2, tc3, tc4 = st.columns(4)
        tc1.metric("Today Mean", f"{today_mean:.4f}")
        tc2.metric("Training Mean", f"{train_mean:.4f}")
        tc3.metric("Today Std", f"{today_std:.4f}")
        tc4.metric("Training Std", f"{train_std:.4f}")

        if train_std > 0:
            zscore = abs(today_mean - train_mean) / train_std
            if zscore > 3.0:
                st.warning(f"Feature drift detected: z-score = {zscore:.2f} (>{3.0} threshold)")
            else:
                st.caption(f"Z-score vs training: {zscore:.2f} (within normal range)")
else:
    st.info("No technical features available for the latest snapshot.")

# ─── Drift Detection ────────────────────────────────────────────────────────
st.subheader("Drift Detection")

drift = _load_drift_report(bucket, latest_date)
if drift:
    if drift.get("status") == "ok":
        st.success(f"No drift detected ({latest_date})")
    else:
        st.error(f"Drift alerts ({drift.get('n_alerts', 0)}):")
        for alert in drift.get("alerts", []):
            st.warning(alert)
else:
    st.info("No drift report available. Drift detection runs after inference — check back after the next daily pipeline.")

# ─── Production vs research delta ───────────────────────────────────────────
st.subheader("Production vs research delta")
st.caption(
    "Features that exist in the store but aren't yet wired into "
    "production inference — the *substrate-for-Phase-3* view per "
    "the presentation revamp plan §3.4."
)

_feature_list = _load_feature_list_meta(bucket)
if not _feature_list:
    st.info(
        "`predictor/weights/meta/feature_list.json` not yet present — "
        "the next predictor weekly-training run will emit it. The L2 production "
        "set is documented in code at `alpha-engine-predictor/model/meta_model.py::META_FEATURES`."
    )
else:
    _prod = production_feature_set(_feature_list)
    _research = research_feature_set(tech_df, interaction_df, macro_df, alt_df, fundamental_df)
    _delta = sorted(_research - _prod)
    _missing_in_store = sorted(_prod - _research)

    dc1, dc2, dc3, dc4 = st.columns(4)
    dc1.metric("Production features", len(_prod))
    dc2.metric("Store features", len(_research))
    dc3.metric("Substrate delta", len(_delta), help="Features in the store but not consumed by production inference")
    dc4.metric("Trained on", str(_feature_list.get("trained_at", "?")))

    if _missing_in_store:
        st.warning(
            f"{len(_missing_in_store)} production feature(s) absent from the latest store snapshot — "
            f"inference likely falls back to inline compute. Missing: `{', '.join(_missing_in_store)}`."
        )

    tab_delta, tab_breakdown = st.tabs(["Substrate delta", "Production breakdown"])
    with tab_delta:
        if _delta:
            st.dataframe(
                pd.DataFrame({"feature": _delta, "status": ["in-store, not-in-production"] * len(_delta)}),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                f"{len(_delta)} feature(s) available as substrate for Phase-3 alpha tuning. "
                "Promotion is gated on per-L1-component IC discipline."
            )
        else:
            st.success("No substrate delta — every feature in the store is consumed by production inference.")
    with tab_breakdown:
        rows: list[dict] = []
        for f in sorted(_feature_list.get("l2_features") or []):
            rows.append({"feature": f, "layer": "L2 meta", "in_store": f in _research})
        for component, feats in (_feature_list.get("l1_features") or {}).items():
            for f in sorted(feats or []):
                rows.append({"feature": f, "layer": f"L1 {component}", "in_store": f in _research})
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("`feature_list.json` present but empty.")
    st.caption(
        f"Source: `s3://{bucket}/predictor/weights/meta/feature_list.json` (trained_at={_feature_list.get('trained_at', '?')}, "
        f"version={_feature_list.get('version', '?')})."
    )

# ─── Store vs Inline Usage ──────────────────────────────────────────────────
st.subheader("Store vs Inline Usage")
st.caption(
    "Tracks whether GBM inference is reading from the feature store or falling back to inline computation. "
    "Goal: 100% from store, 0% inline."
)

preds = _load_predictions_meta(bucket)
if preds:
    pred_date = preds.get("date", "?")
    n_preds = preds.get("n_predictions", 0)
    st.metric("Latest Predictions", f"{n_preds} tickers on {pred_date}")
    st.info(
        "Store vs inline metrics are logged in the predictor inference logs. "
        "Look for: `GBM features: N from store, M from inline, K skipped` in CloudWatch."
    )
else:
    st.info("No predictions metadata available.")

# ─── Recent Snapshots ───────────────────────────────────────────────────────
st.subheader("Recent Snapshots")

snapshot_dates = []
for offset in range(14):
    d = (date.today() - timedelta(days=offset)).isoformat()
    raw = _s3_get_object(bucket, f"features/{d}/technical.parquet")
    if raw is not None:
        snapshot_dates.append(d)

if snapshot_dates:
    st.dataframe(
        pd.DataFrame({
            "Date": snapshot_dates,
            "Age (days)": [(date.today() - date.fromisoformat(d)).days for d in snapshot_dates],
        }),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(f"{len(snapshot_dates)} snapshots found in the last 14 days")
else:
    st.warning("No snapshots found in the last 14 days")

