"""
Predictor charts — model drift / rolling accuracy trend.
"""

import pandas as pd
import plotly.graph_objects as go

from shared.constants import get_thresholds


def make_model_drift_chart(outcomes_df: pd.DataFrame) -> go.Figure:
    """
    Rolling accuracy trend: 30-day (blue thin) and 90-day (orange thick).
    Horizontal bands: green ≥ accuracy_outperform, red < model_degraded, yellow between.
    Requires ≥60 resolved predictions.
    """
    th = get_thresholds()
    baseline_pct = th["accuracy_baseline"] * 100
    outperform_pct = th["accuracy_outperform"] * 100
    degraded_pct = th["model_degraded"] * 100
    if outcomes_df is None or outcomes_df.empty:
        fig = go.Figure()
        fig.update_layout(title="Model Performance Trend — No data")
        return fig

    df = outcomes_df.copy()
    df["prediction_date"] = pd.to_datetime(df["prediction_date"])
    df = df.sort_values("prediction_date")
    df["correct_5d"] = pd.to_numeric(df["correct_5d"], errors="coerce")

    resolved = df[df["correct_5d"].notna()].copy()
    if len(resolved) < 60:
        fig = go.Figure()
        fig.update_layout(title=f"Model Performance Trend — Need ≥60 resolved predictions ({len(resolved)} available)")
        return fig

    resolved["roll_30d"] = resolved["correct_5d"].rolling(30, min_periods=15).mean() * 100
    resolved["roll_90d"] = resolved["correct_5d"].rolling(90, min_periods=30).mean() * 100

    fig = go.Figure()

    # Background bands
    fig.add_hrect(y0=outperform_pct, y1=100, fillcolor="rgba(0,200,100,0.08)", line_width=0)
    fig.add_hrect(y0=degraded_pct, y1=outperform_pct, fillcolor="rgba(255,193,7,0.08)", line_width=0)
    fig.add_hrect(y0=0, y1=degraded_pct, fillcolor="rgba(220,53,69,0.06)", line_width=0)

    # Coin-flip baseline
    fig.add_hline(
        y=baseline_pct,
        line=dict(color="gray", width=1, dash="dash"),
        annotation_text=f"{baseline_pct:.0f}%",
        annotation_position="bottom right",
    )

    # 30d rolling
    fig.add_trace(go.Scatter(
        x=resolved["prediction_date"], y=resolved["roll_30d"],
        mode="lines", name="30-day rolling",
        line=dict(color="#1f77b4", width=1.5),
        hovertemplate="<b>%{x|%Y-%m-%d}</b><br>30d: %{y:.1f}%<extra></extra>",
    ))

    # 90d rolling
    fig.add_trace(go.Scatter(
        x=resolved["prediction_date"], y=resolved["roll_90d"],
        mode="lines", name="90-day rolling",
        line=dict(color="#ff7f0e", width=2.5),
        hovertemplate="<b>%{x|%Y-%m-%d}</b><br>90d: %{y:.1f}%<extra></extra>",
    ))

    # Current 30d annotation
    current_30d = resolved["roll_30d"].dropna().iloc[-1] if not resolved["roll_30d"].dropna().empty else None
    if current_30d is not None:
        fig.add_annotation(
            x=resolved["prediction_date"].iloc[-1],
            y=current_30d,
            text=f"Current 30d: {current_30d:.1f}%",
            showarrow=True, arrowhead=2,
            font=dict(size=11, color="#1f77b4"),
        )

    fig.update_layout(
        title="Model Performance Trend (Rolling Hit Rate)",
        xaxis=dict(title="Date", showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(title="Hit Rate (%)", ticksuffix="%", range=[30, 80], showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor="white", paper_bgcolor="white",
        margin=dict(t=60, b=40, l=60, r=20),
        height=350,
    )
    return fig


def make_feature_importance_chart(fi_data: dict) -> go.Figure:
    """
    Horizontal bar chart of feature importance (SHAP values preferred, gain fallback).
    Shows top 15 features with IC overlay markers.
    """
    shap_imp = fi_data.get("shap_importance") or {}
    gain_imp = fi_data.get("gain_importance") or {}
    feature_ics = fi_data.get("feature_ics") or {}

    # Prefer SHAP, fall back to gain
    importance = shap_imp if shap_imp else gain_imp
    label = "SHAP Importance" if shap_imp else "Gain Importance"

    if not importance:
        fig = go.Figure()
        fig.update_layout(title="Feature Importance — No data available")
        return fig

    # Sort by importance, take top 15
    sorted_features = sorted(importance.items(), key=lambda x: abs(x[1]), reverse=True)[:15]
    sorted_features.reverse()  # bottom-to-top for horizontal bar

    names = [f[0] for f in sorted_features]
    values = [f[1] for f in sorted_features]
    ics = [feature_ics.get(n, 0.0) for n in names]

    fig = go.Figure()

    # Importance bars
    fig.add_trace(go.Bar(
        y=names, x=values,
        orientation="h", name=label,
        marker_color=["#2ca02c" if ic > 0 else "#d62728" for ic in ics],
        hovertemplate="<b>%{y}</b><br>Importance: %{x:.4f}<extra></extra>",
    ))

    # IC markers on secondary x-axis
    fig.add_trace(go.Scatter(
        y=names, x=ics,
        mode="markers", name="Feature IC",
        marker=dict(
            size=10, symbol="diamond",
            color=["#1f77b4" if ic > 0 else "#ff7f0e" for ic in ics],
            line=dict(width=1, color="black"),
        ),
        xaxis="x2",
        hovertemplate="<b>%{y}</b><br>IC: %{x:.4f}<extra></extra>",
    ))

    fig.update_layout(
        title=f"Feature Importance ({label}) & Predictive IC",
        xaxis=dict(title=label, side="bottom", showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        xaxis2=dict(title="Feature IC (correlation with realized forward return)", side="top",
                    overlaying="x", showgrid=False),
        plot_bgcolor="white", paper_bgcolor="white",
        height=max(350, len(names) * 28 + 80),
        margin=dict(t=60, b=40, l=180, r=80),
        legend=dict(orientation="h", yanchor="bottom", y=1.06, xanchor="right", x=1),
        bargap=0.3,
    )
    return fig
