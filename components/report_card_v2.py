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

# ---------------------------------------------------------------------------
# Correctness attestation (sf-pipeline-policy.md §2.3a rule 3)
# ---------------------------------------------------------------------------
# The card carries ``attestation`` — the run's correctness VERDICT, produced by
# crucible-evaluator ``grading/attestation.py::build_run_attestation`` (schema
# ``report_card_attestation-1.0.0``) as the worst of two halves: the evaluator's
# own known-answer battery against the deployed ``nousergon_lib.quant``
# primitives, and the backtester's in-process battery read from
# ``backtest/{run_date}/attestation.json``.
#
# §2.3a rule 3: *every surface presenting the run's results carries the verdict
# state.* This dashboard is the third such surface (the card JSON and the
# Director digest are the other two). Rendering the tiles without it asserts a
# guarantee nobody established — and an ABSENT block is the case that matters,
# because it is exactly what a cycle where the verdict producer never ran looks
# like. `principles.md` §2.7: no data is never rendered as green.

ATTESTATION_PASS = "PASS"
ATTESTATION_FAIL = "FAIL"
ATTESTATION_UNKNOWN = "UNKNOWN"


def verdict_is_pass(verdict: str | None) -> bool:
    """True only for the literal string ``PASS``.

    Mirrors ``crucible-evaluator grading/attestation.py::verdict_is_pass`` (and
    the backtester's). Deliberately NOT a truthiness test: ``None``, ``""``,
    ``"ok"`` and ``UNKNOWN`` must every one of them withhold the guarantee, so
    the "missing reads as pass" bug cannot be written here either.
    """
    return verdict == ATTESTATION_PASS


def _attestation_verdict(card: dict) -> str:
    """The card's verdict, normalized onto the closed vocabulary.

    An absent block, a non-mapping block, and an unrecognised verdict string all
    resolve to ``UNKNOWN`` — never to a pass, and never to silence.
    """
    block = card.get("attestation")
    if not isinstance(block, dict):
        return ATTESTATION_UNKNOWN
    raw = block.get("verdict")
    if raw in (ATTESTATION_PASS, ATTESTATION_FAIL, ATTESTATION_UNKNOWN):
        return raw
    return ATTESTATION_UNKNOWN


def _half_checks(block: dict, half: str) -> str:
    h = block.get(half)
    if not isinstance(h, dict):
        return f"{half} —"
    n = h.get("n_checks")
    return f"{half} {n if n is not None else '—'}"


def render_attestation(card: dict | None) -> str:
    """Render the run's correctness verdict. Returns the verdict rendered.

    The FIRST element on any surface that shows this card's numbers. Also
    surfaces ``degraded_staleness`` / ``stale_tiles``, which the evaluator has
    emitted since config#2885 and which had never reached a rendering surface.
    """
    if not card:
        # No card at all — the caller's own "nothing published" notice covers
        # it, and there are no numbers on screen to qualify.
        return ATTESTATION_UNKNOWN

    verdict = _attestation_verdict(card)
    block = card.get("attestation") if isinstance(card.get("attestation"), dict) else {}

    if verdict == ATTESTATION_PASS:
        st.success(
            "✅ **Correctness attestation: PASS** — the deployed quant primitives and "
            "the backtest engine each agreed with their hand-derived known answers "
            f"this cycle ({_half_checks(block, 'evaluator')} checks, "
            f"{_half_checks(block, 'backtester')} checks)."
        )
    elif verdict == ATTESTATION_FAIL:
        st.error(
            "🔴 **Correctness attestation: FAIL — the numbers on this page are WRONG, "
            "not merely unverified.** A known-answer check disagreed with its "
            "hand-derived expectation, so the arithmetic that produced this cycle's "
            "grades has moved. Do not act on the tiles below.\n\n"
            f"> {block.get('reason') or 'no reason recorded by the producer.'}"
        )
    else:
        reason = block.get("reason") if block else None
        st.warning(
            "⚠️ **Correctness attestation: UNKNOWN — the numbers on this page are NOT "
            "established as correct.** "
            + (
                "The card carries no attestation block at all: the verdict producer "
                "never ran this cycle, so nothing checked whether the arithmetic "
                "behind these grades is still right."
                if not block
                else "The correctness guarantee is WITHHELD for this cycle."
            )
            + " This is an absence of evidence, never a pass.\n\n"
            + (f"> {reason}" if reason else "")
        )

    # config#2885 staleness flags — same rule, different degradation axis.
    if card.get("degraded_staleness"):
        stale = card.get("stale_tiles") or []
        st.warning(
            "⚠️ **Degraded (stale inputs):** "
            + (
                f"{len(stale)} tile(s) graded on stale upstream data — "
                f"{', '.join(str(s) for s in stale)}."
                if stale
                else "at least one tile was graded on stale upstream data."
            )
        )
    return verdict


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
    # §2.3a rule 3 — the verdict precedes the numbers on EVERY surface, the home
    # summary included: this chip row is the most-read rendering of the card.
    render_attestation(card)
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

    # §2.3a rule 3 — FIRST element, above the overall grade. A grade rendered
    # before the verdict has already asserted the guarantee.
    verdict = render_attestation(card)
    attested = verdict_is_pass(verdict)

    overall = card.get("tiles_overall_status", "N/A")
    banner = {"RED": st.error, "WATCH": st.warning, "GREEN": st.success}.get(overall, st.info)
    banner(f"**Overall system status: {_chip(overall)}** — outcome leads; the tiles below decompose it.")
    st.caption(_provenance_caption(card))
    if not attested:
        st.caption(
            f"Grades below are UNVERIFIED (correctness attestation {verdict}) — "
            "rendered for diagnosis only, not as an established result."
        )

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
                    # De-emphasise the grade when the run is not attested: the
                    # status chip still says what the grader concluded, the
                    # letter/score is greyed so it does not read as a result.
                    chip = _chip(tile.get("status"))
                    st.markdown(f"### {chip}" if attested else f"### :gray[{chip}]")
                    detail = (
                        f"letter {tile.get('letter', 'N/A')}"
                        + (f" · {grade:.0f}/100" if grade is not None else "")
                        + f" · {real}/{total} graded"
                    )
                    st.caption(detail if attested else f":gray[unverified · {detail}]")


def render_detail(card: dict | None, *, key_prefix: str = "rcd") -> None:
    """Filterable per-tile MetricRecord tables (the operator drill-down)."""
    if not card:
        st.info("No Report Card to drill into yet.")
        return

    # §2.3a rule 3 — the drill-down renders every component's NUMBER, so it
    # carries the verdict too; a surface reached by a different tab is still a
    # surface presenting the run's results.
    render_attestation(card)
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
