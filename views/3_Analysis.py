"""
Analysis page — Are signals predictive? How is the system learning?

Merges the former Signal Quality, Backtester, and Evaluation pages
(Phase 4 of dashboard-plan-optimized-260404) into three tabs:

  • Signal Accuracy  — accuracy trends, buckets, regime, alpha distribution
  • Backtester       — run summary, portfolio sim, param sweep, attribution, weights
  • Pipeline Eval    — lift waterfall, component diagnostics, self-adjustment status

Scoring weights live on the Backtester tab (they are produced by the backtester).
Predictor accuracy is no longer on this page — it will be consolidated on the
Predictor page in Phase 6.
"""

import logging
import os
import sys

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from charts.accuracy_chart import (
    make_accuracy_by_bucket_chart,
    make_accuracy_by_regime_chart,
    make_accuracy_trend_chart,
    make_alpha_distribution_chart,
    make_regime_alpha_chart,
)
from charts.attribution_chart import make_attribution_chart, make_weight_history_chart
from components import backtester_significance as bsig
from components import sweep_distribution as sweepdist
from loaders.db_loader import get_macro_snapshots, get_score_performance
from loaders.outcome_store import BEAT_SPY_PRIMARY
from loaders.s3_loader import (
    _fetch_s3_json,
    _research_bucket,
    list_backtest_dates,
    load_backtest_file,
    load_eod_pnl,
    load_executor_params,
    load_executor_params_history,
    load_research_params,
    load_scoring_weights,
    load_scoring_weights_history,
)
from shared.formatters import format_pct


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_float(val, decimals=3) -> str:
    try:
        return f"{float(val):.{decimals}f}"
    except (ValueError, TypeError):
        return str(val) if val is not None else "—"


def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _extract_section(md: str, heading: str) -> str | None:
    """Extract a markdown section by heading (## level)."""
    if not md:
        return None
    marker = f"## {heading}"
    start = md.find(marker)
    if start == -1:
        return None
    rest = md[start + len(marker):]
    end = rest.find("\n## ")
    if end == -1:
        end = rest.find("\n# ")
    if end == -1:
        end = rest.find("\n---")
    return rest[:end].strip() if end != -1 else rest.strip()


def _make_heatmap(sweep_df: pd.DataFrame, cb_value: float | None) -> go.Figure:
    """Build Sharpe heatmap for a given circuit breaker value."""
    if cb_value is not None and "drawdown_circuit_breaker" in sweep_df.columns:
        sub = sweep_df[
            pd.to_numeric(sweep_df["drawdown_circuit_breaker"], errors="coerce") == cb_value
        ]
    else:
        sub = sweep_df

    if sub.empty:
        fig = go.Figure()
        fig.update_layout(title=f"No data for CB={cb_value}")
        return fig

    x_col = next((c for c in ["min_score", "min_score_threshold"] if c in sub.columns), None)
    y_col = next((c for c in ["max_position_pct", "max_position_size"] if c in sub.columns), None)
    z_col = next((c for c in ["sharpe", "sharpe_ratio"] if c in sub.columns), None)

    if not x_col or not y_col or not z_col:
        fig = go.Figure()
        fig.update_layout(title=f"CB={cb_value} — Missing columns (need min_score, max_position_pct, sharpe)")
        return fig

    pivot = sub.pivot_table(index=y_col, columns=x_col, values=z_col, aggfunc="mean")
    pivot = pivot.sort_index(ascending=False)

    title = (
        f"Sharpe Ratio Heatmap (Circuit Breaker: {cb_value * 100:.0f}%)"
        if cb_value is not None
        else "Sharpe Ratio Heatmap"
    )
    fig = px.imshow(
        pivot,
        labels=dict(x="Min Score", y="Max Position %", color="Sharpe"),
        color_continuous_scale="RdYlGn",
        aspect="auto",
        title=title,
        text_auto=".2f",
    )
    fig.update_layout(
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=60, b=40, l=80, r=20),
        coloraxis_colorbar=dict(title="Sharpe"),
    )
    return fig


