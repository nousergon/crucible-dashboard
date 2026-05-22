"""
Predictor page — Model health, performance trend, today's predictions, history drilldown,
signal disagreements.
"""

import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.s3_loader import load_predictions_json, load_predictor_metrics, load_production_health, load_signals_json, load_mode_history, load_feature_importance
from loaders.db_loader import get_predictor_outcomes
from loaders.signal_loader import get_available_signal_dates
from charts.predictor_chart import make_model_drift_chart, make_feature_importance_chart
from shared.constants import get_thresholds

_TH = get_thresholds()
_VETO_CONF = _TH["veto_confidence"]
_MODEL_HEALTHY = _TH["model_healthy"]
_MODEL_DEGRADED = _TH["model_degraded"]
_ACC_BASELINE = _TH["accuracy_baseline"]

st.set_page_config(page_title="Predictor — Alpha Engine", layout="wide")

st.title("Predictor")

# ---------------------------------------------------------------------------
# Model health banner
# ---------------------------------------------------------------------------

metrics = load_predictor_metrics()

if not metrics:
    st.error("No predictor metrics found. Is the predictor running?")
    st.info("Expected at `s3://alpha-engine-research/predictor/metrics/latest.json`")
    st.stop()

hit_rate = metrics.get("hit_rate_30d_rolling", 0.0) or 0.0
if hit_rate >= _MODEL_HEALTHY:
    badge = "🟢 Healthy"
elif hit_rate >= _MODEL_DEGRADED:
    badge = "🟡 Degraded"
else:
    badge = "🔴 Below Threshold"

st.subheader(f"Model Health — {badge}")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Version", metrics.get("model_version", "—"))
m2.metric("Last Trained", metrics.get("last_trained", "—"))
m3.metric("Training Samples", f"{metrics.get('training_samples', 0):,}")
m4.metric("High-Confidence Today", metrics.get("n_high_confidence", 0))

m5, m6, m7, m8 = st.columns(4)
m5.metric("Hit Rate (30d Rolling)", f"{hit_rate:.1%}")
m6.metric("IC (30d)", f"{metrics.get('ic_30d', 0):.3f}")
m7.metric("IC IR (30d)", f"{metrics.get('ic_ir_30d', 0):.3f}")
m8.metric("Predictions Today", metrics.get("n_predictions_today", 0))

st.divider()

# ---------------------------------------------------------------------------
# IC decomposition (per-L1 + L2 + L2-lift) — ROADMAP L135
# ---------------------------------------------------------------------------
#
# Reads from `predictor/metrics/production_health.json` (written weekly
# by alpha-engine-backtester `analysis/production_health.py`). Answers
# two operational questions the aggregate `IC (30d)` above can't:
#   1. Which L1 component is contributing or drifting?
#   2. Is the L2 Ridge stacker doing real work, or could ensemble
#      averaging match it?
# `l2_lift_vs_l1_mean` ≤ 0 across multiple cycles = the meta-learner
# is not adding value.

st.subheader("IC Decomposition (per-L1 + L2)")

prod_health = load_production_health()

l1_components = prod_health.get("l1_components") or {}
l2_alpha_ic = prod_health.get("l2_alpha_ic")
l2_lift = prod_health.get("l2_lift_vs_l1_mean")
n_joined = prod_health.get("l1_l2_n_joined", 0)

if not prod_health:
    st.info(
        "No `production_health.json` available yet. Surface populates "
        "after the first weekly Saturday SF Backtester run that joins "
        "predictor_outcomes with the per-date predictions artifacts."
    )
elif not l1_components and l2_alpha_ic is None:
    st.info(
        "IC decomposition not yet computed for this cycle "
        f"(`l1_l2_n_joined`={n_joined}). Likely cause: predictions/{{date}}.json "
        "artifacts missing for the lookback window, or early-cycle "
        "sample counts below the threshold."
    )
