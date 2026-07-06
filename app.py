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
from nousergon_lib.logging import setup_logging
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


from nousergon_lib.health import DASHBOARD_HEALTH_MODULES


# (module, bucket, stale_after_hrs) — derived from lib (config#1728).
HEALTH_MODULES = list(DASHBOARD_HEALTH_MODULES)


@st.cache_data(ttl=900)
def _load_module_health() -> list[dict]:
    """Load health/{module}.json for each pipeline module."""
    now = datetime.utcnow()
    rows = []
    for module_name, bucket_key, stale_after_hrs in HEALTH_MODULES:
        bucket = _research_bucket() if bucket_key == "research" else _trades_bucket()
        health = _fetch_s3_json(bucket, f"health/{module_name}.json")

        if health is None:
            rows.append({
                "module": module_name,
                "status": "unknown",
                "age_hrs": None,
                "error": None,
                "stale_after_hrs": stale_after_hrs,
            })
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
            "stale_after_hrs": stale_after_hrs,
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


def _render_fleet_strip() -> None:
    """Compact fleet dot-strip (views/48_Fleet_Status.py's resolver over the
    same 25s-cached loader snapshot) + link to the full live grid. Any
    gathering failure renders in-line by name — visible degrade, never a
    silently absent strip (feedback_no_silent_fails)."""
    try:
        from fleet_status import resolve_fleet
        from loaders.fleet_status_loader import gather_fleet_inputs

        statuses = resolve_fleet(gather_fleet_inputs())
    except Exception as exc:  # noqa: BLE001 — degraded strip must not take
        # down the console home; the failure is rendered, not swallowed.
        st.caption(f"⚠️ Fleet status unavailable — {type(exc).__name__}: {exc}")
        return
    parts = [f"{s.icon} {s.label.split(' (')[0]}" for s in statuses]
    st.markdown(" &nbsp;·&nbsp; ".join(parts))
    st.page_link("views/48_Fleet_Status.py", label="Open Fleet Status (live) →", icon="🛰")


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

    # Trades executed today. `date` is the NYSE trading_day the order acted
    # on — strictly backward-looking (DATE_CONVENTIONS.md), so a trade filled
    # intraday today is stamped with YESTERDAY's session and never matches
    # date.today() until after today's own close. `created_at` is the actual
    # fill timestamp, so it's what "today" must compare against here.
    trades_today = 0
    if trades_df is not None and not trades_df.empty and "created_at" in trades_df.columns:
        trades_df = trades_df.copy()
        trades_df["created_at"] = pd.to_datetime(trades_df["created_at"], utc=True).dt.date
        trades_today = int((trades_df["created_at"] == date.today()).sum())

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
        elif row.get("age_hrs") is not None and row["age_hrs"] > row.get("stale_after_hrs", 48):
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

    st.subheader("Fleet Status")
    _render_fleet_strip()

    st.divider()
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

    # IA: one tabbed FRONT PAGE per module (lazy-hosted via shared.view_host —
    # only the selected sub-view executes). Slug-owning pages stay standalone
    # st.Page (url_path is a st.Page-only attribute) so their deep-link guard
    # tests stay green: director / eod-report / model-zoo / analysis.
    return st.navigation({
        "🎯 Overview": [
            home,
            # Report Card front: "Report Card" + "Component Detail" (ex-#9 detail).
            page("host_report_card.py", "Report Card", "📋"),
            # url_path pinned to "director" — Director weekly-plan digest email
            # deep-links to …/director?date=YYYY-MM-DD. Guarded by
            # tests/test_director_page.py.
            st.Page(
                "views/Director_Plan.py", title="Director — Weekly Plan", icon="🧭",
                url_path="director",
            ),
        ],
        "📈 Performance": [
            # Canonical portfolio-outcomes page — merges the former Portfolio,
            # EOD Report and Attribution Heatmaps pages (legacy Metrics retired).
            # url_path pinned to "eod-report" — guarded by
            # tests/test_eod_report_page.py.
            st.Page(
                "views/1_Performance.py", title="Performance", icon="💼",
                url_path="eod-report",
            ),
            # Unified Executor-stage front page: Order Book (with the daily
            # book_status banner) / Execution / Optimizer Decision / Optimizer
            # Risk. The optimizer is the executor's planning stage, so its
            # surfaces file here — not under Research & Signals / Backtester.
            page("host_execution.py", "Execution", "⚡"),
        ],
        "🔬 Research & Signals": [
            page("host_research_signals.py", "Signals & Research", "🧭"),
            page("host_universe_scanner.py", "Universe & Scanner", "🔭"),
            page("host_agent_reviews.py", "Agent Reviews", "🏛"),
            # Daily think-tank desk (config#1579): independent 0-100 ratings
            # vs the scanner composite, thesis browser, themes, run costs.
            page("44_Think_Tank.py", "Think Tank", "🧠"),
            page("Daily_News.py", "Daily News", "📰"),
        ],
        "🤖 Predictor": [
            page("host_predictor.py", "Predictor", "🤖"),
            # url_path pinned to "model-zoo" — Model-Zoo Rotation digest email
            # deep-links to …/model-zoo?date=YYYY-MM-DD. Guarded by
            # tests/test_model_zoo_page.py. (Standalone — folding it into a host
            # would move the slug onto the host.)
            st.Page(
                "views/35_Model_Zoo.py", title="Model Zoo", icon="🦓",
                url_path="model-zoo",
            ),
        ],
        "🧪 Backtester & Eval": [
            # url_path pinned to "analysis" — weekly backtester+eval digest email
            # deep-links to …/analysis?date=YYYY-MM-DD. Guarded by
            # tests/test_analysis_page.py. (Standalone — already 3-tab internally.)
            st.Page(
                "views/3_Analysis.py", title="Analysis", icon="📊",
                url_path="analysis",
            ),
            page("host_eval_backtester.py", "Eval & Backtester", "⚖"),
            page("43_Distillation_Corpus.py", "Distillation Corpus", "🔬"),
        ],
        "⚗️ Experiments": [
            # Champion/challenger ablation ledgers (ARCH §37, config#1685):
            # producer ablation (agentic vs no_agent/single_agent) + scanner
            # ablation (live vs momentum_sleeve). Observe-only leaderboards.
            page("46_Experiments.py", "Ablations", "⚗"),
        ],
        "🩺 System & Ops": [
            # Real-time fleet grid (30s st.fragment auto-refresh): every
            # weekly/daily process with a schedule-aware 🟢🟡🔴⚪ dot.
            # url_path pinned to "fleet-status" (slug guard:
            # tests/test_fleet_status_page.py) — standalone st.Page like
            # pipeline-status below, so home-strip page_links + future
            # notification deep-links stay stable.
            st.Page(
                "views/48_Fleet_Status.py", title="Fleet Status", icon="🛰",
                url_path="fleet-status",
            ),
            page("host_system_health.py", "System Health", "🩺"),
            # url_path pinned to "pipeline-status" — the Step Function
            # failure/complete notifications (nousergon-data) deep-link to
            # …/pipeline-status?run=<execution-name> ($$.Execution.Name). So
            # app.py MUST register this standalone st.Page with
            # url_path="pipeline-status" and the page MUST honor ?run=.
            # Guarded by tests/test_pipeline_status_page.py. Standalone (not a
            # host tab) so the slug lives on the page, like director /
            # eod-report / model-zoo / analysis.
            st.Page(
                "views/25_Pipeline_Status.py", title="Pipeline Status", icon="🚦",
                url_path="pipeline-status",
            ),
            page("host_observability.py", "Observability", "⏱"),
            page("host_cost_usage.py", "Cost & Usage", "💰"),
            page("22_Intraday_Surveillance.py", "Intraday Surveillance", "👁"),
        ],
        "🎙 Morning Signal": [
            # Per-date content schedule for the morning-signal podcast:
            # deep-dive overrides / extra segments / skip days, written to
            # the schedule manifest the generator consumes (morning-signal
            # PR #92; contract in loaders/morning_signal_schedule.py).
            page("45_Morning_Signal_Schedule.py", "Content Schedule", "🗓"),
        ],
        "📚 Reference": [
            page("host_reference.py", "Reference", "📚"),
        ],
    })


_build_navigation().run()