# ---------------------------------------------------------------------------
# Page header + shared backtest date selector
# ---------------------------------------------------------------------------

st.title("Analysis")
st.caption("Signal accuracy, backtester results, and pipeline evaluation.")

backtest_dates = list_backtest_dates()
selected_backtest_date = None
if backtest_dates:
    # Honor the ?date= deep-link from the weekly backtester+eval digest email
    # (…/analysis?date=YYYY-MM-DD — the backtest run_date / last completed
    # trading day), defaulting to the latest run. Mirrors the EOD Report page.
    qp_date = st.query_params.get("date")
    default_idx = backtest_dates.index(qp_date) if qp_date in backtest_dates else 0
    selected_backtest_date = st.selectbox(
        "Backtest Run Date",
        options=backtest_dates,
        index=default_idx,
        help="Applies to the Backtester and Pipeline Evaluation tabs",
    )
    st.query_params["date"] = selected_backtest_date

tab_accuracy, tab_backtest, tab_tuning, tab_eval = st.tabs(
    ["Signal Accuracy", "Backtester", "Self-Tuning", "Pipeline Evaluation"]
)

# ===========================================================================
# TAB 1: Signal Accuracy
# ===========================================================================
with tab_accuracy:
    st.subheader("Historical accuracy of Alpha Engine signals vs SPY")

    with st.spinner("Loading signal performance data..."):
        perf_df = get_score_performance()
        macro_df = get_macro_snapshots()

    if perf_df is None or perf_df.empty:
        st.warning(
            "score_performance table is empty or research.db is unavailable. "
            "Signal quality metrics will not be available until the Research Lambda "
            "has run and populated outcome data."
        )
    else:
        beat_21d_col = BEAT_SPY_PRIMARY if BEAT_SPY_PRIMARY in perf_df.columns else None
        populated_rows = int(perf_df[beat_21d_col].notna().sum()) if beat_21d_col else 0

        if populated_rows < 20:
            st.info(
                f"Only {populated_rows} signals have 21d outcome data populated "
                f"(need ≥ 20 for meaningful accuracy stats). Charts will update as outcomes accrue."
            )

        st.markdown("**Accuracy Trend Over Time**")
        st.plotly_chart(make_accuracy_trend_chart(perf_df), use_container_width=True)

        st.markdown("**Accuracy by Score Bucket**")
        st.plotly_chart(make_accuracy_by_bucket_chart(perf_df), use_container_width=True)

        st.markdown("**Accuracy by Market Regime**")
        if macro_df is None or macro_df.empty:
            st.warning("Macro data not available — cannot show regime breakdown.")
        else:
            st.plotly_chart(make_accuracy_by_regime_chart(perf_df, macro_df), use_container_width=True)

        st.markdown("**Alpha by Market Regime**")
        if macro_df is None or macro_df.empty:
            st.warning("Macro data not available — cannot show regime alpha.")
        else:
            eod_df = load_eod_pnl()
            if eod_df is not None and not eod_df.empty and "daily_alpha_pct" in eod_df.columns:
                st.plotly_chart(make_regime_alpha_chart(eod_df, macro_df), use_container_width=True)
            else:
                st.info("Portfolio P&L data not available for regime alpha analysis.")

        st.markdown("**Alpha Distribution (21d Return vs SPY)**")
        st.plotly_chart(make_alpha_distribution_chart(perf_df), use_container_width=True)


