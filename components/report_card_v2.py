"""
report_card_v2.py — render the evaluator's Report Card v2 (the 7-tile
MetricRecord substrate from ``evaluator/{date}/report_card.json``).

Three entry points:
  - ``render_home_summary(card)``  compact overall banner + 7 tile chips (home).
  - ``render_overview(card)``      full tile grid + per-tile letter/grade/coverage.
  - ``render_detail(card)``        filterable per-component MetricRecord tables
                                   (value, CI, N vs floor, target/red-line,
                                   status reason, trend) — the operator drill-down.

The letter is derived from status+value upstream; here ``status`` is the source
of truth and drives all colour. N/A-* render neutral (a component the producer
hasn't wired yet, NOT a low grade).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

# Canonical tile order — Portfolio Outcome leads (the system's product), then
# the component modules that decompose it (RC v2 Principle 7).
TILE_ORDER: list[tuple[str, str]] = [
    ("portfolio_outcome", "Portfolio Outcome"),
    ("research", "Research"),
    ("predictor", "Predictor"),
    ("executor", "Executor"),
    ("backtester", "Backtester"),
    ("substrate", "Substrate"),
    ("agent", "Agent Quality"),
]

_STATUS_EMOJI = {"GREEN": "🟢", "WATCH": "🟡", "RED": "🔴"}
_CRIT_RANK = {"critical": 0, "supporting": 1, "diagnostic": 2}


def _is_na(status: str) -> bool:
    return str(status).startswith("N/A")


def _chip(status: str | None) -> str:
    status = status or "N/A"
    if _is_na(status):
        return f"⚪ {status}"
    return f"{_STATUS_EMOJI.get(status, '⚪')} {status}"


def _real_graded(tile: dict) -> tuple[int, int]:
    comps = tile.get("components", []) or []
    real = sum(1 for c in comps if not _is_na(c.get("status", "")))
    return real, len(comps)


def _fmt_value(c: dict) -> str:
    v = c.get("value")
    if v is None:
        return "—"
    mt = c.get("metric_type")
    if mt == "pct":
        return f"{v:.1%}" if abs(v) <= 1.5 else f"{v:.2f}"
    if mt == "duration":
        return f"{v:.0f}d"
    if mt == "count":
        return f"{v:.0f}"
    return f"{v:.3g}"


def _fmt_ci(c: dict) -> str:
    lo, hi = c.get("ci_low"), c.get("ci_high")
    if lo is None or hi is None:
        return "—"
    return f"[{lo:.3g}, {hi:.3g}]"


def _fmt_n(c: dict) -> str:
    n, floor = c.get("n_samples"), c.get("n_floor")
    if n is None:
        return f"— / {floor}" if floor is not None else "—"
    return f"{n} / {floor}" if floor is not None else f"{n}"


def _provenance_caption(card: dict) -> str:
    prov = card.get("_provenance", {}) or {}
    arts = prov.get("artifacts", {}) or {}
    rd = prov.get("run_date", "?")
    n_read, n_missing = arts.get("n_read"), arts.get("n_missing")
    extra = f" · {n_read} artifacts read, {n_missing} absent" if n_read is not None else ""
    return f"Report Card v2 · run date **{rd}**{extra} · source `evaluator/{rd}/report_card.json`"


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_home_summary(card: dict | None) -> None:
    """Compact headline for the home page: overall status + 7 tile chips."""
    st.markdown("#### System Report Card")
    if not card:
        st.info("No Report Card published yet — the evaluator produces it as the final Saturday-pipeline step.")
        return
    overall = card.get("tiles_overall_status", "N/A")
    st.markdown(f"**Overall:** {_chip(overall)}")
    cols = st.columns(len(TILE_ORDER))
    for col, (key, label) in zip(cols, TILE_ORDER):
        tile = card.get("tiles", {}).get(key, {})
        with col:
            st.caption(label)
            st.markdown(_chip(tile.get("status")))
    st.caption(_provenance_caption(card))


def render_overview(card: dict | None) -> None:
    """Full overview: overall banner + a graded card per tile."""
    if not card:
        st.info(
            "No Report Card has been published yet. The evaluator builds "
            "`evaluator/{date}/report_card.json` as the final step of the "
            "Saturday pipeline (the non-fatal `ReportCard` SF state)."
        )
        return

    overall = card.get("tiles_overall_status", "N/A")
    banner = {"RED": st.error, "WATCH": st.warning, "GREEN": st.success}.get(overall, st.info)
    banner(f"**Overall system status: {_chip(overall)}** — outcome leads; the tiles below decompose it.")
    st.caption(_provenance_caption(card))

    tiles = card.get("tiles", {}) or {}
    # Two rows of tile cards.
    for row_start in (0, 4):
        row = TILE_ORDER[row_start:row_start + 4]
        cols = st.columns(len(row))
        for col, (key, label) in zip(cols, row):
            tile = tiles.get(key, {})
            real, total = _real_graded(tile)
            grade = tile.get("numeric_grade")
            with col:
                with st.container(border=True):
                    st.markdown(f"**{label}**")
                    st.markdown(f"### {_chip(tile.get('status'))}")
                    st.caption(
                        f"letter {tile.get('letter', 'N/A')}"
                        + (f" · {grade:.0f}/100" if grade is not None else "")
                        + f" · {real}/{total} graded"
                    )


def render_detail(card: dict | None, *, key_prefix: str = "rcd") -> None:
    """Filterable per-tile MetricRecord tables (the operator drill-down)."""
    if not card:
        st.info("No Report Card to drill into yet.")
        return

    st.caption(_provenance_caption(card))
    tiles = card.get("tiles", {}) or {}

    c1, c2 = st.columns([2, 3])
    with c1:
        status_filter = st.radio(
            "Show", ["All", "RED + WATCH only", "RED only", "N/A only"],
            horizontal=False, key=f"{key_prefix}_status",
        )
    with c2:
        tile_choices = ["All tiles"] + [label for _, label in TILE_ORDER]
        tile_pick = st.selectbox("Tile", tile_choices, key=f"{key_prefix}_tile")

    def _keep(status: str) -> bool:
        if status_filter == "All":
            return True
        if status_filter == "RED only":
            return status == "RED"
        if status_filter == "RED + WATCH only":
            return status in ("RED", "WATCH")
        if status_filter == "N/A only":
            return _is_na(status)
        return True

    for key, label in TILE_ORDER:
        if tile_pick != "All tiles" and tile_pick != label:
            continue
        tile = tiles.get(key, {})
        comps = sorted(
            tile.get("components", []) or [],
            key=lambda c: (_CRIT_RANK.get(c.get("criticality"), 3), c.get("name", "")),
        )
        rows = []
        for c in comps:
            if not _keep(c.get("status", "")):
                continue
            rows.append({
                "Component": c.get("name"),
                "Crit": c.get("criticality", ""),
                "Status": _chip(c.get("status")),
                "Value": _fmt_value(c),
                "CI": _fmt_ci(c),
                "N / floor": _fmt_n(c),
                "Target": "—" if c.get("target") is None else f"{c['target']:.3g}",
                "Red-line": "—" if c.get("red_line") is None else f"{c['red_line']:.3g}",
                "Trend": c.get("trend_decoration", ""),
                "Why": c.get("status_reason", ""),
            })
        real, total = _real_graded(tile)
        header = f"{label} — {_chip(tile.get('status'))} ({real}/{total} graded)"
        # Expand tiles that have any rows under the current filter.
        with st.expander(header, expanded=bool(rows) and tile_pick != "All tiles"):
            if rows:
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            else:
                st.caption("No components match the current filter.")
