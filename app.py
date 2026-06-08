"""
Alpha Engine Dashboard — Overview (home page).

Entry point for the Streamlit multi-page app. Designed for triage, not analysis:
answer "is everything working?" in 10 seconds. Detail pages handle the rest.

Layout (top to bottom):
  1. Status Banner      — pipeline module health (green/yellow/red)
  2. Today's Activity   — compact activity feed (approvals, vetoes, trades)
  3. Key Metrics        — NAV, Daily Alpha, Cumulative Alpha, Model Hit Rate
  4. Market Context     — regime, VIX, 10yr yield (single row)
  5. Alerts             — only shown when non-empty
"""

import logging
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Structured logging + flow-doctor singleton via alpha-engine-lib (shared
# pattern across all 5 entrypoints; see executor/main.py for reference).
# When FLOW_DOCTOR_ENABLED=1, attaches a FlowDoctorHandler at ERROR so
# every logger.error() call across app.py + pages/* + loaders/* routes
# through flow-doctor's dispatch (email + GitHub issue) without explicit
# fd.report() plumbing — child loggers propagate to the root handler.
#
# Module-top so import-time errors in streamlit / pandas / loaders are
# also captured. Streamlit is a long-running EC2 process (not Lambda),
# no cold-start init-timeout concern. flow-doctor.yaml lives at the
# repo root next to this file.
#
# exclude_patterns starts empty by deliberate convention: add patterns
# only after observing real ERROR-level noise from the dashboard.
from alpha_engine_lib.logging import setup_logging
_FLOW_DOCTOR_EXCLUDE_PATTERNS: list[str] = []
_FLOW_DOCTOR_YAML = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "flow-doctor.yaml"
)
setup_logging(
    "dashboard",
    flow_doctor_yaml=_FLOW_DOCTOR_YAML,
    exclude_patterns=_FLOW_DOCTOR_EXCLUDE_PATTERNS,
)

import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)

from loaders.db_loader import get_macro_snapshots
from components.report_card_v2 import render_home_summary
from loaders.s3_loader import (
    _fetch_s3_json,
    _research_bucket,
    _trades_bucket,
    get_recent_s3_errors,
    load_eod_pnl,
    load_order_book_summary,
    load_predictions_json,
    load_predictor_metrics,
    load_report_card,
    load_trades_full,
)
from shared.constants import get_thresholds
from shared.formatters import format_dollar, regime_label
from shared.normalizers import to_decimal_scalar, to_decimal_series

_TH = get_thresholds()
_VETO_CONF_DEFAULT = _TH["veto_confidence"]

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Alpha Engine — Nous Ergon",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------


HEALTH_MODULES = [
    ("research", "research"),
    ("predictor_training", "research"),
    ("predictor_inference", "research"),
    ("executor", "research"),
    ("eod_reconcile", "trades"),
]


@st.cache_data(ttl=900)
def _load_module_health() -> list[dict]:
    """Load health/{module}.json for each pipeline module."""
    now = datetime.utcnow()
    rows = []
    for module_name, bucket_key in HEALTH_MODULES:
        bucket = _research_bucket() if bucket_key == "research" else _trades_bucket()
        health = _fetch_s3_json(bucket, f"health/{module_name}.json")

        if health is None:
            rows.append({"module": module_name, "status": "unknown", "age_hrs": None, "error": None})
            continue

        last_success = health.get("last_success")
        age_hrs = None
        if last_success:
            try:
                last_dt = datetime.fromisoformat(last_success.replace("Z", "+00:00")).replace(tzinfo=None)
                age_hrs = (now - last_dt).total_seconds() / 3600
            except (ValueError, TypeError):
                pass

        rows.append({
            "module": module_name,
            "status": health.get("status", "unknown"),
            "age_hrs": age_hrs,
            "error": health.get("error"),
        })
    return rows


def _status_icon(status: str) -> str:
    if status == "ok":
        return "🟢"
    if status == "degraded":
        return "🟡"
    if status == "failed":
        return "🔴"
    return "⚪"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_status_banner(health_rows: list[dict]) -> None:
    """One compact row with colored badges for each module."""
    cols = st.columns(len(health_rows))
    for col, row in zip(cols, health_rows):
        with col:
            icon = _status_icon(row["status"])
            age = row.get("age_hrs")
            age_str = f"{age:.0f}h ago" if age is not None else "—"
            st.metric(f"{icon} {row['module']}", age_str, delta=row["status"], delta_color="off")


