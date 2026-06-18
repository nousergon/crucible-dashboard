"""
NAV vs SPY cumulative return chart for the Alpha Engine Dashboard.
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from shared.normalizers import to_decimal_series


def make_nav_chart(eod_df: pd.DataFrame) -> go.Figure:
    """
    NAV vs SPY cumulative return chart.

    eod_df needs columns:
        date, portfolio_nav, daily_return_pct, spy_return_pct, daily_alpha_pct

    Returns a Plotly Figure with:
    - Portfolio cumulative return line (blue)
    - SPY cumulative return line (gray)
    - Shaded region between them (green where portfolio > SPY, red otherwise)
    - Hover showing date, portfolio %, SPY %, alpha %
    """
    if eod_df is None or eod_df.empty:
        fig = go.Figure()
        fig.update_layout(title="NAV vs SPY — No data available")
        return fig

    df = eod_df.copy()

    # Ensure date column is datetime
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    port_ret = to_decimal_series(df["daily_return_pct"])
    spy_ret = to_decimal_series(df["spy_return_pct"])
    alpha = to_decimal_series(df["daily_alpha_pct"])

    port_cum = ((1 + port_ret).cumprod() - 1) * 100  # percent
    spy_cum = ((1 + spy_ret).cumprod() - 1) * 100     # percent
    alpha_cum = port_cum - spy_cum

    dates = df["date"]

    # ---------- Shaded region ----------
    # Build fill traces: green where portfolio > SPY, red where below
    # We create two filled traces by masking
    above_mask = port_cum >= spy_cum

    # Helper to build segment traces (fill between two lines)
    def _fill_segment(dates_seg, upper, lower, color, name):
        return go.Scatter(
            x=pd.concat([dates_seg, dates_seg[::-1]]),
            y=pd.concat([upper, lower[::-1]]),
            fill="toself",
            fillcolor=color,
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
            name=name,
        )

    traces = []

    # Identify contiguous segments
    segments = []
    current_above = above_mask.iloc[0]
    seg_start = 0
    for i in range(1, len(above_mask)):
        if above_mask.iloc[i] != current_above:
            segments.append((seg_start, i, current_above))
            seg_start = i
            current_above = above_mask.iloc[i]
    segments.append((seg_start, len(above_mask), current_above))

    for seg_start, seg_end, is_above in segments:
        idx = range(seg_start, seg_end)
        d_seg = dates.iloc[idx]
        p_seg = port_cum.iloc[idx]
        s_seg = spy_cum.iloc[idx]
        upper = p_seg if is_above else s_seg
        lower = s_seg if is_above else p_seg
        color = "rgba(0,200,100,0.15)" if is_above else "rgba(220,50,50,0.15)"
        name = "Outperformance" if is_above else "Underperformance"
        traces.append(_fill_segment(d_seg, upper, lower, color, name))

    # ---------- Main lines ----------
    hover_text = [
        f"<b>{d.strftime('%Y-%m-%d')}</b><br>"
        f"Portfolio: {p:+.2f}%<br>"
        f"SPY: {s:+.2f}%<br>"
        f"Alpha: {a:+.2f}%"
        for d, p, s, a in zip(dates, port_cum, spy_cum, alpha_cum)
    ]

    traces.append(
        go.Scatter(
            x=dates,
            y=port_cum,
            mode="lines",
            name="Portfolio",
            line=dict(color="#1f77b4", width=2.5),
            hovertext=hover_text,
            hoverinfo="text",
        )
    )

    traces.append(
        go.Scatter(
            x=dates,
            y=spy_cum,
            mode="lines",
            name="SPY",
            line=dict(color="#7f7f7f", width=2, dash="dash"),
            hovertext=hover_text,
            hoverinfo="text",
        )
    )

    # Zero reference line
    traces.append(
        go.Scatter(
            x=[dates.iloc[0], dates.iloc[-1]],
            y=[0, 0],
            mode="lines",
            line=dict(color="black", width=0.5, dash="dot"),
            showlegend=False,
            hoverinfo="skip",
        )
    )

    fig = go.Figure(data=traces)
    fig.update_layout(
        title="Portfolio vs SPY — Cumulative Return",
        xaxis=dict(title="Date", showgrid=True, gridcolor="rgba(0,0,0,0.07)"),
        yaxis=dict(
            title="Cumulative Return (%)",
            ticksuffix="%",
            showgrid=True,
            gridcolor="rgba(0,0,0,0.07)",
            zeroline=True,
            zerolinecolor="rgba(0,0,0,0.2)",
        ),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(t=60, b=40, l=60, r=20),
    )
    return fig


def make_intraday_curve(curve_df: pd.DataFrame) -> go.Figure:
    """Intraday portfolio-vs-SPY cumulative-return curve with a shaded alpha
    region.

    curve_df needs columns: ``time`` (datetime, ET), ``port_cum`` and
    ``spy_cum`` (percent points). The fill between the two lines is green
    where the portfolio leads SPY and red where it trails, so intraday
    out/under-performance reads at a glance. ``spy_cum`` may be all-NA (no
    SPY baseline) — then only the portfolio line is drawn, no shading.
    """
    if curve_df is None or curve_df.empty:
        fig = go.Figure()
        fig.update_layout(title="Intraday — No data yet")
        return fig

    df = curve_df.copy()
    t = df["time"]
    port = pd.to_numeric(df["port_cum"], errors="coerce")
    spy = pd.to_numeric(df["spy_cum"], errors="coerce")
    has_spy = spy.notna().any()

    traces = []

    # Shaded alpha region — only meaningful with a SPY baseline.
    if has_spy:
        s = spy.ffill().fillna(0.0)
        above_mask = port >= s
        segments = []
        current_above = bool(above_mask.iloc[0])
        seg_start = 0
        for i in range(1, len(above_mask)):
            if bool(above_mask.iloc[i]) != current_above:
                segments.append((seg_start, i, current_above))
                seg_start = i
                current_above = bool(above_mask.iloc[i])
        segments.append((seg_start, len(above_mask), current_above))

        for seg_start, seg_end, is_above in segments:
            idx = range(seg_start, seg_end)
            d_seg = t.iloc[idx]
            p_seg = port.iloc[idx]
            s_seg = s.iloc[idx]
            upper = p_seg if is_above else s_seg
            lower = s_seg if is_above else p_seg
            color = "rgba(0,200,100,0.15)" if is_above else "rgba(220,50,50,0.15)"
            traces.append(
                go.Scatter(
                    x=pd.concat([d_seg, d_seg[::-1]]),
                    y=pd.concat([upper, lower[::-1]]),
                    fill="toself", fillcolor=color, line=dict(width=0),
                    showlegend=False, hoverinfo="skip",
                )
            )

    traces.append(
        go.Scatter(
            x=t, y=port, mode="lines", name="Portfolio",
            line=dict(color="#1a73e8", width=2.5),
            hovertemplate="%{x|%-I:%M %p}<br>Portfolio: %{y:+.2f}%<extra></extra>",
        )
    )
    if has_spy:
        traces.append(
            go.Scatter(
                x=t, y=spy, mode="lines", name="S&P 500",
                line=dict(color="#7f7f7f", width=2, dash="dash"),
                hovertemplate="%{x|%-I:%M %p}<br>S&P 500: %{y:+.2f}%<extra></extra>",
            )
        )

    traces.append(
        go.Scatter(
            x=[t.iloc[0], t.iloc[-1]], y=[0, 0], mode="lines",
            line=dict(color="rgba(255,255,255,0.3)", width=0.5, dash="dot"),
            showlegend=False, hoverinfo="skip",
        )
    )

    fig = go.Figure(data=traces)
    fig.update_layout(
        xaxis=dict(
            title="", showgrid=True, gridcolor="rgba(255,255,255,0.06)",
            tickformat="%-I:%M %p", tickfont=dict(color="#aaa"),
        ),
        yaxis=dict(
            title="Today (%)", ticksuffix="%", showgrid=True,
            gridcolor="rgba(255,255,255,0.06)",
            zeroline=True, zerolinecolor="rgba(255,255,255,0.15)",
            tickfont=dict(color="#aaa"), title_font=dict(color="#aaa"),
        ),
        hovermode="x unified",
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02,
            xanchor="right", x=1, font=dict(color="#ccc"),
        ),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=20, b=40, l=60, r=20),
        height=300,
    )
    return fig