else:
    # Three L1 components side-by-side with L2 + the lift delta.
    icols = st.columns(5)
    momentum_ic = l1_components.get("momentum")
    volatility_ic = l1_components.get("volatility")
    research_ic = l1_components.get("research_calibrator")
    icols[0].metric(
        "Momentum L1 IC",
        f"{momentum_ic:.3f}" if momentum_ic is not None else "—",
    )
    icols[1].metric(
        "Volatility L1 IC",
        f"{volatility_ic:.3f}" if volatility_ic is not None else "—",
    )
    icols[2].metric(
        "Research Cal L1 IC",
        f"{research_ic:.3f}" if research_ic is not None else "—",
    )
    icols[3].metric(
        "L2 Stacker IC",
        f"{l2_alpha_ic:.3f}" if l2_alpha_ic is not None else "—",
    )
    if l2_lift is not None:
        delta_arrow = "↑" if l2_lift > 0 else ("↓" if l2_lift < 0 else "→")
        icols[4].metric(
            "L2 lift vs L1 mean",
            f"{l2_lift:+.3f}",
            help=(
                "L2_ic - mean(L1_ic). Positive = Ridge stacker is "
                "contributing alpha above ensemble averaging. Negative "
                "or near-zero across multiple cycles = meta-learner not "
                "adding value."
            ),
            delta=delta_arrow,
        )
    else:
        icols[4].metric("L2 lift vs L1 mean", "—")

    # Mini chart — single-bar visual of per-L1 + L2 ICs for quick scan.
    bar_data = []
    for label, val in (
        ("Momentum L1", momentum_ic),
        ("Volatility L1", volatility_ic),
        ("Research Cal L1", research_ic),
        ("L2 (Ridge)", l2_alpha_ic),
    ):
        if val is not None:
            bar_data.append({"Component": label, "Spearman IC": float(val)})
    if bar_data:
        bar_df = pd.DataFrame(bar_data)
        fig_bars = go.Figure(
            data=[
                go.Bar(
                    x=bar_df["Component"],
                    y=bar_df["Spearman IC"],
                    marker_color=[
                        "#60a5fa", "#60a5fa", "#60a5fa", "#22c55e",
                    ][: len(bar_df)],
                )
            ]
        )
        fig_bars.update_layout(
            template="plotly_dark",
            height=260,
            margin=dict(l=20, r=20, t=20, b=20),
            yaxis_title="Spearman IC vs canonical_actual",
            showlegend=False,
        )
        st.plotly_chart(fig_bars, use_container_width=True)

    st.caption(
        f"Source: `s3://alpha-engine-research/predictor/metrics/production_health.json`. "
        f"Joined {n_joined} predictor-outcomes rows with per-date predictions "
        "artifacts; lookback "
        f"{prod_health.get('lookback_days', '?')}d. "
        f"Last computed for {prod_health.get('date', '?')}. ROADMAP L135."
    )

st.divider()

# ---------------------------------------------------------------------------
# Model Performance Trend (Gap #5)
# ---------------------------------------------------------------------------

st.subheader("Model Performance Trend")

outcomes_df = get_predictor_outcomes()

if outcomes_df.empty:
    st.info("No prediction history available yet.")