def _render_todays_activity(
    order_book_summary: dict | None,
    predictions_data: dict,
    trades_df: pd.DataFrame | None,
) -> None:
    """Compact summary — entries, exits, vetoes, trades. Metric cards only."""
    approved = len(order_book_summary.get("entries_approved", [])) if order_book_summary else 0
    blocked = len(order_book_summary.get("entries_blocked", [])) if order_book_summary else 0
    exits = len(order_book_summary.get("exits", [])) if order_book_summary else 0

    # Count high-confidence vetoes
    vetoes = 0
    if predictions_data:
        predictor_params = _fetch_s3_json(_research_bucket(), "config/predictor_params.json") or {}
        veto_threshold = predictor_params.get("veto_confidence", _VETO_CONF_DEFAULT)
        for pred in predictions_data.values():
            if pred.get("predicted_direction") == "DOWN" and (pred.get("prediction_confidence") or 0) >= veto_threshold:
                vetoes += 1

    # Trades executed today
    trades_today = 0
    if trades_df is not None and not trades_df.empty and "date" in trades_df.columns:
        trades_df = trades_df.copy()
        trades_df["date"] = pd.to_datetime(trades_df["date"]).dt.date
        trades_today = int((trades_df["date"] == date.today()).sum())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Entries Approved", approved)
    c2.metric("Entries Blocked", blocked)
    c3.metric("Exits / Covers", exits)
    c4.metric("Vetoes", vetoes)
    c5.metric("Trades Executed Today", trades_today)


def _compute_cumulative_alpha(eod_df: pd.DataFrame) -> tuple[float | None, float | None]:
    """Return (daily_alpha, cumulative_alpha) — both in decimal form."""
    if eod_df is None or eod_df.empty:
        return None, None

    eod_df = eod_df.copy()
    eod_df["date"] = pd.to_datetime(eod_df["date"])
    eod_df = eod_df.sort_values("date")

    daily_alpha = None
    if "daily_alpha_pct" in eod_df.columns:
        last_row = eod_df.iloc[-1]
        daily_alpha = to_decimal_scalar(last_row.get("daily_alpha_pct"))

    # Cumulative alpha: portfolio cum return minus SPY cum return, preferring NAV/spy_close
    nav_series = pd.to_numeric(eod_df.get("portfolio_nav"), errors="coerce")
    spy_close = pd.to_numeric(eod_df.get("spy_close"), errors="coerce")
    cumulative_alpha = None

    if nav_series.notna().sum() >= 2 and spy_close.notna().sum() >= 2:
        port_cum = nav_series.iloc[-1] / nav_series.iloc[0] - 1
        spy_cum = spy_close.dropna().iloc[-1] / spy_close.dropna().iloc[0] - 1
        cumulative_alpha = port_cum - spy_cum
    elif "daily_alpha_pct" in eod_df.columns:
        alphas = to_decimal_series(eod_df["daily_alpha_pct"]).dropna()
        if not alphas.empty:
            cumulative_alpha = alphas.sum()

    return daily_alpha, cumulative_alpha


def _render_key_metrics(eod_df: pd.DataFrame | None, predictor_metrics: dict | None) -> None:
    """Four KPI cards: NAV, Daily Alpha, Cumulative Alpha, Model Hit Rate."""
    nav = None
    if eod_df is not None and not eod_df.empty:
        nav = pd.to_numeric(eod_df.sort_values("date").iloc[-1].get("portfolio_nav"), errors="coerce")

    daily_alpha, cumulative_alpha = _compute_cumulative_alpha(eod_df)
    hit_rate = (predictor_metrics or {}).get("hit_rate_30d_rolling")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Portfolio NAV", format_dollar(nav) if nav and pd.notna(nav) else "—")
    with c2:
        st.metric(
            "Daily Alpha vs SPY",
            f"{daily_alpha * 100:+.2f}%" if daily_alpha is not None else "—",
        )
    with c3:
        st.metric(
            "Cumulative Alpha",
            f"{cumulative_alpha * 100:+.1f}%" if cumulative_alpha is not None else "—",
        )
    with c4:
        if hit_rate is not None:
            st.metric("Model Hit Rate (30d)", f"{float(hit_rate):.1%}")
        else:
            st.metric("Model Hit Rate (30d)", "—")


