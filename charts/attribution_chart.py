"""
Attribution and weight history charts for the Alpha Engine Dashboard.
"""

import pandas as pd
import plotly.graph_objects as go


def make_attribution_chart(attribution_data: dict) -> go.Figure:
    """
    Horizontal bar chart of sub-score correlations with the beat-SPY / return
    21d outcomes.

    X: correlation coefficient (-1 to +1)
    Y: quant, qual
    Two bars per sub-score: beat_spy_21d and return_21d correlation

    ``attribution_data`` is the raw ``analysis/attribution.py::compute_attribution``
    output (crucible-backtester), written verbatim to ``attribution.json`` and
    loaded as-is by this dashboard — a NESTED schema, not a flat one:

        {
            "status": "ok",
            "correlations": {
                "quant": {"beat_spy_21d": 0.12, "return_21d": 0.09, ...},
                "qual": {"beat_spy_21d": ..., "return_21d": ..., ...},
            },
            "ranking_21d": ["qual", "quant"],
            ...
        }

    (config#1456 / config#1481: the producer retired the 10d/30d horizons for
    a single canonical 21d target in crucible-backtester#428; this chart was
    previously reading a stale flat ``{technical,news,research}_{10d,30d}``
    shape that never matched the real producer output, so every bar silently
    defaulted to 0.0.)
    """
    if not attribution_data or attribution_data.get("status") != "ok":
        fig = go.Figure()
        fig.update_layout(title="Sub-Score Attribution — No data available")
        return fig

    correlations = attribution_data.get("correlations") or {}

    sub_scores = ["quant", "qual"]
    labels = {"quant": "Quant", "qual": "Qual"}

    corr_beat_spy = [
        (correlations.get(s) or {}).get("beat_spy_21d", 0.0) or 0.0
        for s in sub_scores
    ]
    corr_return = [
        (correlations.get(s) or {}).get("return_21d", 0.0) or 0.0
        for s in sub_scores
    ]
    y_labels = [labels[s] for s in sub_scores]

    def _bar_color(values):
        return ["#2ca02c" if v >= 0 else "#d62728" for v in values]

    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            y=y_labels,
            x=corr_beat_spy,
            name="beat_spy_21d Correlation",
            orientation="h",
            marker_color=_bar_color(corr_beat_spy),
            opacity=0.85,
            text=[f"{v:.3f}" for v in corr_beat_spy],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>beat_spy_21d Correlation: %{x:.4f}<extra></extra>",
        )
    )

    fig.add_trace(
        go.Bar(
            y=y_labels,
            x=corr_return,
            name="return_21d Correlation",
            orientation="h",
            marker_color=_bar_color(corr_return),
            opacity=0.55,
            text=[f"{v:.3f}" for v in corr_return],
            textposition="outside",
            hovertemplate="<b>%{y}</b><br>return_21d Correlation: %{x:.4f}<extra></extra>",
        )
    )

    # Zero reference line
    fig.add_vline(
        x=0,
        line=dict(color="rgba(0,0,0,0.4)", width=1.5, dash="solid"),
    )

    fig.update_layout(
        title="Sub-Score Correlation with Outperformance",
        xaxis=dict(
            title="Correlation Coefficient",
            range=[-1.05, 1.05],
            showgrid=True,
            gridcolor="rgba(0,0,0,0.07)",
            zeroline=False,
        ),
        yaxis=dict(title="Sub-Score", categoryorder="array", categoryarray=y_labels),
        barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=60, b=40, l=100, r=80),
    )

    return fig


def make_weight_history_chart(weight_history: list[dict]) -> go.Figure:
    """
    Line chart of scoring weight history over time.

    X: date (updated_at)
    Y: three lines — technical %, news %, research %
    Each dict in weight_history should have keys: updated_at, technical, news, research
    Values are expected as decimals (0.4 = 40%) or percentages (40 = 40%).
    """
    if not weight_history:
        fig = go.Figure()
        fig.update_layout(title="Weight History — No data available")
        return fig

    df = pd.DataFrame(weight_history)

    if "updated_at" not in df.columns:
        fig = go.Figure()
        fig.update_layout(title="Weight History — Missing 'updated_at' field")
        return fig

    df["updated_at"] = pd.to_datetime(df["updated_at"])
    df = df.sort_values("updated_at")

    def _to_pct(series: pd.Series) -> pd.Series:
        s = pd.to_numeric(series, errors="coerce").fillna(0.0)
        # If values look like decimals (max < 2), multiply by 100
        if s.max() <= 2.0:
            s = s * 100
        return s

    colors = {
        "technical": "#1f77b4",
        "news": "#ff7f0e",
        "research": "#2ca02c",
    }

    fig = go.Figure()

    for col, color in colors.items():
        if col not in df.columns:
            continue
        y_vals = _to_pct(df[col])
        fig.add_trace(
            go.Scatter(
                x=df["updated_at"],
                y=y_vals,
                mode="lines+markers",
                name=col.capitalize(),
                line=dict(color=color, width=2.5),
                marker=dict(size=6),
                hovertemplate=f"<b>%{{x|%Y-%m-%d}}</b><br>{col.capitalize()}: %{{y:.1f}}%<extra></extra>",
            )
        )

    # Reference line at 33% (equal weighting)
    fig.add_hline(
        y=33.33,
        line=dict(color="rgba(0,0,0,0.2)", width=1, dash="dot"),
        annotation_text="Equal weight (33%)",
        annotation_position="bottom right",
        annotation_font_size=10,
    )

    fig.update_layout(
        title="Scoring Weight History Over Time",
        xaxis=dict(title="Date", showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(
            title="Weight (%)",
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
