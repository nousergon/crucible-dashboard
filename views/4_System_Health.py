"""
System Health page — Is the infrastructure working?

Module freshness, data volume, feedback loop maturity, pipeline
manifests, missing data alerts.

Feature Store content lives on its own dedicated page at
/Feature_Store (split out 2026-05-05 for a cleaner screenshare URL).
"""

import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.db_loader import load_research_db
from loaders.s3_loader import (
    _fetch_s3_json,
    _research_bucket,
    _trades_bucket,
    get_s3_client,
    list_s3_prefixes,
    load_eod_pnl,
    load_executor_params,
    load_trades_full,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@st.cache_data(ttl=900)
def _load_health(module: str) -> dict | None:
    return _fetch_s3_json(_research_bucket(), f"health/{module}.json")


@st.cache_data(ttl=900)
def _load_health_from_trades(module: str) -> dict | None:
    return _fetch_s3_json(_trades_bucket(), f"health/{module}.json")


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


# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------

st.title("System Health")
st.caption(
    "Is the plumbing working? Module freshness, data volume, feedback loops. "
    "Feature store inventory lives at [/Feature_Store](/Feature_Store)."
)


# ─── Section 0: Artifact Freshness Monitor KPI strip ────────────────────
# Companion summary for the dedicated page at /Artifact_Freshness.
# Reads the same _freshness_monitor/heartbeat.json the Lambda emits
# every 15min. Absence-driven monitoring complement to the
# event-driven flow-doctor / SF Catch surfaces below.


@st.cache_data(ttl=60)
def _load_freshness_heartbeat() -> dict | None:
    return _fetch_s3_json(_research_bucket(), "_freshness_monitor/heartbeat.json")


_freshness_heartbeat = _load_freshness_heartbeat()
if _freshness_heartbeat is not None:
    st.subheader("Artifact Freshness Monitor")
    _counts = _freshness_heartbeat.get("counts", {})
    _last_run = _freshness_heartbeat.get("last_run", "")
    _alerts_enabled = _freshness_heartbeat.get("alerts_enabled", False)
    _kpi_cols = st.columns(7)
    _kpi_cols[0].metric("Total checked", _freshness_heartbeat.get("n_entries_checked", 0))
    _kpi_cols[1].metric("✅ fresh", _counts.get("fresh", 0))
    _kpi_cols[2].metric("⏳ grace", _counts.get("grace_period", 0))
    _kpi_cols[3].metric("⚠️ stale", _counts.get("stale", 0))
    _kpi_cols[4].metric("❌ missing", _counts.get("missing", 0))
    _kpi_cols[5].metric("🚨 probe failed", _counts.get("probe_failed", 0))
    _kpi_cols[6].metric(
        "Mode", "🔔 alerts live" if _alerts_enabled else "👁 observe",
    )
    st.caption(
        f"Last run: `{_last_run}` — full per-artifact detail at "
        "[/Artifact_Freshness](/Artifact_Freshness)."
    )
    st.divider()


# ─── Section 0.5: Active Observations KPI strip ─────────────────────────
# Companion summary for the dedicated page at /Active_Observations.
# Reads alpha-engine-config/private-docs/OBSERVATION_REGISTRY.yaml
# directly (no Lambda or S3 hop — the registry IS the data). Sibling
# axis to Section 0: freshness is "does the artifact land?";
# observation is "is the consumer plumbed to read it yet?"
# See feedback_observe_mode_unconditional_gates_govern_cutover.

from loaders.observation_registry_loader import (  # noqa: E402
    load_observation_registry,
    summarize_by_phase,
    summarize_by_state,
)


@st.cache_data(ttl=60)
def _load_observation_summary() -> dict | None:
    reg = load_observation_registry()
    if reg is None:
        return None
    obs = reg["observations"]
    return {
        "total": len(obs),
        "state_counts": summarize_by_state(obs),
        "phase_counts": summarize_by_phase(obs),
        "source_path": reg.get("_source_path", "<unknown>"),
    }


_obs_summary = _load_observation_summary()
if _obs_summary is not None:
    st.subheader("Active Observations")
    _state = _obs_summary["state_counts"]
    _phase = _obs_summary["phase_counts"]
    _obs_cols = st.columns(7)
    _obs_cols[0].metric("Total", _obs_summary["total"])
    _obs_cols[1].metric("✅ always-on", _state["always-on"])
    _obs_cols[2].metric("🟡 gated-on (soak)", _state["gated-on"])
    _obs_cols[3].metric("⏸ gated-off", _state["gated-off"])
    _obs_cols[4].metric("🧱 substrate", _phase["substrate"])
    _obs_cols[5].metric("🔁 cutover", _phase["cutover"])
    _obs_cols[6].metric("✅ promoted", _phase["promoted"])
    st.caption(
        "Declarative SoT for in-flight observe-mode rollouts. "
        "Full per-entry detail at "
        "[/Active_Observations](/Active_Observations). "
        "SoT: `alpha-engine-config/private-docs/OBSERVATION_REGISTRY.yaml`."
    )
    st.divider()


# ===========================================================================
# Page body — Modules & Data
# ===========================================================================
# ─── Section 1: Module Health & Freshness ───────────────────────────────
st.subheader("Module Health & Freshness")

health_modules = [
    ("research", _research_bucket()),
    ("predictor_training", _research_bucket()),
    ("predictor_inference", _research_bucket()),
    ("executor", _research_bucket()),
    ("eod_reconcile", _trades_bucket()),
]

now = datetime.utcnow()
health_rows = []
health_cache: dict[str, dict | None] = {}

for module_name, bucket in health_modules:
    if bucket == _trades_bucket():
        health = _load_health_from_trades(module_name)
    else:
        health = _load_health(module_name)
    health_cache[module_name] = health

    if health is None:
        health_rows.append({
            "Module": module_name,
            "Status": "unknown",
            "Last Run": "—",
            "Age (hrs)": "—",
            "Duration (s)": "—",
        })
        continue

    last_success = health.get("last_success")
    age_str = "—"
    if last_success:
        try:
            last_dt = datetime.fromisoformat(last_success.replace("Z", "+00:00")).replace(tzinfo=None)
            age_hrs = (now - last_dt).total_seconds() / 3600
            age_str = f"{age_hrs:.1f}"
        except (ValueError, TypeError):
            pass

    health_rows.append({
        "Module": module_name,
        "Status": health.get("status", "unknown"),
        "Last Run": health.get("run_date", "—"),
        "Age (hrs)": age_str,
        "Duration (s)": health.get("duration_seconds", "—"),
    })

health_df = pd.DataFrame(health_rows)

def _status_color(val):
    if val == "ok":
        return "background-color: #d4edda"
    elif val == "failed":
        return "background-color: #f8d7da"
    elif val == "degraded":
        return "background-color: #fff3cd"
    return ""

st.dataframe(
    health_df.style.map(_status_color, subset=["Status"]),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# ─── Section 1b: Live Optimizer Params (ROADMAP L234) ───────────────────
#
# Surfaces the auto-tuned params the executor reads from S3 at
# cold-start, so the operator no longer has to
# `tail /var/log/executor.log | grep "Loaded executor params from S3"`
# to see effective `min_score_to_enter` / `max_position_pct` /
# `atr_multiplier`. Closes ROADMAP L234 dashboard side; the morning-
# email side is a separate follow-up.

st.subheader("Live Optimizer Params")
st.caption(
    "The backtester's `executor_optimizer` writes auto-tuned values "
    "to `config/executor_params.json` on each Saturday SF promotion. "
    "These OVERRIDE the corresponding keys in the executor's local "
    "`risk.yaml` at cold-start. Keys NOT present here fall through to "
    "the risk.yaml default — see "
    "`alpha-engine/executor/main.py::_load_executor_params_from_s3`."
)

exec_params = load_executor_params()

if not exec_params:
    st.info(
        "No `config/executor_params.json` found — executor running on "
        "hardcoded defaults + local `risk.yaml`. The Saturday SF "
        "backtester optimizer writes this artifact on each promotion."
    )
else:
    meta_cols = st.columns(4)
    updated = exec_params.get("updated_at", "—")
    meta_cols[0].metric("Last promoted", str(updated))
    best_sharpe = exec_params.get("best_sharpe")
    meta_cols[1].metric(
        "Best Sharpe (sweep)",
        f"{best_sharpe:.2f}" if isinstance(best_sharpe, (int, float)) else "—",
    )
    best_alpha = exec_params.get("best_alpha")
    meta_cols[2].metric(
        "Best alpha (sweep)",
        f"{best_alpha:+.1%}" if isinstance(best_alpha, (int, float)) else "—",
    )
    n_combos = exec_params.get("n_combos_tested")
    meta_cols[3].metric(
        "Combos tested",
        f"{int(n_combos):,}" if isinstance(n_combos, (int, float)) else "—",
    )

    _PARAM_LABELS = {
        "min_score": "min_score_to_enter (research score)",
        "max_position_pct": "max_position_pct (NAV)",
        "atr_multiplier": "atr_multiplier (trailing stop)",
        "time_decay_reduce_days": "time_decay_reduce_days",
        "time_decay_exit_days": "time_decay_exit_days",
        "profit_take_pct": "profit_take_pct (intraday)",
        "reduce_fraction": "reduce_fraction",
        "atr_sizing_target_risk": "atr_sizing_target_risk",
        "confidence_sizing_min": "confidence_sizing_min",
        "confidence_sizing_range": "confidence_sizing_range",
        "staleness_decay_per_day": "staleness_decay_per_day",
        "earnings_sizing_reduction": "earnings_sizing_reduction",
        "earnings_proximity_days": "earnings_proximity_days",
        "momentum_gate_threshold": "momentum_gate_threshold",
        "correlation_block_threshold": "correlation_block_threshold",
        "momentum_exit_threshold": "momentum_exit_threshold",
        "use_p_up_sizing": "use_p_up_sizing (Phase 4 flag)",
        "p_up_sizing_blend": "p_up_sizing_blend",
        "disabled_triggers": "disabled_triggers (intraday)",
    }
    _METADATA_KEYS = {
        "updated_at", "best_sharpe", "best_alpha", "improvement_pct",
        "improvement_delta", "n_combos_tested", "manual_override",
    }
    rows = []
    for key, val in exec_params.items():
        if key in _METADATA_KEYS:
            continue
        label = _PARAM_LABELS.get(key, key)
        if isinstance(val, float):
            display = f"{val:.4f}" if abs(val) < 1 else f"{val:.2f}"
        elif isinstance(val, list):
            display = ", ".join(str(x) for x in val) if val else "(none)"
        else:
            display = str(val)
        rows.append({"Param": label, "Live value": display, "Source": "S3 (auto-tuned)"})

    if rows:
        params_df = pd.DataFrame(rows).sort_values("Param").reset_index(drop=True)
        st.dataframe(params_df, hide_index=True, use_container_width=True)
    else:
        st.caption("Artifact present but no auto-tuned param keys recognized.")

    if exec_params.get("manual_override"):
        st.warning(
            "**Manual override flag set** in `config/executor_params.json` — "
            "the backtester optimizer is currently held off the auto-apply "
            "path. Investigate before next Saturday SF."
        )

    improvement_pct = exec_params.get("improvement_pct")
    if isinstance(improvement_pct, (int, float)):
        st.caption(
            f"Promotion gate margin: **{improvement_pct:+.1%}** Sharpe "
            f"improvement vs prior live config (backtester "
            f"`executor_optimizer.recommend` decision)."
        )

st.divider()

# ─── Section 2: Data Volume Growth ──────────────────────────────────────
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

# ─── Section 3: Feedback Loop Maturity ──────────────────────────────────
st.subheader("Feedback Loop Maturity")

n_score_perf = table_counts.get("score_performance", 0)
n_pred_outcomes = table_counts.get("predictor_outcomes", 0)

conn = load_research_db()
n_resolved_21d = 0
if conn:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM score_performance WHERE return_21d IS NOT NULL"
        ).fetchone()
        n_resolved_21d = row[0] if row else 0
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

maturity_data = [
    {
        "Optimizer": "Scoring weights",
        "Metric": "21d resolved signals",
        "Current": n_resolved_21d,
        "Threshold": 30,
        "Status": "Active" if n_resolved_21d >= 30 else "Blocked",
    },
    {
        "Optimizer": "Attribution analysis",
        "Metric": "21d resolved signals",
        "Current": n_resolved_21d,
        "Threshold": 50,
        "Status": "Active" if n_resolved_21d >= 50 else "Blocked",
    },
    {
        "Optimizer": "Predictor veto tuning",
        "Metric": "Resolved predictions",
        "Current": n_pred_outcomes,
        "Threshold": 20,
        "Status": "Active" if n_pred_outcomes >= 20 else "Blocked",
    },
    {
        "Optimizer": "Research param optimizer",
        "Metric": "Total signals",
        "Current": n_score_perf,
        "Threshold": 200,
        "Status": "Active" if n_score_perf >= 200 else "Deferred",
    },
    {
        "Optimizer": "Roundtrip linkage",
        "Metric": "Paired exit trades",
        "Current": n_roundtrips,
        "Threshold": "—",
        "Status": "Collecting" if n_roundtrips > 0 else "Pending deploy",
    },
    {
        "Optimizer": "4a Scanner auto-relax",
        "Metric": "Scanner eval weeks",
        "Current": n_se_weeks,
        "Threshold": 8,
        "Status": "Active" if n_se_weeks >= 8 else "Collecting",
    },
    {
        "Optimizer": "4b Team slot allocation",
        "Metric": "Team candidate weeks",
        "Current": n_tc_weeks,
        "Threshold": 8,
        "Status": "Active" if n_tc_weeks >= 8 else "Collecting",
    },
    {
        "Optimizer": "4c CIO fallback",
        "Metric": "CIO eval weeks",
        "Current": n_cio_weeks,
        "Threshold": 8,
        "Status": "Active" if n_cio_weeks >= 8 else "Collecting",
    },
    {
        "Optimizer": "4d Predictor p_up sizing",
        "Metric": "Resolved predictions",
        "Current": n_pred_outcomes,
        "Threshold": 30,
        "Status": "Active" if n_pred_outcomes >= 30 else "Collecting",
    },
    {
        "Optimizer": "4e Trigger optimizer",
        "Metric": "Total trades",
        "Current": n_trades,
        "Threshold": 200,
        "Status": "Active" if n_trades >= 200 else "Collecting",
    },
    {
        "Optimizer": "4f Sizing A/B test",
        "Metric": "Total trades",
        "Current": n_trades,
        "Threshold": 50,
        "Status": "Active" if n_trades >= 50 else "Collecting",
    },
]

maturity_df = pd.DataFrame(maturity_data)
st.dataframe(maturity_df, use_container_width=True, hide_index=True)

for row in maturity_data:
    if isinstance(row["Threshold"], int) and row["Threshold"] > 0:
        pct = min(row["Current"] / row["Threshold"], 1.0)
        st.progress(pct, text=f"{row['Optimizer']}: {row['Current']}/{row['Threshold']}")

st.divider()

# ─── Section 4: Data Manifests ──────────────────────────────────────────
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

st.divider()

# ─── Section 5: Missing Data Alerts ─────────────────────────────────────
st.subheader("Missing Data Alerts")

alerts = []

if eod_df is not None and not eod_df.empty and "date" in eod_df.columns:
    eod_dates = set(pd.to_datetime(eod_df["date"]).dt.date)
    today = date.today()
    check_date = today
    missing_eod = []
    for _ in range(30):
        check_date -= timedelta(days=1)
        if check_date.weekday() < 5 and check_date not in eod_dates:
            missing_eod.append(str(check_date))
    if missing_eod:
        alerts.append(f"Missing EOD records for {len(missing_eod)} trading day(s): {', '.join(missing_eod[:5])}")

for module_name, _ in health_modules:
    health = health_cache.get(module_name)
    if health and health.get("status") == "failed":
        alerts.append(f"Module **{module_name}** last status: FAILED — {health.get('error', 'unknown error')}")
    elif health is None:
        alerts.append(f"Module **{module_name}** has no health status (never run?)")

if n_score_perf > 0 and n_resolved_21d < n_score_perf:
    n_pending = n_score_perf - n_resolved_21d
    alerts.append(f"{n_pending} score_performance rows awaiting 21d return resolution")

if alerts:
    for alert in alerts:
        st.warning(alert)
else:
    st.success("No data alerts. All systems nominal.")