def _render_market_context(macro_df: pd.DataFrame | None) -> None:
    if macro_df is None or macro_df.empty:
        return

    macro_df = macro_df.copy()
    macro_df["date"] = pd.to_datetime(macro_df["date"])
    today_macro = macro_df[macro_df["date"].dt.date == date.today()]
    if today_macro.empty:
        today_macro = macro_df.tail(1)
    if today_macro.empty:
        return

    row = today_macro.iloc[-1]
    regime = row.get("market_regime", row.get("regime", "—"))
    vix = row.get("vix", "—")
    yield_10yr = row.get("yield_10yr", row.get("10yr_yield", "—"))

    mc1, mc2, mc3 = st.columns(3)
    with mc1:
        st.metric("Regime", regime_label(regime))
    with mc2:
        try:
            st.metric("VIX", f"{float(vix):.1f}")
        except (ValueError, TypeError):
            st.metric("VIX", str(vix))
    with mc3:
        try:
            st.metric("10yr Yield", f"{float(yield_10yr):.2f}%")
        except (ValueError, TypeError):
            st.metric("10yr Yield", str(yield_10yr))


def _render_alerts(
    health_rows: list[dict],
    eod_df: pd.DataFrame | None,
) -> None:
    """Only shown when non-empty. Failed modules, stale modules, S3 errors, drawdown warnings."""
    alerts: list[str] = []

    # Failed or stale modules
    for row in health_rows:
        if row["status"] == "failed":
            err = row.get("error") or "unknown error"
            alerts.append(f"❌ Module **{row['module']}** FAILED — {err}")
        elif row["status"] == "unknown":
            alerts.append(f"⚠ Module **{row['module']}** has no health status (never run?)")
        elif row.get("age_hrs") is not None and row["age_hrs"] > 48:
            alerts.append(f"⚠ Module **{row['module']}** stale — last success {row['age_hrs']:.0f}h ago")

    # Drawdown warning
    if eod_df is not None and not eod_df.empty and "daily_return_pct" in eod_df.columns:
        returns = to_decimal_series(eod_df["daily_return_pct"]).dropna()
        if not returns.empty:
            cum = returns.cumsum()
            current_dd = (cum - cum.cummax()).iloc[-1]
            if current_dd <= -0.05:
                alerts.append(f"📉 Current drawdown: {current_dd * 100:.1f}%")

    # Recent S3 errors
    s3_errors = get_recent_s3_errors()
    if s3_errors:
        latest = s3_errors[-1]
        alerts.append(
            f"S3 error: **{latest.get('error_type', '?')}** on `{latest.get('key', '?')}` "
            f"— {latest.get('message', '')[:100]}"
        )

    if alerts:
        st.subheader("Alerts")
        for a in alerts:
            st.warning(a)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _render_report_card() -> None:
    """Home headline — the Report Card v2 overall + 7-tile chips.

    Reads the evaluator's ``report_card.json`` (7-tile MetricRecord substrate);
    full detail lives on the Report Card pages. Replaces the legacy v1
    grading.json summary (3 modules, letters only).
    """
    render_home_summary(load_report_card())
    st.caption("Full breakdown → **Report Card** + **Report Card — Detail** (top of the sidebar).")


def main() -> None:
    st.title("Alpha Engine")
    st.caption("Autonomous equity portfolio — LLM research + GBM predictions + quantitative execution")

    today = date.today().isoformat()

    with st.spinner("Loading..."):
        eod_df = load_eod_pnl()
        trades_df = load_trades_full()
        macro_df = get_macro_snapshots()
        predictions_data = load_predictions_json()
        order_book_summary = load_order_book_summary(today)
        predictor_metrics = load_predictor_metrics()
        health_rows = _load_module_health()

    st.subheader("Pipeline Status")
    _render_status_banner(health_rows)

    st.divider()
    st.subheader("Today's Activity")
    _render_todays_activity(order_book_summary, predictions_data, trades_df)

    st.divider()
    st.subheader("Key Metrics")
    _render_key_metrics(eod_df, predictor_metrics)

    st.divider()
    st.subheader("System Report Card")
    _render_report_card()

    st.divider()
    st.subheader("Market Context")
    _render_market_context(macro_df)

    st.divider()
    _render_alerts(health_rows, eod_df)


