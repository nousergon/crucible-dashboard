"""
Signal accuracy charts for the Alpha Engine Dashboard.
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from loaders.outcome_store import BEAT_SPY_PRIMARY, RETURN_PRIMARY, SPY_RETURN_PRIMARY
from shared.accuracy_metrics import wilson_ci as _wilson_ci
from shared.constants import get_thresholds


def _threshold_pcts() -> tuple[float, float]:
    """Return (baseline_pct, outperform_pct) for accuracy charts.

    Called lazily inside each chart function — not at module load — so chart
    tests that mock streamlit aren't forced to import loaders.s3_loader first.
    """
    th = get_thresholds()
    return th["accuracy_baseline"] * 100, th["accuracy_outperform"] * 100


def make_accuracy_trend_chart(perf_df: pd.DataFrame) -> go.Figure:
    """
    Rolling 4-week (~20 trading day) accuracy trend line.
    Shows accuracy_21d over time (canonical horizon).
    Dashed 50% reference line. Shaded band at 55%+ (outperformance zone).

    perf_df needs: score_date, BEAT_SPY_PRIMARY (bool)
    """
    if perf_df is None or perf_df.empty:
        fig = go.Figure()
        fig.update_layout(title="Accuracy Trend — No data available")
        return fig

    df = perf_df.copy()
    df["score_date"] = pd.to_datetime(df["score_date"])
    df = df.sort_values("score_date")

    # Convert bool columns to numeric (1/0) for rolling mean
    for col in [BEAT_SPY_PRIMARY]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Rolling 20-row window (approx 4 calendar weeks of trading days)
    window = 20
    df["acc_21d"] = df[BEAT_SPY_PRIMARY].rolling(window, min_periods=5).mean() * 100

    baseline_pct, outperform_pct = _threshold_pcts()
    fig = go.Figure()

    # Shaded outperformance band
    fig.add_hrect(
        y0=outperform_pct,
        y1=100,
        fillcolor="rgba(0,200,100,0.08)",
        line_width=0,
        annotation_text=f"{outperform_pct:.0f}%+ zone",
        annotation_position="top right",
        annotation_font_size=11,
        annotation_font_color="#2ca02c",
    )

    # Baseline (coin-flip) reference line
    fig.add_hline(
        y=baseline_pct,
        line=dict(color="rgba(0,0,0,0.4)", width=1.5, dash="dash"),
        annotation_text=f"{baseline_pct:.0f}% (coin flip)",
        annotation_position="bottom right",
        annotation_font_size=10,
    )

    # 21d accuracy line (canonical horizon)
    fig.add_trace(
        go.Scatter(
            x=df["score_date"],
            y=df["acc_21d"],
            mode="lines",
            name="21d Accuracy (4-wk rolling)",
            line=dict(color="#1f77b4", width=2.5),
            hovertemplate="<b>%{x|%Y-%m-%d}</b><br>21d Accuracy: %{y:.1f}%<extra></extra>",
        )
    )

    fig.update_layout(
        title="Signal Accuracy Trend (Rolling 4-Week Window)",
        xaxis=dict(title="Date", showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(
            title="Accuracy (%)",
            ticksuffix="%",
            range=[0, 100],
            showgrid=True,
            gridcolor="rgba(0,0,0,0.07)",
        ),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=60, b=40, l=60, r=20),
    )
    return fig


SCORE_BUCKET_BINS = [60, 70, 80, 90, 101]
SCORE_BUCKET_LABELS = ["60-70", "70-80", "80-90", "90+"]


def prepare_bucket_data(perf_df: pd.DataFrame) -> pd.DataFrame | None:
    """Aggregate accuracy metrics by score bucket with Wilson CIs.

    Returns a DataFrame with columns: bucket, acc_21d, count,
    ci_21d_lower, ci_21d_upper.
    Returns None if the input is empty or missing required columns.
    """
    if perf_df is None or perf_df.empty:
        return None

    df = perf_df.copy()
    score_col = "composite_score" if "composite_score" in df.columns else "score"
    if score_col not in df.columns:
        return None

    df[score_col] = pd.to_numeric(df[score_col], errors="coerce")
    for col in [BEAT_SPY_PRIMARY]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["bucket"] = pd.cut(df[score_col], bins=SCORE_BUCKET_BINS, labels=SCORE_BUCKET_LABELS, right=False)

    grouped = df.groupby("bucket", observed=True).agg(
        acc_21d=(BEAT_SPY_PRIMARY, "mean"),
        sum_21d=(BEAT_SPY_PRIMARY, "sum"),
        count=(score_col, "count"),
    ).reset_index()

    grouped["acc_21d"] = grouped["acc_21d"] * 100

    ci_21d_lower, ci_21d_upper = [], []
    for _, row in grouped.iterrows():
        n = int(row["count"])
        lo21, hi21 = _wilson_ci(int(row["sum_21d"]), n)
        ci_21d_lower.append(row["acc_21d"] - lo21 * 100)
        ci_21d_upper.append(hi21 * 100 - row["acc_21d"])

    grouped["ci_21d_lower"] = ci_21d_lower
    grouped["ci_21d_upper"] = ci_21d_upper

    return grouped


def make_accuracy_by_bucket_chart(perf_df: pd.DataFrame) -> go.Figure:
    """Grouped bar chart: accuracy by score bucket (60-70, 70-80, 80-90, 90+).

    One bar per bucket: accuracy_21d (canonical horizon).
    Includes Wilson CI error bars and sample size annotations.
    """
    grouped = prepare_bucket_data(perf_df)
    if grouped is None or grouped.empty:
        fig = go.Figure()
        fig.update_layout(title="Accuracy by Score Bucket — No data available")
        return fig

    baseline_pct, _ = _threshold_pcts()
    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            x=grouped["bucket"].astype(str),
            y=grouped["acc_21d"],
            name="21d Accuracy",
            marker_color="#1f77b4",
            text=grouped["acc_21d"].round(1).astype(str) + "%",
            textposition="outside",
            hovertemplate="Bucket: %{x}<br>21d Accuracy: %{y:.1f}%<extra></extra>",
            error_y=dict(type="data", symmetric=False, array=grouped["ci_21d_upper"].tolist(), arrayminus=grouped["ci_21d_lower"].tolist()),
        )
    )

    fig.add_hline(
        y=baseline_pct,
        line=dict(color="rgba(0,0,0,0.4)", width=1.5, dash="dash"),
        annotation_text=f"{baseline_pct:.0f}%",
        annotation_position="top right",
    )

    for _, row in grouped.iterrows():
        fig.add_annotation(
            x=str(row["bucket"]),
            y=-5,
            text=f"(n={int(row['count'])})",
            showarrow=False,
            font=dict(size=10, color="gray"),
        )

    fig.update_layout(
        title="Signal Accuracy by Score Bucket",
        xaxis=dict(title="Score Bucket", categoryorder="array", categoryarray=SCORE_BUCKET_LABELS),
        yaxis=dict(
            title="Accuracy (%)",
            ticksuffix="%",
            range=[-10, 110],
            showgrid=True,
            gridcolor="rgba(0,0,0,0.07)",
        ),
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=60, b=40, l=60, r=20),
    )
    return fig


def make_accuracy_by_regime_chart(perf_df: pd.DataFrame, macro_df: pd.DataFrame) -> go.Figure:
    """
    Grouped bar chart: accuracy by market regime (bull, neutral, bear, caution).
    Joins perf_df to macro_df on date. One bar per regime: 21d (canonical horizon).

    perf_df needs: score_date, BEAT_SPY_PRIMARY
    macro_df needs: date, regime
    """
    if perf_df is None or perf_df.empty or macro_df is None or macro_df.empty:
        fig = go.Figure()
        fig.update_layout(title="Accuracy by Regime — No data available")
        return fig

    perf = perf_df.copy()
    macro = macro_df.copy()

    perf["score_date"] = pd.to_datetime(perf["score_date"]).dt.date.astype(str)
    macro["date"] = pd.to_datetime(macro["date"]).dt.date.astype(str)

    # Support both "regime" and "market_regime" column names
    regime_col = "regime" if "regime" in macro.columns else "market_regime" if "market_regime" in macro.columns else None
    if regime_col is None:
        fig = go.Figure()
        fig.update_layout(title="Accuracy by Regime — No regime column in macro data")
        return fig

    merged = perf.merge(macro[["date", regime_col]], left_on="score_date", right_on="date", how="left")
    merged = merged.rename(columns={regime_col: "regime"})
    # A column collision (perf already carrying a 'regime'/'market_regime') makes
    # pandas suffix the merged column ('regime_x'/'regime_y'), leaving no plain
    # 'regime'. Coalesce from the suffixed variant, else degrade gracefully
    # rather than KeyError (pre-existing crash surfaced 2026-06-04).
    if "regime" not in merged.columns:
        suffixed = next((c for c in ("regime_y", "regime_x") if c in merged.columns), None)
        if suffixed is None:
            fig = go.Figure()
            fig.update_layout(title="Accuracy by Regime — regime column unavailable after merge")
            return fig
        merged = merged.rename(columns={suffixed: "regime"})
    merged["regime"] = merged["regime"].fillna("unknown")

    for col in [BEAT_SPY_PRIMARY]:
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")

    grouped = (
        merged.groupby("regime")
        .agg(acc_21d=(BEAT_SPY_PRIMARY, "mean"), count=(BEAT_SPY_PRIMARY, "count"))
        .reset_index()
    )
    grouped["acc_21d"] = grouped["acc_21d"] * 100

    # 3-class Ang-Bekaert macro taxonomy (v0.42.0 / 2026-05-28 —
    # caution-regime-retirement-260528.md) + grandfather for historical
    # rows carrying legacy "caution". "unknown" for null/missing.
    regime_order = ["bull", "neutral", "bear", "caution", "unknown"]
    grouped["regime"] = pd.Categorical(grouped["regime"], categories=regime_order, ordered=True)
    grouped = grouped.sort_values("regime")

    baseline_pct, _ = _threshold_pcts()
    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            x=grouped["regime"].astype(str),
            y=grouped["acc_21d"],
            name="21d Accuracy",
            marker_color="#1f77b4",
            text=grouped["acc_21d"].round(1).astype(str) + "%",
            textposition="outside",
            hovertemplate="Regime: %{x}<br>21d Accuracy: %{y:.1f}%<extra></extra>",
        )
    )

    fig.add_hline(
        y=baseline_pct,
        line=dict(color="rgba(0,0,0,0.4)", width=1.5, dash="dash"),
        annotation_text=f"{baseline_pct:.0f}%",
        annotation_position="top right",
    )

    fig.update_layout(
        title="Signal Accuracy by Market Regime",
        xaxis=dict(title="Market Regime"),
        yaxis=dict(
            title="Accuracy (%)",
            ticksuffix="%",
            range=[0, 110],
            showgrid=True,
            gridcolor="rgba(0,0,0,0.07)",
        ),
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=60, b=40, l=60, r=20),
    )
    return fig


def make_alpha_distribution_chart(perf_df: pd.DataFrame) -> go.Figure:
    """
    Histogram of per-signal alpha (RETURN_PRIMARY - SPY_RETURN_PRIMARY).
    Two panels: score >= 70 and all signals. Mean and median vertical lines.

    perf_df needs: composite_score (or score), RETURN_PRIMARY, SPY_RETURN_PRIMARY
    """
    if perf_df is None or perf_df.empty:
        fig = go.Figure()
        fig.update_layout(title="Alpha Distribution — No data available")
        return fig

    df = perf_df.copy()
    score_col = "composite_score" if "composite_score" in df.columns else "score"

    for col in [score_col, RETURN_PRIMARY, SPY_RETURN_PRIMARY]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["alpha_21d"] = df[RETURN_PRIMARY] - df[SPY_RETURN_PRIMARY]
    df = df.dropna(subset=["alpha_21d"])

    all_alpha = df["alpha_21d"] * 100
    if score_col in df.columns:
        high_score_alpha = df.loc[df[score_col] >= 70, "alpha_21d"] * 100
    else:
        high_score_alpha = all_alpha

    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=["All Signals", "Score ≥ 70"],
        shared_yaxes=False,
    )

    def _add_histogram(data: pd.Series, row: int, col: int, name: str, color: str):
        if data.empty:
            return
        fig.add_trace(
            go.Histogram(
                x=data,
                name=name,
                marker_color=color,
                opacity=0.75,
                nbinsx=30,
                hovertemplate="Alpha: %{x:.1f}%<br>Count: %{y}<extra></extra>",
            ),
            row=row,
            col=col,
        )
        mean_val = data.mean()
        median_val = data.median()
        # Mean line
        fig.add_vline(
            x=mean_val,
            line=dict(color="red", width=2, dash="dash"),
            annotation_text=f"Mean: {mean_val:.1f}%",
            annotation_position="top right",
            row=row,
            col=col,
        )
        # Median line
        fig.add_vline(
            x=median_val,
            line=dict(color="blue", width=2, dash="dot"),
            annotation_text=f"Median: {median_val:.1f}%",
            annotation_position="top left",
            row=row,
            col=col,
        )

    _add_histogram(all_alpha, 1, 1, "All Signals", "#7f7f7f")
    _add_histogram(high_score_alpha, 1, 2, "Score ≥ 70", "#1f77b4")

    fig.update_layout(
        title="Alpha Distribution (21d Return vs SPY)",
        showlegend=False,
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=80, b=40, l=60, r=20),
    )
    fig.update_xaxes(title_text="21d Alpha (%)", ticksuffix="%")
    fig.update_yaxes(title_text="Count")

    return fig


def make_regime_alpha_chart(eod_df: pd.DataFrame, macro_df: pd.DataFrame) -> go.Figure:
    """
    Grouped bar chart: average daily alpha by market regime.
    Merges eod_pnl with macro on date.

    eod_df needs: date, daily_alpha_pct
    macro_df needs: date, regime
    """
    if eod_df is None or eod_df.empty or macro_df is None or macro_df.empty:
        fig = go.Figure()
        fig.update_layout(title="Alpha by Regime — No data available")
        return fig

    eod = eod_df.copy()
    macro = macro_df.copy()

    eod["date"] = pd.to_datetime(eod["date"]).dt.date.astype(str)
    macro["date"] = pd.to_datetime(macro["date"]).dt.date.astype(str)

    from shared.normalizers import to_decimal_series
    eod["daily_alpha_pct"] = to_decimal_series(eod["daily_alpha_pct"])

    regime_col = "regime" if "regime" in macro.columns else "market_regime" if "market_regime" in macro.columns else None
    if regime_col is None:
        fig = go.Figure()
        fig.update_layout(title="Alpha by Regime — No regime column")
        return fig

    merged = eod.merge(macro[["date", regime_col]], on="date", how="left")
    merged = merged.rename(columns={regime_col: "regime"})
    merged["regime"] = merged["regime"].fillna("unknown")

    grouped = merged.groupby("regime").agg(
        avg_alpha=("daily_alpha_pct", "mean"),
        total_alpha=("daily_alpha_pct", "sum"),
        days=("daily_alpha_pct", "count"),
    ).reset_index()

    # 3-class Ang-Bekaert macro taxonomy (v0.42.0 / 2026-05-28 —
    # caution-regime-retirement-260528.md) + grandfather for historical
    # rows carrying legacy "caution". "unknown" for null/missing.
    regime_order = ["bull", "neutral", "bear", "caution", "unknown"]
    grouped["regime"] = pd.Categorical(grouped["regime"], categories=regime_order, ordered=True)
    grouped = grouped.sort_values("regime")

    # Color bars by sign
    colors = ["#28a745" if v >= 0 else "#dc3545" for v in grouped["avg_alpha"]]

    fig = go.Figure(
        go.Bar(
            x=grouped["regime"].astype(str),
            y=grouped["avg_alpha"] * 100,
            marker_color=colors,
            text=[f"{v*100:+.2f}%<br>({d}d)" for v, d in zip(grouped["avg_alpha"], grouped["days"])],
            textposition="outside",
            hovertemplate="Regime: %{x}<br>Avg Daily Alpha: %{y:.3f}%<br><extra></extra>",
        )
    )

    fig.add_hline(y=0, line=dict(color="gray", width=1, dash="dash"))

    fig.update_layout(
        title="Average Daily Alpha by Market Regime",
        xaxis=dict(title="Market Regime"),
        yaxis=dict(title="Avg Daily Alpha (%)", ticksuffix="%", showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=60, b=40, l=60, r=20),
    )
    return fig
