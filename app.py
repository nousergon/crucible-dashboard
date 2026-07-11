"""
Alpha Engine Dashboard — Overview (home page).

Entry point for the Streamlit multi-page app. Designed for triage, not analysis:
answer "is everything working?" in 10 seconds. Detail pages handle the rest.

Layout (top to bottom) — slimmed to a pure triage ROUTER (console-IA phase 3,
config#1989): ONE status truth (the fleet resolver; the old health/*.json
Pipeline banner was a second, disagreeing data path), KPIs sourced from the
same eod_report.json headline the Performance page renders:
  1. Fleet strip + Decision Queue chip
  2. Today's Activity   — compact order-book/trades row → Execution
  3. Key Metrics        — NAV, Daily Alpha (eod_report), Cum Alpha, Hit Rate
  4. System Report Card — chips → Report Card page
  5. Regime chip line   — → Predictor › Regime
  6. Alerts             — drawdown + S3 errors, only when non-empty
"""

import logging
import os
import sys
from datetime import date

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
    get_recent_s3_errors,
    list_eod_report_dates,
    load_eod_pnl,
    load_eod_report,
    load_order_book_summary,
    load_predictor_metrics,
    load_report_card,
    load_trades_full,
)
from shared.formatters import format_dollar, regime_label
from shared.normalizers import to_decimal_series

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
# Section renderers
# ---------------------------------------------------------------------------
# The old health/*.json Pipeline banner (and its _load_module_health reader)
# was retired in console-IA phase 3 (config#1989): the fleet-strip resolver
# consumes the same health stamps with SLA-aware logic (config#1724 doctrine)
# — one status truth on home, not three.


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


def _render_decision_queue_chip() -> None:
    """Home chip for the human-gated backlog pool (config#1926): pending
    count + oldest age, linking to the Decision Queue page. Degrades to a
    named caption — never silently absent (feedback_no_silent_fails)."""
    try:
        from loaders.decision_queue_loader import load_decision_queue

        data = load_decision_queue()
    except Exception as exc:  # noqa: BLE001 — home must render; failure shown
        st.caption(f"⚠️ Decision queue unavailable — {type(exc).__name__}: {exc}")
        return
    items, snoozed = data["items"], data["snoozed"]
    deferred_note = f" · {len(snoozed)} deferred" if snoozed else ""
    if not items:
        st.caption(f"🗳 Decision Queue: clear — nothing gated on you.{deferred_note}")
        return
    st.markdown(
        f"🗳 **{len(items)} decision(s) pending** — oldest {items[0]['age_days']}d{deferred_note}"
    )
    st.page_link("views/49_Decision_Queue.py", label="Open Decision Queue →", icon="🗳")


def _render_todays_activity(
    order_book_summary: dict | None,
    trades_df: pd.DataFrame | None,
) -> None:
    """Compact order-book/trades row → the Execution front page.

    The former Vetoes card was dropped (config#1989): it read the
    never-produced ``config/predictor_params.json`` and silently presented
    the code-default threshold as configured state (I1984 item 4a).
    """
    approved = len(order_book_summary.get("entries_approved", [])) if order_book_summary else 0
    blocked = len(order_book_summary.get("entries_blocked", [])) if order_book_summary else 0
    exits = len(order_book_summary.get("exits", [])) if order_book_summary else 0

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

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Entries Approved", approved)
    c2.metric("Entries Blocked", blocked)
    c3.metric("Exits / Covers", exits)
    c4.metric("Trades Executed Today", trades_today)
    # Host tab, not a registered page — markdown link, not st.page_link
    # (same constraint as the Fleet Status deep-links).
    st.markdown("[Order book & rationale →](/host_execution?tab=Order+Book)")


def _compute_cumulative_alpha(eod_df: pd.DataFrame) -> float | None:
    """Cumulative alpha (decimal) from the eod_pnl series — the one KPI the
    daily eod_report headline doesn't carry (daily NAV/alpha come from the
    report itself, config#1989)."""
    if eod_df is None or eod_df.empty:
        return None

    eod_df = eod_df.copy()
    eod_df["date"] = pd.to_datetime(eod_df["date"])
    eod_df = eod_df.sort_values("date")

    # Portfolio cum return minus SPY cum return, preferring NAV/spy_close
    nav_series = pd.to_numeric(eod_df.get("portfolio_nav"), errors="coerce")
    spy_close = pd.to_numeric(eod_df.get("spy_close"), errors="coerce")

    if nav_series.notna().sum() >= 2 and spy_close.notna().sum() >= 2:
        port_cum = nav_series.iloc[-1] / nav_series.iloc[0] - 1
        spy_cum = spy_close.dropna().iloc[-1] / spy_close.dropna().iloc[0] - 1
        return port_cum - spy_cum
    if "daily_alpha_pct" in eod_df.columns:
        alphas = to_decimal_series(eod_df["daily_alpha_pct"]).dropna()
        if not alphas.empty:
            return float(alphas.sum())
    return None