else:
    resolved = outcomes_df[outcomes_df["correct_5d"].notna()].copy()
    if len(resolved) < 60:
        st.info(
            f"Model drift chart requires ≥60 resolved predictions "
            f"(currently {len(resolved)}). Check back after the predictor has been running longer."
        )
    else:
        drift_fig = make_model_drift_chart(resolved)
        st.plotly_chart(drift_fig, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# Model Mode History (O1)
# ---------------------------------------------------------------------------

st.subheader("Model Mode History")

mode_history = load_mode_history()

if len(mode_history) < 2:
    st.info(
        f"Mode history requires at least 2 weekly training runs "
        f"(currently {len(mode_history)}). Check back after more training cycles."
    )
else:
    mh_df = pd.DataFrame(mode_history)
    mh_df["date"] = pd.to_datetime(mh_df["date"])

    mode_fig = go.Figure()
    mode_fig.add_trace(go.Scatter(
        x=mh_df["date"], y=mh_df["mse_ic"],
        mode="lines+markers", name="MSE IC",
        line=dict(color="#1f77b4", width=2),
        marker=dict(size=8),
    ))
    if "rank_ic" in mh_df.columns and mh_df["rank_ic"].notna().any():
        mode_fig.add_trace(go.Scatter(
            x=mh_df["date"], y=mh_df["rank_ic"],
            mode="lines+markers", name="Lambdarank IC",
            line=dict(color="#ff7f0e", width=2),
            marker=dict(size=8),
        ))
    if "ensemble_ic" in mh_df.columns and mh_df["ensemble_ic"].notna().any():
        mode_fig.add_trace(go.Scatter(
            x=mh_df["date"], y=mh_df["ensemble_ic"],
            mode="lines+markers", name="Ensemble IC",
            line=dict(color="#2ca02c", width=2),
            marker=dict(size=8),
        ))

    # Highlight selected mode each week with a star marker
    for _, row in mh_df.iterrows():
        ic_val = row.get(f"{row['best_mode']}_ic") if row["best_mode"] != "ensemble" else row.get("ensemble_ic")
        if ic_val is not None and pd.notna(ic_val):
            mode_fig.add_trace(go.Scatter(
                x=[row["date"]], y=[ic_val],
                mode="markers", showlegend=False,
                marker=dict(symbol="star", size=14, color="gold", line=dict(width=1, color="black")),
                hovertemplate=f"<b>Selected: {row['best_mode']}</b><br>IC: {ic_val:.4f}<extra></extra>",
            ))

    mode_fig.update_layout(
        title="Weekly IC by Model Type (star = selected mode)",
        xaxis_title="Training Date", yaxis_title="Test IC",
        plot_bgcolor="white", paper_bgcolor="white",
        height=350, margin=dict(t=40, b=30, l=60, r=20),
    )
    st.plotly_chart(mode_fig, use_container_width=True)

    # Summary metrics
    from collections import Counter
    wins = Counter(mh_df["best_mode"])
    cols = st.columns(len(wins))
    for i, (mode, count) in enumerate(wins.most_common()):
        pct = count / len(mh_df) * 100
        cols[i].metric(f"{mode.title()} Wins", f"{count} ({pct:.0f}%)")

st.divider()

# ---------------------------------------------------------------------------
# Feature Importance
# ---------------------------------------------------------------------------

st.subheader("Feature Importance")

fi_data = load_feature_importance()

if not fi_data:
    st.info(
        "Feature importance data not available yet. "
        "It is written after each weekly GBM training run."
    )
else:
    fi_fig = make_feature_importance_chart(fi_data)
    st.plotly_chart(fi_fig, use_container_width=True)

    # Metadata row
    fi_cols = st.columns(4)
    fi_cols[0].metric("Training Date", fi_data.get("date", "—"))
    fi_cols[1].metric("Model Version", fi_data.get("model_version", "—"))
    fi_cols[2].metric("Promoted", "Yes" if fi_data.get("promoted") else "No")
    n_noise = len(fi_data.get("noise_candidates", []) or [])
    fi_cols[3].metric("Noise Candidates", n_noise)

    # Noise candidates expander
    noise = fi_data.get("noise_candidates", [])
    if noise:
        with st.expander(f"Noise feature candidates ({len(noise)})"):
            st.caption("Features with SHAP < 1% of max AND |IC| < 0.005 — candidates for removal.")
            st.write(", ".join(noise))

st.divider()

# ---------------------------------------------------------------------------
# Today's predictions table
# ---------------------------------------------------------------------------

st.subheader("Today's Predictions")

today_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
predictions = load_predictions_json()
signals_data = load_signals_json(today_str) if get_available_signal_dates() else None

if not predictions:
    st.info("No predictions available for today. Run the predictor to populate.")
else:
    show_all = st.toggle("Show all predictions (including low confidence)", value=False)

    universe = {}
    if signals_data:
        universe = {t["ticker"]: t for t in signals_data.get("universe", [])}

    rows = []
    for ticker, pred in predictions.items():
        conf = pred.get("prediction_confidence") or 0.0
        if not show_all and conf < _VETO_CONF:
            continue
        direction = pred.get("predicted_direction", "—")
        arrow = {"UP": "↑", "DOWN": "↓", "FLAT": "→"}.get(direction, "")
        p_up = pred.get("p_up") or 0.0
        p_down = pred.get("p_down") or 0.0
        modifier = (p_up - p_down) * 10.0 * conf if conf >= _VETO_CONF else 0.0
        sig = universe.get(ticker, {})
        rows.append({
            "Ticker": ticker,
            "Direction": f"{direction} {arrow}",
            "Confidence": conf,
            "P(UP)": p_up,
            "P(FLAT)": pred.get("p_flat") or 0.0,
            "P(DOWN)": p_down,
            "Score Modifier": f"+{modifier:.1f}" if modifier > 0 else (f"{modifier:.1f}" if modifier != 0 else "—"),
            "Signal": sig.get("signal", "—"),
            "Score": sig.get("score", "—"),
        })

    if rows:
        df = pd.DataFrame(rows).sort_values("Confidence", ascending=False).reset_index(drop=True)

        def _row_color(row):
            d = str(row.get("Direction", ""))
            if "↑" in d:
                return ["background-color: #d4edda"] * len(row)
            elif "↓" in d:
                return ["background-color: #f8d7da"] * len(row)
            return [""] * len(row)

        styled = df.style.apply(_row_color, axis=1)
        for col in ["Confidence", "P(UP)", "P(FLAT)", "P(DOWN)"]:
            styled = styled.format({col: "{:.0%}"}, na_rep="—")
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.info("No high-confidence predictions today. Toggle to show all.")

st.divider()

# ---------------------------------------------------------------------------
# Ticker drilldown
# ---------------------------------------------------------------------------

st.subheader("Prediction History — Ticker Drilldown")

if outcomes_df.empty:
    st.info("No prediction history available yet.")
else:
    tickers = sorted(outcomes_df["symbol"].dropna().unique().tolist())
    selected = st.selectbox("Select ticker", options=tickers)

    ticker_df = outcomes_df[outcomes_df["symbol"] == selected].copy()
    ticker_df = ticker_df.sort_values("prediction_date")

    if not ticker_df.empty:
        p_up_col = pd.to_numeric(ticker_df["p_up"], errors="coerce").fillna(0)
        p_down_col = pd.to_numeric(ticker_df["p_down"], errors="coerce").fillna(0)
        ticker_df["net_signal"] = p_up_col - p_down_col

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=ticker_df["prediction_date"], y=ticker_df["net_signal"],
            mode="lines", name="Net Signal",
            line=dict(color="#1f77b4", width=2),
            hovertemplate="<b>%{x}</b><br>Net: %{y:.2f}<extra></extra>",
        ))
        fig.add_hline(y=0, line_dash="dash", line_color="gray")

        resolved = ticker_df[ticker_df["correct_5d"].notna()]
        correct = resolved[resolved["correct_5d"] == 1]
        wrong = resolved[resolved["correct_5d"] == 0]

        if not correct.empty:
            fig.add_trace(go.Scatter(
                x=correct["prediction_date"], y=correct["net_signal"],
                mode="markers", name="Correct ✅",
                marker=dict(symbol="circle", color="green", size=10),
            ))
        if not wrong.empty:
            fig.add_trace(go.Scatter(
                x=wrong["prediction_date"], y=wrong["net_signal"],
                mode="markers", name="Wrong ❌",
                marker=dict(symbol="x", color="red", size=10),
            ))

        fig.update_layout(
            title=f"{selected} — Net Directional Signal (p_up − p_down)",
            xaxis_title="Date", yaxis_title="Net Signal",
            yaxis=dict(range=[-1.1, 1.1]),
            plot_bgcolor="white", paper_bgcolor="white",
            height=350, margin=dict(t=40, b=30, l=60, r=20),
        )
        st.plotly_chart(fig, use_container_width=True)

        total = len(resolved)
        n_correct = int(resolved["correct_5d"].sum()) if total > 0 else 0
        acc = n_correct / total if total > 0 else 0
        st.caption(f"Running accuracy: **{n_correct} correct of {total} predictions ({acc:.1%})**")

