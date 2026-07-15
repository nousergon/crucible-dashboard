"""
Data & Maturity — Alpha Engine (private console)

The surviving remainder of the former System Health page (console-IA phase 2a,
alpha-engine-config#1987): data-volume growth, feedback-loop maturity, and the
per-module data manifests. The page's freshness/observation KPI strips, module
health table, and missing-data alerts were retired — Fleet Status, Artifact
Freshness, and Active Observations own those axes now; Live Optimizer Params
moved to the Analysis page (Backtester tab).

Maturity honesty (config#1841): data-accrual thresholds here say an optimizer
COULD act — only `config/executor_params.json` has ever actually promoted to
live S3. The three dead write paths are named per row instead of implying an
"Active" loop that does not exist.
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.db_loader import load_research_db
from loaders.outcome_store import load_outcomes
from loaders.s3_loader import (
    _fetch_s3_json,
    _research_bucket,
    _trades_bucket,
    get_s3_client,
    list_s3_prefixes,
    load_eod_pnl,
    load_trades_full,
)


@st.cache_data(ttl=900)
def _load_manifests(bucket: str, module: str, max_days: int = 90) -> list[dict]:
    """Load recent data manifests for a module."""
    client = get_s3_client()
    prefix = f"data_manifest/{module}/"
    manifests = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        for key in sorted(keys)[-max_days:]:
            data = _fetch_s3_json(bucket, key)
            if data:
                manifests.append(data)
    except Exception:
        pass
    return manifests


@st.cache_data(ttl=900)
def _count_s3_objects(bucket: str, prefix: str) -> int:
    client = get_s3_client()
    count = 0
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            count += page.get("KeyCount", 0)
    except Exception:
        pass
    return count


@st.cache_data(ttl=900)
def _table_counts() -> dict[str, int]:
    conn = load_research_db()
    if conn is None:
        return {}
    tables = [
        "investment_thesis",
        "score_performance",
        "predictor_outcomes",
        "scanner_appearances",
        "macro_snapshots",
        "candidate_tenures",
        "population_history",
        "stock_archive",
        "thesis_history",
        "universe_returns",
        "scanner_evaluations",
        "team_candidates",
        "cio_evaluations",
        "executor_shadow_book",
    ]
    counts = {}
    for t in tables:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()  # noqa: S608
            counts[t] = row[0] if row else 0
        except Exception:
            counts[t] = 0
    return counts


st.title("Data & Maturity")
st.caption(
    "Corpus growth + feedback-loop data-accrual thresholds + per-module data "
    "manifests. Liveness lives on [Fleet Status](/fleet-status); per-artifact "
    "SLA detail on [Artifact Freshness](/host_observability?tab=Artifact+Freshness)."
)

# ─── Data Volume Growth ──────────────────────────────────────────────────
st.subheader("Data Volume Growth")

with st.spinner("Loading data counts..."):
    table_counts = _table_counts()
    trades_df = load_trades_full()
    eod_df = load_eod_pnl()
    n_signals_dates = len(list_s3_prefixes(_research_bucket(), "signals/"))
    n_predictions_dates = len(list_s3_prefixes(_research_bucket(), "predictor/predictions/"))
    # staging/ prefix per 2026-04-29 migration (alpha-engine-data PR #112)
    n_daily_closes = _count_s3_objects(_research_bucket(), "staging/daily_closes/")
    # Wave-4: predictor/price_cache_slim/ retired — ArcticDB universe lib is
    # canonical (its freshness is monitored upstream in alpha-engine-data's
    # preflight, which runs before consumers in every Step Function).

n_trades = len(trades_df) if trades_df is not None else 0
n_eod = len(eod_df) if eod_df is not None else 0

volume_data = {
    "Dataset": [
        "Signals (investment_thesis)",
        "Score Performance (21d)",
        "Predictor Outcomes",
        "Trades (executed)",
        "EOD P&L (days)",
        "Macro Snapshots",
        "Scanner Appearances",
        "Candidate Tenures",
        "Population History",
        "Signal Dates (S3)",
        "Prediction Dates (S3)",
        "Daily Closes (S3)",
        "Universe Returns (eval)",
        "Scanner Evaluations (eval)",
        "Team Candidates (eval)",
        "CIO Evaluations (eval)",
        "Executor Shadow Book (eval)",
    ],
    "Records": [
        table_counts.get("investment_thesis", "—"),
        table_counts.get("score_performance", "—"),
        table_counts.get("predictor_outcomes", "—"),
        n_trades,
        n_eod,
        table_counts.get("macro_snapshots", "—"),
        table_counts.get("scanner_appearances", "—"),
        table_counts.get("candidate_tenures", "—"),
        table_counts.get("population_history", "—"),
        n_signals_dates,
        n_predictions_dates,
        n_daily_closes,
        table_counts.get("universe_returns", "—"),
        table_counts.get("scanner_evaluations", "—"),
        table_counts.get("team_candidates", "—"),
        table_counts.get("cio_evaluations", "—"),
        table_counts.get("executor_shadow_book", "—"),
    ],
}

st.dataframe(pd.DataFrame(volume_data), use_container_width=True, hide_index=True)

if eod_df is not None and not eod_df.empty:
    eod_df.columns = [c.strip().lower().replace(" ", "_") for c in eod_df.columns]
    if "date" in eod_df.columns:
        eod_df["date"] = pd.to_datetime(eod_df["date"])
        eod_df = eod_df.sort_values("date")
        eod_df["trading_day_number"] = range(1, len(eod_df) + 1)
        fig = px.line(
            eod_df, x="date", y="trading_day_number",
            title="Cumulative Trading Days",
            labels={"trading_day_number": "Days", "date": "Date"},
        )
        fig.update_layout(height=300)
        st.plotly_chart(fig, use_container_width=True)

if trades_df is not None and not trades_df.empty:
    trades_df.columns = [c.strip().lower().replace(" ", "_") for c in trades_df.columns]
    if "date" in trades_df.columns:
        trades_by_date = trades_df.groupby("date").size().reset_index(name="count")
        trades_by_date["date"] = pd.to_datetime(trades_by_date["date"])
        trades_by_date = trades_by_date.sort_values("date")
        trades_by_date["cumulative"] = trades_by_date["count"].cumsum()
        fig2 = px.line(
            trades_by_date, x="date", y="cumulative",
            title="Cumulative Trade Records",
            labels={"cumulative": "Trades", "date": "Date"},
        )
        fig2.update_layout(height=300)
        st.plotly_chart(fig2, use_container_width=True)

st.divider()

# ─── Feedback Loop Maturity ──────────────────────────────────────────────
st.subheader("Feedback Loop Maturity")
st.caption(
    "Thresholds measure DATA accrual only. Per config#1841, the only "
    "auto-apply artifact that has ever promoted to live S3 is "
    "`config/executor_params.json` — rows whose write path is dead say so "
    "explicitly (a met threshold is 'Data ready', not an active loop). The "
    "per-run promoted/blocked record is the backtester's apply-audit "
    "artifact (`config/apply_audit/latest.json`, first emit 2026-07-11) — "
    "surfaced on the Analysis page's Self-Tuning tab."
)

n_score_perf = table_counts.get("score_performance", 0)
n_pred_outcomes = table_counts.get("predictor_outcomes", 0)

conn = load_research_db()
n_resolved_21d = 0
if conn:
    try:
        # Resolved-21d count comes from the long-format
        # score_performance_outcomes store, filtered to the canonical
        # primary horizon (EPIC config#1483 Phase 3, config#1531).
        outcomes_21d = load_outcomes(conn, horizons=(21,))
        n_resolved_21d = len(outcomes_21d)
    except Exception:
        pass

n_roundtrips = 0
if trades_df is not None and not trades_df.empty and "entry_trade_id" in trades_df.columns:
    n_roundtrips = int(trades_df["entry_trade_id"].notna().sum())

n_ur_weeks = n_se_weeks = n_tc_weeks = n_cio_weeks = 0
if conn:
    for tbl, attr in [
        ("universe_returns", "n_ur_weeks"),
        ("scanner_evaluations", "n_se_weeks"),
        ("team_candidates", "n_tc_weeks"),
        ("cio_evaluations", "n_cio_weeks"),
    ]:
        try:
            row = conn.execute(f"SELECT COUNT(DISTINCT eval_date) FROM {tbl}").fetchone()  # noqa: S608
            cnt = row[0] if row else 0
            if attr == "n_ur_weeks":
                n_ur_weeks = cnt
            elif attr == "n_se_weeks":
                n_se_weeks = cnt
            elif attr == "n_tc_weeks":
                n_tc_weeks = cnt
            elif attr == "n_cio_weeks":
                n_cio_weeks = cnt
        except Exception:
            pass

# Dead-write-path honesty (config#1841): threshold met → "Data ready", never
# "Active", for optimizers whose live artifact has never been written / is
# frozen. Executor-side loops (trigger optimizer etc.) ride the one live
# channel (executor_params.json) and keep plain accrual statuses.
_NEVER_PROMOTED = "Data ready — NEVER promoted (config#1841)"

maturity_data = [
    {
        "Optimizer": "Scoring weights",
        "Metric": "21d resolved signals",
        "Current": n_resolved_21d,
        "Threshold": 30,
        "Status": _NEVER_PROMOTED if n_resolved_21d >= 30 else "Blocked",
        "Live artifact": "config/scoring_weights.json — never written",
    },
    {
        "Optimizer": "Attribution analysis",
        "Metric": "21d resolved signals",
        "Current": n_resolved_21d,
        "Threshold": 50,
        "Status": "Active" if n_resolved_21d >= 50 else "Blocked",
        "Live artifact": "— (analysis only, no write path)",
    },
    {
        "Optimizer": "Predictor veto tuning",
        "Metric": "Resolved predictions",
        "Current": n_pred_outcomes,
        "Threshold": 20,
        "Status": _NEVER_PROMOTED if n_pred_outcomes >= 20 else "Blocked",
        "Live artifact": "config/predictor_params.json — never written",
    },
    {
        "Optimizer": "Research param optimizer",
        "Metric": "Total signals",
        "Current": n_score_perf,
        "Threshold": 200,
        "Status": (
            "Data ready — artifact FROZEN since 2026-05-02 (config#1841)"
            if n_score_perf >= 200 else "Deferred"
        ),
        "Live artifact": "config/research_params.json — stale since 2026-05-02",
    },
    {
        "Optimizer": "Roundtrip linkage",
        "Metric": "Paired exit trades",
        "Current": n_roundtrips,
        "Threshold": "—",
        "Status": "Collecting" if n_roundtrips > 0 else "Pending deploy",
        "Live artifact": "—",
    },
    {
        "Optimizer": "4a Scanner auto-relax",
        "Metric": "Scanner eval weeks",
        "Current": n_se_weeks,
        "Threshold": 8,
        "Status": "Active" if n_se_weeks >= 8 else "Collecting",
        "Live artifact": "—",
    },
    {
        "Optimizer": "4b Team slot allocation",
        "Metric": "Team candidate weeks",
        "Current": n_tc_weeks,
        "Threshold": 8,
        "Status": "Active" if n_tc_weeks >= 8 else "Collecting",
        "Live artifact": "—",
    },
    {
        "Optimizer": "4c CIO fallback",
        "Metric": "CIO eval weeks",
        "Current": n_cio_weeks,
        "Threshold": 8,
        "Status": "Active" if n_cio_weeks >= 8 else "Collecting",
        "Live artifact": "—",
    },
    {
        "Optimizer": "4d Predictor p_up sizing",
        "Metric": "Resolved predictions",
        "Current": n_pred_outcomes,
        "Threshold": 30,
        "Status": "Active" if n_pred_outcomes >= 30 else "Collecting",
        "Live artifact": "—",
    },
    {
        "Optimizer": "4e Trigger optimizer",
        "Metric": "Total trades",
        "Current": n_trades,
        "Threshold": 200,
        "Status": "Active" if n_trades >= 200 else "Collecting",
        "Live artifact": "config/executor_params.json — LIVE (the one promoting channel)",
    },
    {
        "Optimizer": "4f Sizing A/B test",
        "Metric": "Total trades",
        "Current": n_trades,
        "Threshold": 50,
        "Status": "Active" if n_trades >= 50 else "Collecting",
        "Live artifact": "—",
    },
]

maturity_df = pd.DataFrame(maturity_data)
st.dataframe(maturity_df, use_container_width=True, hide_index=True)

for row in maturity_data:
    if isinstance(row["Threshold"], int) and row["Threshold"] > 0:
        pct = min(row["Current"] / row["Threshold"], 1.0)
        st.progress(pct, text=f"{row['Optimizer']}: {row['Current']}/{row['Threshold']}")

st.divider()

# ─── Data Manifests ──────────────────────────────────────────────────────
st.subheader("Data Manifests")

manifest_modules = [
    ("executor_morning", _research_bucket()),
    ("daemon", _research_bucket()),
    ("eod_reconcile", _trades_bucket()),
    ("research", _research_bucket()),
    ("predictor_training", _research_bucket()),
    ("predictor_inference", _research_bucket()),
]

for module_name, bucket in manifest_modules:
    manifests = _load_manifests(bucket, module_name, max_days=30)
    if manifests:
        with st.expander(f"{module_name} — {len(manifests)} manifests"):
            latest = manifests[-1]
            st.json(latest)
    else:
        st.caption(f"{module_name} — no manifests yet (will appear after next run)")