# ===========================================================================
# TAB 2: Backtester
# ===========================================================================
with tab_backtest:
    if not backtest_dates:
        st.warning("No backtest results found in S3. Run the backtester to populate results.")
    else:
        with st.spinner(f"Loading backtest results for {selected_backtest_date}..."):
            metrics = load_backtest_file(selected_backtest_date, "metrics.json")
            sweep_df = load_backtest_file(selected_backtest_date, "param_sweep.csv")
            signal_quality_df = load_backtest_file(selected_backtest_date, "signal_quality.csv")
            attribution = load_backtest_file(selected_backtest_date, "attribution.json")
            report_md_bt = load_backtest_file(selected_backtest_date, "report.md")

        # ---- Cross-Date Trend (config#1444 item 2) ----
        # backtest_dates is newest-first; take the most recent N for the trend.
        _TREND_N = 12
        _per_date: dict[str, dict] = {}
        for _d in backtest_dates[:_TREND_N]:
            if _d == selected_backtest_date and isinstance(metrics, dict):
                _per_date[_d] = metrics
            else:
                _m = load_backtest_file(_d, "metrics.json")
                if isinstance(_m, dict):
                    _per_date[_d] = _m
        bsig.render_trend(_per_date, n_shown=len(_per_date), n_total=len(backtest_dates))
        st.divider()

        # ---- Last Run Summary ----
        st.subheader("Last Run Summary")
        if not metrics:
            st.warning("metrics.json not found for this backtest run.")
        else:
            run_date = metrics.get("run_date", metrics.get("date", selected_backtest_date))
            strategy = metrics.get("strategy", metrics.get("strategy_name", "—"))
            universe_size = metrics.get("universe_size", metrics.get("num_signals", "—"))
            data_start = metrics.get("data_start", metrics.get("start_date", "—"))
            data_end = metrics.get("data_end", metrics.get("end_date", "—"))

            b1, b2, b3 = st.columns(3)
            with b1:
                st.metric("Run Date", str(run_date))
                st.metric("Strategy", str(strategy))
            with b2:
                st.metric("Data Range", f"{data_start} → {data_end}")
                st.metric("Universe Size", str(universe_size))
            with b3:
                runtime = metrics.get("runtime_seconds", metrics.get("runtime", "—"))
                st.metric("Runtime", f"{runtime}s" if runtime != "—" else "—")
                st.metric("Status", str(metrics.get("status", "—")))

        st.divider()

        # ---- Portfolio Simulation Stats ----
        st.subheader("Portfolio Simulation Stats")
        if metrics:
            sim = metrics.get("simulation", metrics)
            m1, m2, m3 = st.columns(3)
            m4, m5, m6 = st.columns(3)
            with m1:
                st.metric("Total Return", format_pct(sim.get("total_return")))
            with m2:
                st.metric("Sharpe Ratio", _fmt_float(sim.get("sharpe_ratio", sim.get("sharpe"))))
            with m3:
                st.metric("Max Drawdown", format_pct(sim.get("max_drawdown")))
            with m4:
                st.metric("Win Rate", format_pct(sim.get("win_rate")))
            with m5:
                st.metric("Avg Alpha", format_pct(sim.get("avg_alpha", sim.get("mean_alpha"))))
            with m6:
                st.metric("Num Trades", str(sim.get("num_trades", sim.get("trade_count", "—"))))
        else:
            st.info("No simulation stats available.")

        st.divider()

        # ---- Param Sweep Heatmap ----
        st.subheader("Parameter Sweep — Sharpe Heatmap")
        if sweep_df is None or sweep_df.empty:
            st.warning("param_sweep.csv not found or empty for this run.")
        else:
            cb_col = next(
                (c for c in ["drawdown_circuit_breaker", "circuit_breaker", "cb"] if c in sweep_df.columns),
                None,
            )
            if cb_col:
                cb_values = sorted(pd.to_numeric(sweep_df[cb_col], errors="coerce").dropna().unique().tolist())
                if cb_values:
                    tab_labels = [f"CB: {v * 100:.0f}%" for v in cb_values]
                    inner_tabs = st.tabs(tab_labels)
                    for inner_tab, cb_val in zip(inner_tabs, cb_values):
                        with inner_tab:
                            st.plotly_chart(_make_heatmap(sweep_df, cb_val), use_container_width=True)
                            sub = sweep_df[pd.to_numeric(sweep_df[cb_col], errors="coerce") == cb_val]
                            sharpe_col = next((c for c in ["sharpe", "sharpe_ratio"] if c in sub.columns), None)
                            if sharpe_col:
                                top5 = sub.nlargest(5, sharpe_col)
                                st.markdown("**Top 5 Parameter Combinations**")
                                st.dataframe(top5.reset_index(drop=True), use_container_width=True, hide_index=True)
                else:
                    st.info("No circuit breaker values found in sweep data.")
            else:
                st.plotly_chart(_make_heatmap(sweep_df, None), use_container_width=True)
                sharpe_col = next((c for c in ["sharpe", "sharpe_ratio"] if c in sweep_df.columns), None)
                if sharpe_col:
                    top5 = sweep_df.nlargest(5, sharpe_col)
                    st.markdown("**Top 5 Parameter Combinations**")
                    st.dataframe(top5.reset_index(drop=True), use_container_width=True, hide_index=True)

            # Sweep-score distribution (config#1444 item 3) — random search has no
            # convergence trajectory; the trial-score distribution + where the
            # selected combo sits is the meaningful view.
            _sharpe_col = next((c for c in ["sharpe", "sharpe_ratio"] if c in sweep_df.columns), None)
            sweepdist.render(sweep_df, _sharpe_col)

        st.divider()

        # ---- Signal Quality Summary (from backtester metrics) ----
        st.subheader("Signal Quality Summary")
        if metrics:
            sq_metrics = metrics.get("signal_quality", {})
            if sq_metrics:
                s1, s2 = st.columns(2)
                with s1:
                    st.metric("Accuracy 21d", format_pct(sq_metrics.get("accuracy_21d")))
                with s2:
                    st.metric("Avg Alpha 21d", format_pct(sq_metrics.get("avg_alpha_21d")))
        if signal_quality_df is not None and not signal_quality_df.empty:
            st.markdown("**Signal Quality Detail**")
            st.dataframe(signal_quality_df, use_container_width=True, hide_index=True)

        # ---- Per-Sector Signal Accuracy ----
        if metrics and metrics.get("report_card"):
            st.divider()
            st.subheader("System Report Card")
            rc = metrics["report_card"]
            c1, c2, c3 = st.columns(3)
            for col, key, label in [(c1, "research", "Research"), (c2, "predictor", "Predictor"), (c3, "executor", "Executor")]:
                mod = rc.get(key, {})
                g = mod.get("grade")
                letter = mod.get("letter", "N/A")
                with col:
                    st.metric(label, letter, f"{g:.0f}/100" if g is not None else None)

            # Component detail table
            rows = []
            for mod_key in ("research", "predictor", "executor"):
                mod = rc.get(mod_key, {})
                for comp_key, comp in mod.get("components", {}).items():
                    if comp_key in ("sector_teams", "sector_teams_avg"):
                        continue
                    if isinstance(comp, dict) and "grade" in comp:
                        detail = comp.get("detail", {})
                        detail_str = ", ".join(f"{k}: {v}" for k, v in detail.items() if not isinstance(v, list))
                        rows.append({
                            "Module": mod_key.title(),
                            "Component": comp_key.replace("_", " ").title(),
                            "Grade": comp.get("letter", "N/A"),
                            "Score": f"{comp['grade']:.0f}" if comp.get("grade") is not None else "—",
                            "Detail": detail_str,
                        })
            if rows:
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            # Sector team grades
            teams = rc.get("research", {}).get("components", {}).get("sector_teams", [])
            if teams:
                st.markdown("**Sector Team Grades**")
                team_rows = []
                for t in teams:
                    detail = t.get("detail", {})
                    team_rows.append({
                        "Team": t.get("team_id", "?").replace("_", " ").title(),
                        "Grade": t.get("letter", "N/A"),
                        "Score": f"{t['grade']:.0f}" if t.get("grade") is not None else "—",
                        "Precision": detail.get("precision", "—"),
                        "Recall": detail.get("recall", "—"),
                        "Lift vs Sector": detail.get("lift_vs_sector", "—"),
                        "Picks": detail.get("n_picks", "—"),
                    })
                st.dataframe(pd.DataFrame(team_rows), use_container_width=True, hide_index=True)

        st.divider()

        # ---- Promotion-Gate Significance (observe) — config#1444 item 1 ----
        bsig.render(metrics)

        st.divider()

        # ---- Sub-Score Attribution ----
        st.subheader("Sub-Score Attribution")
        if not attribution:
            st.info("attribution.json not found for this run.")
        else:
            st.plotly_chart(make_attribution_chart(attribution), use_container_width=True)

        st.divider()

        # ---- Scoring Weight Recommendations ----
        # The backtester's per-run suggested weights (real weekly output).
        # The LIVE weights channel has never promoted (config#1841) — its
        # honest status lives on the Self-Tuning tab, not as a permanent
        # not-found warning here.
        st.subheader("Scoring Weight Recommendations")

        current_weights = load_scoring_weights()

        # Recommendations (from metrics.json)
        if metrics:
            recs = metrics.get("weight_recommendations", {})
            current = metrics.get("current_weights", {})
            suggested = metrics.get("suggested_weights", recs)

            if current or suggested:
                live_weights = current_weights or {}
                rec_rows = []
                for key in ["technical", "news", "research"]:
                    curr_val = live_weights.get(key, current.get(key))
                    sugg_val = suggested.get(key) if suggested else None
                    try:
                        curr_f = (
                            float(curr_val) * 100
                            if curr_val is not None and float(curr_val) <= 1
                            else float(curr_val)
                            if curr_val is not None
                            else None
                        )
                        sugg_f = (
                            float(sugg_val) * 100
                            if sugg_val is not None and float(sugg_val) <= 1
                            else float(sugg_val)
                            if sugg_val is not None
                            else None
                        )
                        if curr_f is not None and sugg_f is not None:
                            delta = sugg_f - curr_f
                            direction = "⬆" if delta > 0.5 else ("⬇" if delta < -0.5 else "→")
                        else:
                            direction = "—"
                        rec_rows.append({
                            "Sub-Score": key.capitalize(),
                            "Current Weight": f"{curr_f:.1f}%" if curr_f is not None else "—",
                            "Suggested Weight": f"{sugg_f:.1f}%" if sugg_f is not None else "—",
                            "Direction": direction,
                        })
                    except (ValueError, TypeError) as e:
                        logger.debug("Weight formatting failed for %s: %s", key, e)
                        rec_rows.append({
                            "Sub-Score": key.capitalize(),
                            "Current Weight": "—",
                            "Suggested Weight": "—",
                            "Direction": "—",
                        })

                if rec_rows:
                    st.markdown("**Weight Recommendations**")
                    st.dataframe(pd.DataFrame(rec_rows), use_container_width=True, hide_index=True)

        st.divider()

        # ---- Raw Report ----
        with st.expander("View Full Backtest Report (report.md)", expanded=False):
            if report_md_bt:
                st.markdown(report_md_bt)
            else:
                st.info("report.md not found for this backtest run.")