st.divider()

# ---------------------------------------------------------------------------
# Hit rate by confidence bucket (moved from former Signal Quality page)
# ---------------------------------------------------------------------------

st.subheader("Hit Rate by Confidence Bucket")
st.caption("Validates that confidence is monotonically predictive. Non-monotonic = calibration issue.")

if outcomes_df.empty:
    st.info("No predictor outcome data available yet.")
else:
    resolved_bucket = outcomes_df[outcomes_df["correct_5d"].notna()].copy()
    if len(resolved_bucket) < 20:
        st.info(
            f"Requires ≥20 resolved predictions (currently {len(resolved_bucket)}). "
            "The more fine-grained Confidence Calibration chart below needs ≥100."
        )
    else:
        bins = [0.65, 0.75, 0.85, 1.01]
        labels = ["0.65–0.75", "0.75–0.85", "0.85–1.0"]
        resolved_bucket["conf_bucket"] = pd.cut(
            pd.to_numeric(resolved_bucket["prediction_confidence"], errors="coerce"),
            bins=bins,
            labels=labels,
            right=False,
        )
        bucket_stats = (
            resolved_bucket.groupby("conf_bucket", observed=True)["correct_5d"]
            .agg(["mean", "count"])
            .reset_index()
        )
        if not bucket_stats.empty:
            bucket_fig = go.Figure(go.Bar(
                x=bucket_stats["conf_bucket"].astype(str),
                y=bucket_stats["mean"],
                text=[f"{v:.0%} (n={n})" for v, n in zip(bucket_stats["mean"], bucket_stats["count"])],
                textposition="outside",
                marker_color="#2ca02c",
            ))
            bucket_fig.add_hline(y=_ACC_BASELINE, line_dash="dash", line_color="gray")
            bucket_fig.update_layout(
                title="Hit Rate by Confidence Bucket",
                xaxis_title="Confidence Bucket",
                yaxis_title="Hit Rate",
                yaxis=dict(tickformat=".0%", range=[0, 1]),
                plot_bgcolor="white",
                paper_bgcolor="white",
                height=300,
                margin=dict(t=40, b=30, l=60, r=20),
            )
            st.plotly_chart(bucket_fig, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# Confidence calibration chart
# ---------------------------------------------------------------------------

st.subheader("Confidence Calibration")

if not outcomes_df.empty:
    resolved_all = outcomes_df[outcomes_df["correct_5d"].notna()].copy()
    resolved_all["prediction_confidence"] = pd.to_numeric(
        resolved_all["prediction_confidence"], errors="coerce"
    )
    if len(resolved_all) < 100:
        st.info(
            f"Confidence calibration requires ≥100 resolved predictions "
            f"(currently {len(resolved_all)}). A well-calibrated model produces a near-diagonal line."
        )
    else:
        resolved_all["conf_decile"] = pd.qcut(
            resolved_all["prediction_confidence"], q=10, duplicates="drop"
        )
        cal = resolved_all.groupby("conf_decile", observed=True).agg(
            avg_conf=("prediction_confidence", "mean"),
            hit_rate=("correct_5d", "mean"),
            n=("correct_5d", "count"),
        ).reset_index()

        cal_fig = go.Figure()
        cal_fig.add_trace(go.Scatter(
            x=cal["avg_conf"], y=cal["hit_rate"],
            mode="markers+lines", name="Actual",
            marker=dict(size=cal["n"] / cal["n"].max() * 20 + 6, color="#1f77b4"),
            hovertemplate="Conf: %{x:.2f}<br>Hit: %{y:.0%}<extra></extra>",
        ))
        cal_fig.add_trace(go.Scatter(
            x=[0, 1], y=[0, 1],
            mode="lines", name="Perfect calibration",
            line=dict(dash="dash", color="gray"),
        ))
        cal_fig.update_layout(
            title="Confidence Calibration (diagonal = well-calibrated)",
            xaxis=dict(title="Avg Confidence in Decile", tickformat=".0%"),
            yaxis=dict(title="Actual Hit Rate", tickformat=".0%"),
            plot_bgcolor="white", paper_bgcolor="white",
            height=350, margin=dict(t=40, b=40, l=60, r=20),
        )
        st.plotly_chart(cal_fig, use_container_width=True)
else:
    st.info("No prediction history available for calibration chart.")

st.divider()

# ---------------------------------------------------------------------------
# Signal disagreements
# ---------------------------------------------------------------------------

st.subheader("Prediction vs. Signal Disagreements")
st.caption("Tickers where predictor direction conflicts with composite score signal (high tension)")

if predictions and signals_data:
    universe_list = signals_data.get("universe", [])
    disagreements = []
    for ticker_data in universe_list:
        ticker = ticker_data.get("ticker", "")
        pred = predictions.get(ticker, {})
        if not pred:
            continue
        conf = pred.get("prediction_confidence") or 0.0
        if conf < _VETO_CONF:
            continue
        direction = pred.get("predicted_direction", "")
        signal = ticker_data.get("signal", "")

        is_disagreement = (
            (signal == "ENTER" and direction == "DOWN") or
            (signal == "EXIT" and direction == "UP")
        )
        if is_disagreement:
            disagreements.append({
                "Ticker": ticker,
                "Signal": signal,
                "Score": ticker_data.get("score", "—"),
                "Predicted Direction": direction,
                "Confidence": conf,
            })

    if disagreements:
        dis_df = pd.DataFrame(disagreements)
        dis_df = dis_df.sort_values("Confidence", ascending=False).reset_index(drop=True)
        styled_dis = dis_df.style.format({"Confidence": "{:.0%}"}, na_rep="—")
        st.dataframe(styled_dis, use_container_width=True, hide_index=True)
        st.caption("These are the highest-tension cases for manual review before acting on a signal.")
    else:
        st.success("No signal/prediction disagreements today with high-confidence predictions.")
else:
    st.info("Load both signals and predictions to see disagreements.")