@st.cache_data(ttl=900)
def _load_latest_eod_report() -> dict | None:
    """Latest eod_report.json — the SAME headline source the Performance
    page renders (config#1989): home and Performance must never disagree on
    NAV / daily alpha because one re-derived them from eod_pnl.csv."""
    dates = list_eod_report_dates()
    return load_eod_report(dates[0]) if dates else None


def _render_key_metrics(
    eod_report: dict | None,
    eod_df: pd.DataFrame | None,
    predictor_metrics: dict | None,
) -> None:
    """Four KPI cards: NAV, Daily Alpha, Cumulative Alpha, Model Hit Rate.

    NAV + daily alpha come from the eod_report headline (one computation
    path, shared with /eod-report); cumulative alpha stays derived from the
    eod_pnl series (the daily report carries no cumulative figure)."""
    summary = (eod_report or {}).get("summary", {})
    nav = summary.get("nav")
    daily_alpha = summary.get("daily_alpha_pct")
    provisional = bool(summary.get("spy_close_provisional"))
    cumulative_alpha = _compute_cumulative_alpha(eod_df)
    hit_rate = (predictor_metrics or {}).get("hit_rate_30d_rolling")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Portfolio NAV", format_dollar(nav) if isinstance(nav, (int, float)) else "—")
    with c2:
        st.metric(
            "Daily Alpha vs SPY" + (" ⏳" if provisional else ""),
            f"{daily_alpha:+.2f}%" if isinstance(daily_alpha, (int, float)) else "—",
            help=("SPY close not yet settled — re-finalizes on the T+1 "
                  "reconcile pass (config#1276)." if provisional else None),
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


def _render_regime_chip(macro_df: pd.DataFrame | None) -> None:
    """One-line regime chip → the Regime tab (the full Market Context grid
    duplicated views/15_Regime — config#1989)."""
    if macro_df is None or macro_df.empty:
        return

    macro_df = macro_df.copy()
    macro_df["date"] = pd.to_datetime(macro_df["date"])
    row = macro_df.sort_values("date").iloc[-1]
    regime = row.get("market_regime", row.get("regime", "—"))
    vix = row.get("vix", None)
    try:
        vix_str = f" · VIX {float(vix):.1f}" if vix is not None else ""
    except (ValueError, TypeError):
        vix_str = ""
    st.markdown(
        f"**Regime:** {regime_label(regime)}{vix_str} &nbsp;·&nbsp; "
        "[Regime detail →](/host_predictor?tab=Regime)"
    )


def _render_alerts(
    eod_df: pd.DataFrame | None,
) -> None:
    """Only shown when non-empty: drawdown warnings + recent S3 errors.

    Module-health alerts were dropped with the Pipeline banner (config#1989)
    — the fleet strip's 🟡/🔴 dots carry that signal with SLA-aware logic."""
    alerts: list[str] = []

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
    st.caption(
        "Full breakdown → **Report Card** (top of the sidebar; the Component "
        "Detail view is its second tab)."
    )


def main() -> None:
    st.title("Alpha Engine")
    st.caption("Autonomous equity portfolio — LLM research + GBM predictions + quantitative execution")

    today = date.today().isoformat()

    with st.spinner("Loading..."):
        eod_df = load_eod_pnl()
        eod_report = _load_latest_eod_report()
        trades_df = load_trades_full()
        macro_df = get_macro_snapshots()
        order_book_summary = load_order_book_summary(today)
        predictor_metrics = load_predictor_metrics()

    st.subheader("Fleet Status")
    _render_fleet_strip()
    _render_decision_queue_chip()

    st.divider()
    st.subheader("Today's Activity")
    _render_todays_activity(order_book_summary, trades_df)

    st.divider()
    st.subheader("Key Metrics")
    _render_key_metrics(eod_report, eod_df, predictor_metrics)

    st.divider()
    st.subheader("System Report Card")
    _render_report_card()

    st.divider()
    _render_regime_chip(macro_df)

    st.divider()
    _render_alerts(eod_df)


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
            # Report Card: one page, Overview + Component Detail views over the
            # same cached artifact (host retired — console-IA phase 1, config#1990).
            page("Report_Card.py", "Report Card", "📋"),
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
            # Daily News is a tab of the Signals host; Focus List moved to
            # the Universe host's Focus Audit tab (console-IA phase 2b,
            # config#1988).
            page("host_research_signals.py", "Signals & Research", "🧭"),
            page("host_universe_scanner.py", "Universe", "🔭"),
            page("host_agent_reviews.py", "Agent Reviews", "🏛"),
            # Daily think-tank desk (config#1579): independent 0-100 ratings
            # vs the scanner composite, thesis browser, themes, run costs.
            page("44_Think_Tank.py", "Think Tank", "🧠"),
        ],
        "🤖 Predictor": [
            # url_path pinned to "predictor" — the predictor's slim morning-
            # briefing email (config#856) deep-links to
            # …/predictor?date=YYYY-MM-DD. Guarded by
            # tests/test_predictor_page.py. (Standalone — folding it into a
            # host would move the slug onto the host; pulled out of
            # host_predictor.py's tab list for the same reason model-zoo/
            # analysis/eod-report are standalone.)
            st.Page(
                "views/7_Predictor.py", title="Predictor", icon="🤖",
                url_path="predictor",
            ),
            page("host_predictor.py", "Predictor Detail", "🔍"),
            # url_path pinned to "model-zoo" — Model-Zoo Rotation digest email
            # deep-links to …/model-zoo?date=YYYY-MM-DD. Guarded by
            # tests/test_model_zoo_page.py. (Standalone — folding it into a host
            # would move the slug onto the host.)
            st.Page(
                "views/35_Model_Zoo.py", title="Model Zoo", icon="🦓",
                url_path="model-zoo",
            ),
            # url_path pinned to "predictor-training" — the weekly training
            # summary email (config#856, slimmed to headline + link) deep-links
            # to …/predictor-training?date=YYYY-MM-DD (the training cycle's
            # trading-day key). Guarded by tests/test_predictor_training_page.py.
            # (Standalone — same rationale as model-zoo/eod-report: folding it
            # into a host would move the slug onto the host.)
            st.Page(
                "views/36_Predictor_Training.py", title="Predictor Training",
                icon="🏋️", url_path="predictor-training",
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
            page("host_eval_backtester.py", "Evaluator", "⚖"),
            page("43_Distillation_Corpus.py", "Distillation Corpus", "🔬"),
        ],
        "⚗️ Experiments": [
            # Crucible product surface v1 (config#1957): experiment-scoped
            # results for the Reference Rate experiment — Overview /
            # Validation / Evaluation tabs over the shared results.view_model
            # layer. Console-mounted for dogfooding; the public
            # crucible.nousergon.ai/dash route flip is gated on the trust
            # battery (config#1958).
            page("host_crucible_results.py", "Crucible Results", "🏛"),
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
            # Human-gated backlog ruling surface (config#1926). url_path
            # pinned to "decision-queue" — the weekly Telegram digest
            # (config#1922) deep-links to it and the home chip page_links to
            # it. Guarded by tests/test_decision_queue_page.py. Standalone
            # st.Page (slug lives on the page, like fleet-status above).
            st.Page(
                "views/49_Decision_Queue.py", title="Decision Queue", icon="🗳",
                url_path="decision-queue",
            ),
            # Renamed from "System Health" (page retired — console-IA phase
            # 2a, config#1987): this host now carries the agent-fleet
            # surfaces (SF/CI Watch, Backlog Groom, Merged PRs). Filename
            # stays host_system_health.py — the Fleet Status deep-link
            # `/host_system_health?tab=Backlog+Groom` is pinned by tests.
            page("host_system_health.py", "Agent Fleet", "🦾"),
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
            # Intraday Surveillance retired (console-IA phase 2a, config#1987):
            # the live NAV strip/curve duplicated the public live page (same
            # shared intraday_live module); its raw daemon-snapshot expanders
            # moved to Fleet Status.
        ],
        "🎙 Morning Signal": [
            # Per-date content schedule for the morning-signal podcast:
            # deep-dive overrides / extra segments / skip days, written to
            # the schedule manifest the generator consumes (morning-signal
            # PR #92; contract in loaders/morning_signal_schedule.py).
            page("45_Morning_Signal_Schedule.py", "Content Schedule", "🗓"),
        ],
        # Renamed from "📚 Reference" (config#2588): now also hosts the
        # browsable private-docs system-doc corpus (System State /
        # Architecture Doc / Experiments Log / Generated Status tabs), not
        # just the original Architecture/Signal Lifecycle/RAG Inventory
        # trio — "Library" is the term Brian used and better fits the wider
        # scope. Filename/key stay host_reference.py / host_reference (no
        # deep-links pin the old "Reference" label).
        "📚 Library": [
            page("host_reference.py", "Library", "📚"),
        ],
    })


_build_navigation().run()