# ---------------------------------------------------------------------------
# Navigation — grouped sections (st.navigation) replacing the flat pages/ menu.
#
# Order is intentional: the NEW Report Card surface leads; then the reused
# operational categories; then a Deprecated section (candidates the Report Card
# substantially covers — kept, not yet removed, pending operator confirmation).
# set_page_config (top of file) is the single entrypoint config; the view
# scripts no longer call it (st.navigation requirement).
# ---------------------------------------------------------------------------

def _build_navigation():
    home = st.Page(main, title="Home", icon="🏠", default=True)

    def page(path, title, icon):
        return st.Page(f"views/{path}", title=title, icon=icon)

    return st.navigation({
        "🎯 Overview & Report Card": [
            home,
            page("Report_Card.py", "Report Card", "📋"),
            page("Report_Card_Detail.py", "Report Card — Detail", "🔎"),
            page("Director_Plan.py", "Director — Weekly Plan", "🧭"),
        ],
        "📈 Performance": [
            page("1_Portfolio.py", "Portfolio", "💼"),
            page("6_Execution.py", "Execution", "⚡"),
            page("19_EOD_Reconcile_Archive.py", "EOD Reconcile (archive)", "🧾"),
        ],
        "🔬 Research & Signals": [
            page("2_Signals_and_Research.py", "Signals & Research", "🧭"),
            page("29_Decision_Review.py", "Decision Review", "🔍"),
            page("5_Focus_List.py", "Focus List", "🎯"),
            page("16_Order_Book_Rationale.py", "Order Book Rationale", "📒"),
            page("17_Research_Briefing_Archive.py", "Research Briefing (archive)", "📰"),
            page("22_Intraday_Surveillance.py", "Intraday Surveillance", "👁"),
        ],
        "🤖 Predictor": [
            page("7_Predictor.py", "Predictor", "🤖"),
            page("15_Regime.py", "Regime", "🌐"),
            page("13_Feature_Store.py", "Feature Store", "🗃"),
            page("18_Predictor_Briefing_Archive.py", "Predictor Briefing (archive)", "📨"),
            page("20_Predictor_Training_Archive.py", "Training Runs (archive)", "🏋"),
        ],
        "🧪 Backtester & Eval": [
            page("3_Analysis.py", "Analysis", "📊"),
            page("8_Eval_Quality.py", "Eval Quality", "⚖"),
            page("12_Feedback_Loop.py", "Feedback Loop", "🔁"),
            page("21_Backtester_Evaluator_Archive.py", "Backtester Report (archive)", "📑"),
        ],
        "🩺 System & Ops": [
            page("4_System_Health.py", "System Health", "🩺"),
            page("25_Pipeline_Status.py", "Pipeline Status", "🚦"),
            page("26_Artifact_Freshness.py", "Artifact Freshness", "⏱"),
            page("27_Active_Observations.py", "Active Observations", "🔭"),
            page("28_Retros.py", "Retros", "📓"),
            page("23_LLM_Cost.py", "LLM Cost", "💰"),
        ],
        "📚 Reference & Deep-Dives": [
            page("10_Architecture.py", "Architecture", "🏛"),
            page("11_Signal_Lifecycle.py", "Signal Lifecycle", "🧬"),
            page("14_RAG_Inventory.py", "RAG Inventory", "📚"),
        ],
        # Candidates the Report Card v2 substantially subsumes on the console
        # (both primarily back the PUBLIC site, which is unaffected). Kept for
        # now — confirm before removal.
        "🗑 Deprecated (review)": [
            page("9_Metrics.py", "Metrics (legacy)", "🧮"),
            page("24_Evidence.py", "Evidence (legacy)", "🔖"),
        ],
    })


_build_navigation().run()
