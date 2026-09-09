"""Promotion Record — what the harness graded this cycle, and what it did
about it (alpha-engine-config-I10218, Brian's ruling 2026-09-08: content
option (a), nav option (e)).

The default landing page of live.nousergon.ai. Two promotion gates, both
weekly, both fed by producers that already exist:

  1. the **selection-producer** champion/challenger ledger — which entry-
     selection arm holds the live seat, what the weekly gate decided, and
     why (``config/producer_champion.json`` +
     ``config/apply_audit/producer_champion/{date}.json``);
  2. the **model-zoo (M-slot)** promotion leaderboard — which candidate
     models were scored out of sample, which cleared the gate, and what was
     promoted (``predictor/model_zoo/leaderboard/{date}.json``).

Why this is the landing page rather than the paper portfolio: the product
claim (``business/product-positioning/crucible.md`` §3) is promotion gates
and ablation discipline — every component must beat a named baseline before
it contributes, and components are removed when they lose. This page is that
claim rendered literally, from the record. The portfolio is experiment #1;
the harness is the thing.

What this page deliberately does NOT show:

  * **Nothing benchmark-relative** — no alpha, no vs-SPY, no Sharpe, no
    information ratio, no hit rate, no win rate (positioning §4). The
    dormant ``performance.py`` page owns that content and stays dormant.
  * **No ops tile** — no groom, Step-Functions watch, CI watch, box health,
    freshness monitor or agent telemetry (``architecture.d/069`` point 4).
    The loaders behind this page read only the two promotion artifacts, and
    ``live/promotion_record.py`` renders an explicit field allowlist.
  * **No status colour on an outcome** (point 1). A measured negative result
    is a result. Colour appears only on the awaiting-data state, which is
    never green.

Every grader verdict on this page renders with its reason (point 3), via
``promotion_record.verdict_line`` — the single chokepoint that makes a bare
verdict unrepresentable.
"""

from __future__ import annotations

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import streamlit as st

from loaders.s3_loader import (
    champion_audit_last_modified,
    list_champion_audit_dates,
    load_champion_audit,
    load_champion_audit_latest,
    load_champion_pointer,
    load_model_zoo_history,
    load_model_zoo_leaderboard,
    model_zoo_leaderboard_last_modified,
)
from promotion_record import (
    CHAMPION_COMPONENT,
    MODEL_ZOO_COMPONENT,
    arm_label,
    champion_evidence_notes,
    champion_history_rows,
    champion_score_rows,
    champion_verdict,
    missing_components,
    model_zoo_candidate_rows,
    model_zoo_estimator_note,
    model_zoo_history_rows,
    model_zoo_verdict,
    staleness_note,
)

_HISTORY_WEEKS = 12


def _table(rows: list[dict]) -> None:
    """Render rows as a plain table. No conditional styling: a promotion
    ledger is a record, and colouring its cells would reintroduce the status
    vocabulary architecture.d/069 point 1 keeps off outcomes."""
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


st.markdown("### Promotion record")
st.caption(
    "Every component of this system has to beat a named baseline before it "
    "is allowed to contribute, and it is removed when it stops winning. "
    "This page is that record: what the harness graded in its most recent "
    "weekly cycle, what it promoted or held, and the reason it gave. "
    "Decisions are shown whichever way they went — a held week and a "
    "negative result are results, not faults."
)

# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

champion_pointer = load_champion_pointer()
champion_audit = load_champion_audit_latest()
audit_dates = list_champion_audit_dates()
if not champion_audit and audit_dates:
    # The latest.json mirror can be absent while dated records exist; fall
    # back to the newest dated record rather than reporting the whole
    # producer missing, which would be a false absence.
    champion_audit = load_champion_audit(audit_dates[-1])

leaderboard = load_model_zoo_leaderboard()
zoo_history = load_model_zoo_history(limit=_HISTORY_WEEKS)
if not leaderboard and zoo_history:
    leaderboard = load_model_zoo_leaderboard(zoo_history[0].get("date"))

missing = missing_components(champion_audit, leaderboard)

# ---------------------------------------------------------------------------
# Awaiting data — names the missing components, never renders as green
# ---------------------------------------------------------------------------

if missing:
    st.warning(
        "**Awaiting data from "
        + ("both promotion gates" if len(missing) > 1 else "one promotion gate")
        + ".** The following producer(s) have written no record this system "
        "can read, so nothing below is claimed about them:\n\n"
        + "\n".join(f"- {name}" for name in missing)
        + "\n\nA component that emits nothing is unobserved, not healthy — "
        "so this is shown as an open question rather than as an empty "
        "success. Both gates write weekly."
    )

if len(missing) == 2:
    st.stop()