# ===========================================================================
# TAB: Self-Tuning — the auto-apply channels (absorbed the former standalone
# Feedback Loop page; console-IA phase 2b, config#1988)
# ===========================================================================
with tab_tuning:
    st.subheader("Self-Tuning — governed auto-apply channels")
    st.caption(
        "Every Saturday the backtester sweeps params and MAY write winners "
        "back to S3 behind promotion gates. Honest status (config#1841): "
        "**executor_params is the only channel that has ever promoted to "
        "live S3** — scoring_weights and predictor_params have never been "
        "written, research_params has been frozen since 2026-05-02. The "
        "per-run promoted/blocked record is the apply-audit artifact "
        "(`config/apply_audit/latest.json`, first emit Sat 2026-07-11), "
        "rendered on Experiments › Crucible Results › Feedback loop."
    )

    st.divider()

    # ---- Live Optimizer Params (ROADMAP L234) ----
    # Rehomed from the retired System Health page (config#1987), now on the
    # Self-Tuning tab (config#1988): the auto-tuned params the executor
    # reads from S3 at cold-start — the ONE auto-apply channel that actually
    # promotes to live S3 (config#1841).
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
        param_rows = []
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
            param_rows.append(
                {"Param": label, "Live value": display, "Source": "S3 (auto-tuned)"}
            )

        if param_rows:
            params_df = pd.DataFrame(param_rows).sort_values("Param").reset_index(drop=True)
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
    # ---- Executor promotion history (the one LIVE channel) ----
    st.subheader("Executor promotion history")
    st.caption(
        "Source: `config/executor_params_history/{date}.json` (producer: "
        "crucible-backtester `optimizer/executor_optimizer.py`). Auto-scaled "
        "random search over the core risk params; ranked by Sharpe; promoted "
        "on holdout validation behind a guarded margin. Gaps between dates "
        "are Saturdays where the optimizer ran but the gate held."
    )
    exec_history = load_executor_params_history()
    if not exec_history:
        st.info("No executor param history yet — no successful promotion recorded.")
    else:
        hist_df = pd.DataFrame(exec_history).sort_values("updated_at").reset_index(drop=True)
        st.markdown(f"**{len(hist_df)} dated promotions** since the optimizer started writing back.")
        hist_cols = [c for c in [
            "updated_at", "min_score", "max_position_pct", "atr_multiplier",
            "time_decay_reduce_days", "time_decay_exit_days",
            "best_sharpe", "improvement_pct", "n_combos_tested",
        ] if c in hist_df.columns]
        hist_fmt = hist_df[hist_cols].copy()
        if "max_position_pct" in hist_fmt.columns:
            hist_fmt["max_position_pct"] = hist_fmt["max_position_pct"].apply(
                lambda v: f"{float(v) * 100:.1f}%" if pd.notna(v) else "—")
        if "best_sharpe" in hist_fmt.columns:
            hist_fmt["best_sharpe"] = hist_fmt["best_sharpe"].apply(
                lambda v: f"{float(v):.3f}" if pd.notna(v) else "—")
        if "improvement_pct" in hist_fmt.columns:
            hist_fmt["improvement_pct"] = hist_fmt["improvement_pct"].apply(
                lambda v: f"{float(v) * 100:+.2f}%" if pd.notna(v) else "—")
        hist_fmt.columns = [
            "Date" if c == "updated_at" else c.replace("_", " ").title()
            for c in hist_fmt.columns
        ]
        st.dataframe(hist_fmt, use_container_width=True, hide_index=True)

        if "updated_at" in hist_df.columns:
            hist_df["updated_at"] = pd.to_datetime(hist_df["updated_at"], errors="coerce")
        if "best_sharpe" in hist_df.columns and hist_df["best_sharpe"].notna().any():
            sharpe_fig = go.Figure(go.Scatter(
                x=hist_df["updated_at"],
                y=pd.to_numeric(hist_df["best_sharpe"], errors="coerce"),
                mode="lines+markers",
                line=dict(color="#7fd17f", width=2), marker=dict(size=10),
            ))
            sharpe_fig.add_hline(y=0, line_dash="dot", line_color="#888")
            sharpe_fig.update_layout(
                title="Backtester best Sharpe at each promotion", height=260,
                margin=dict(l=10, r=10, t=40, b=20),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(sharpe_fig, use_container_width=True, key="tuning_sharpe_trajectory")

    st.divider()

    # ---- The dead / frozen channels, stated honestly ----
    st.subheader("Dead / frozen channels")
    research_params = load_research_params() or {}
    if research_params:
        rc1, rc2 = st.columns([1, 2])
        rc1.metric("CIO mode (research_params)", str(research_params.get("cio_mode", "—")))
        rc2.warning(
            "`config/research_params.json` has been **frozen since 2026-05-02** "
            "(config#1841) — the value shown is stale, not a live decision. "
            f"Recorded rationale: {research_params.get('cio_mode_reason', '—')}"
        )
    else:
        st.markdown("- `config/research_params.json` — frozen since 2026-05-02 (config#1841).")
    st.markdown(
        "- `config/scoring_weights.json` — **never written** to live S3; the "
        "weekly recommendations render on the Backtester tab.\n"
        "- `config/predictor_params.json` — **never written**; the veto "
        "threshold in production is the code default."
    )

    st.divider()

    # ---- Why every weekly run isn't a promotion ----
    st.markdown(
        "**Why every weekly run isn't a promotion** — each optimizer has a "
        "promotion gate: the executor optimizer validates the winner on a "
        "held-out split before writing back; the weight optimizer requires "
        "improvement vs the prior baseline by a configurable margin. When "
        "the gate fails, the prior config stays and the run records a "
        "no-promotion outcome. The gate is the discipline that keeps the "
        "loop from shifting the system around in response to noise."
    )

# ===========================================================================
# TAB 3: Pipeline Evaluation
# ===========================================================================
with tab_eval:
    if not backtest_dates:
        st.warning("No backtest results found. Run the backtester to populate results.")
    else:
        with st.spinner(f"Loading evaluation data for {selected_backtest_date}..."):
            eval_metrics = load_backtest_file(selected_backtest_date, "metrics.json")
            report_md = load_backtest_file(selected_backtest_date, "report.md")

        # ---- Section 1: Pipeline Lift Waterfall ----
        st.subheader("1. Pipeline Lift — Decision Boundary Analysis")

        lift_section = _extract_section(report_md, "End-to-end pipeline lift") if report_md else None

        if lift_section:
            lift_steps = []
            step_names = [
                ("Scanner filter lift", "Scanner"),
                ("Team selection lift", "Teams"),
                ("CIO selection lift", "CIO"),
                ("Predictor lift", "Predictor"),
                ("Executor lift", "Executor"),
                ("Full pipeline lift", "Full Pipeline"),
            ]
            for search_term, label in step_names:
                for line in lift_section.split("\n"):
                    if search_term.lower() in line.lower() and ":" in line:
                        parts = line.split(":")
                        if len(parts) >= 2:
                            val_str = parts[-1].strip().split()[0].replace("%", "").replace("+", "")
                            val = _safe_float(val_str)
                            if val is not None:
                                if abs(val) < 1:
                                    lift_steps.append({"step": label, "lift": val})
                                else:
                                    lift_steps.append({"step": label, "lift": val / 100})
                        break

            if lift_steps:
                fig = go.Figure(go.Waterfall(
                    name="Lift",
                    orientation="v",
                    x=[s["step"] for s in lift_steps],
                    y=[s["lift"] * 100 for s in lift_steps],
                    textposition="outside",
                    text=[f"{s['lift']*100:+.2f}%" for s in lift_steps],
                    connector={"line": {"color": "rgb(63, 63, 63)"}},
                    increasing={"marker": {"color": "#2ca02c"}},
                    decreasing={"marker": {"color": "#d62728"}},
                ))
                fig.update_layout(
                    title="Pipeline Lift at Each Decision Boundary",
                    yaxis_title="Lift (percentage points)",
                    plot_bgcolor="white",
                    paper_bgcolor="white",
                    height=400,
                )
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("Lift waterfall chart will populate once lift metrics have data.")

            with st.expander("Raw Lift Report"):
                st.markdown(lift_section)
        else:
            st.info("Pipeline lift data not available for this backtest run.")

        st.divider()

        # ---- Section 2: Component Diagnostics ----
        st.subheader("2. Component Diagnostics")

        tab_triggers, tab_exits, tab_veto, tab_alpha, tab_shadow, tab_macro = st.tabs([
            "Entry Triggers", "Exit Timing", "Veto Value", "Alpha Distribution",
            "Shadow Book", "Macro A/B",
        ])

        with tab_triggers:
            section = _extract_section(report_md, "Entry trigger scorecard") if report_md else None
            if section:
                st.markdown(section)
            else:
                st.info("Entry trigger scorecard not available. Requires trades with trigger_type logged.")

        with tab_exits:
            section = _extract_section(report_md, "Exit timing analysis") if report_md else None
            if section:
                st.markdown(section)
            else:
                st.info("Exit timing analysis not available. Requires completed roundtrip trades.")

        with tab_veto:
            section = _extract_section(report_md, "Net veto value") if report_md else None
            if section:
                st.markdown(section)
            else:
                st.info("Net veto value not available. Requires predictor vetoes with resolved returns.")

        with tab_alpha:
            alpha_section = _extract_section(report_md, "Alpha magnitude distribution") if report_md else None
            cal_section = _extract_section(report_md, "Score calibration") if report_md else None
            if alpha_section:
                st.markdown(alpha_section)
            if cal_section:
                st.markdown(cal_section)
            if not alpha_section and not cal_section:
                st.info("Alpha distribution not available. Requires score_performance with resolved returns.")

        with tab_shadow:
            section = _extract_section(report_md, "Risk guard shadow book") if report_md else None
            if section:
                st.markdown(section)
            else:
                st.info("Shadow book analysis not available. Requires executor_shadow_book entries.")

        with tab_macro:
            section = _extract_section(report_md, "Macro multiplier evaluation") if report_md else None
            if section:
                st.markdown(section)
            else:
                st.info("Macro A/B evaluation not available. Requires cio_evaluations with macro shift data.")

        st.divider()

        # ---- Section 3: Self-Adjustment Status ----
        st.subheader("3. Self-Adjustment Mechanisms")

        executor_params = _fetch_s3_json(_research_bucket(), "config/executor_params.json")
        scanner_params = _fetch_s3_json(_research_bucket(), "config/scanner_params.json")
        team_slots = _fetch_s3_json(_research_bucket(), "config/team_slots.json")
        research_params = _fetch_s3_json(_research_bucket(), "config/research_params.json")

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Executor Adjustments**")

            disabled = executor_params.get("disabled_triggers", []) if executor_params else []
            if disabled:
                st.warning(f"Disabled triggers: {', '.join(disabled)}")
                updated = executor_params.get("disabled_triggers_updated_at", "—") if executor_params else "—"
                st.caption(f"Last updated: {updated}")
            else:
                st.success("All triggers active")

            p_up_enabled = executor_params.get("use_p_up_sizing", False) if executor_params else False
            if p_up_enabled:
                ic = executor_params.get("p_up_sizing_ic", "—") if executor_params else "—"
                st.success(f"p_up sizing enabled (IC={ic})")
            else:
                st.info("p_up sizing disabled — awaiting positive IC")

            sizing_section = _extract_section(report_md, "Position sizing A/B test") if report_md else None
            if sizing_section:
                with st.expander("Sizing A/B Results"):
                    st.markdown(sizing_section)

        with col2:
            st.markdown("**Research Adjustments**")

            if scanner_params:
                st.success("Scanner params active from S3")
                updated = scanner_params.get("updated_at", "—")
                st.caption(f"Last updated: {updated}")
                with st.expander("Scanner Params"):
                    display_keys = [k for k in scanner_params if k not in ("updated_at", "leakage_rate", "n_weeks")]
                    if display_keys:
                        st.json({k: scanner_params[k] for k in display_keys})
            else:
                st.info("Scanner params: using defaults (no S3 override)")

            if team_slots:
                st.success("Team slot allocation active")
                updated = team_slots.get("updated_at", "—")
                st.caption(f"Last updated: {updated}")
                slot_display = {k: v for k, v in team_slots.items() if k != "updated_at"}
                if slot_display:
                    slot_df = pd.DataFrame(
                        [{"Team": k, "Slots": v} for k, v in slot_display.items()]
                    )
                    st.dataframe(slot_df, use_container_width=True, hide_index=True)
            else:
                st.info("Team slots: using defaults (3 per team)")

            cio_mode = research_params.get("cio_mode", "llm") if research_params else "llm"
            if cio_mode == "deterministic":
                reason = research_params.get("cio_mode_reason", "") if research_params else ""
                st.warning("CIO mode: deterministic")
                if reason:
                    st.caption(reason)
            else:
                st.success("CIO mode: LLM (default)")

        phase4_section = _extract_section(report_md, "Phase 4: Self-Adjustment Mechanisms") if report_md else None
        if not phase4_section and report_md:
            for heading in ["Trigger optimizer", "Predictor p_up sizing", "Scanner filter optimizer"]:
                section = _extract_section(report_md, heading)
                if section:
                    phase4_section = section
                    break

        if phase4_section:
            with st.expander("Full Phase 4 Report", expanded=False):
                st.markdown(phase4_section)