# ---------------------------------------------------------------------------
# 1 · Selection-producer champion/challenger ledger
# ---------------------------------------------------------------------------

if CHAMPION_COMPONENT not in missing:
    st.markdown("#### Entry-selection arm — weekly champion/challenger gate")
    st.caption(
        "Several ways of choosing which stocks to look at run side by side "
        "every week. Each is scored on the same measurement, against the "
        "same named baseline arm, and the one holding the live seat keeps it "
        "only while it wins. Scores below are that gate's own measurement — "
        "they are not returns and not a comparison to any market index."
    )

    stale = staleness_note(
        CHAMPION_COMPONENT,
        champion_audit_last_modified(),
        (champion_audit or {}).get("date"),
    )
    if stale:
        st.warning(stale)

    current = (champion_pointer or {}).get("champion")
    left, right = st.columns([1, 2])
    with left:
        if current:
            st.metric("Live champion arm", arm_label(current))
            promoted_at = (champion_pointer or {}).get("promoted_at")
            if promoted_at:
                st.caption(f"Holding the seat since {promoted_at}.")
        else:
            # An honest absence, not a guessed default.
            st.metric("Live champion arm", "not recorded")
            st.caption(
                "No champion pointer has been written yet, so the live arm "
                "is not asserted here."
            )
    with right:
        st.markdown(f"**Most recent decision** ({(champion_audit or {}).get('date', '—')})")
        st.markdown(champion_verdict(champion_audit))
        notes = champion_evidence_notes(champion_audit)
        if notes:
            st.caption(
                "How much evidence stood behind each arm this week — "
                + "; ".join(notes)
                + ". Shown beside the scores rather than instead of them: "
                "“we could not tell” and “the challenger lost” "
                "are different outcomes."
            )

    score_rows = champion_score_rows(champion_audit)
    if score_rows:
        _table(score_rows)
        st.caption(
            "Estimator: each arm's realized weekly top-N lift over the same "
            "shared baseline arm, on the gate's 21-trading-day horizon. "
            "An arm with no comparable evidence this week shows an em dash, "
            "never a zero."
        )
    else:
        st.caption(
            "No per-arm scores were recorded for this week — the record "
            "above states why the gate could not compare them."
        )

    history = [
        a for a in (load_champion_audit(d) for d in audit_dates[-_HISTORY_WEEKS:])
        if isinstance(a, dict) and a
    ]
    rows = champion_history_rows(list(reversed(history)))
    if rows:
        with st.expander(f"Previous weeks ({len(rows)} recorded decisions)"):
            _table(rows)
            st.caption(
                "One row per weekly decision, newest first, each with the "
                "reason the gate gave. A record is written every week "
                "regardless of the outcome, so a run of held weeks is "
                "evidence the gate ran, not evidence it stalled."
            )

    st.divider()

# ---------------------------------------------------------------------------
# 2 · Model-zoo (M-slot) weekly promotion leaderboard
# ---------------------------------------------------------------------------

if MODEL_ZOO_COMPONENT not in missing:
    st.markdown("#### Prediction model — weekly promotion leaderboard")
    st.caption(
        "Each week a set of candidate models is trained and scored out of "
        "sample, and the serving model is replaced only by a candidate that "
        "clears the champion architecture by a required margin. Every "
        "candidate below carries the gate's verdict and the reason for it."
    )

    stale = staleness_note(
        MODEL_ZOO_COMPONENT,
        model_zoo_leaderboard_last_modified(),
        (leaderboard or {}).get("date"),
    )
    if stale:
        st.warning(stale)

    st.markdown(f"**Cycle {(leaderboard or {}).get('date', '—')}**")
    st.markdown(model_zoo_verdict(leaderboard))

    cand_rows = model_zoo_candidate_rows(leaderboard)
    if cand_rows:
        _table(cand_rows)
        st.caption(model_zoo_estimator_note(leaderboard))
    else:
        st.caption(
            "No candidates were recorded for this cycle — the verdict above "
            "states what the rotation did with that."
        )

    hist_rows = model_zoo_history_rows(zoo_history)
    if hist_rows:
        with st.expander(f"Previous cycles ({len(hist_rows)} recorded rotations)"):
            _table(hist_rows)
            st.caption(
                "Newest first. A cycle that promoted nothing is the ordinary "
                "case: the gate exists to refuse, and a leaderboard full of "
                "candidates that did not clear it is the discipline working."
            )

st.divider()
st.caption(
    "Both gates run weekly and write their record whether or not anything "
    "moved. Where a figure is absent it is shown as absent, never as zero "
    "and never as a pass. What the system is and how it is designed: "
    "[crucible.nousergon.ai](https://crucible.nousergon.ai)"
)
