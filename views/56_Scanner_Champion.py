"""
Scanner champion/challenger — Alpha Engine (private console)

THE QUESTION THIS PANE ANSWERS, THAT NO OTHER PANE ANSWERS
----------------------------------------------------------
*Which universe cut is feeding the sector teams right now, what did the
promotion engine decide this cycle, why, and how far is it from being able to
decide anything at all?*

Before this page (alpha-engine-config-I9278) the scanner-cut slot wrote a
decision record on every weekly evaluation and **nothing rendered any of
them**. Fifty-two consecutive ``hold`` decisions and fifty-two silent failures
were therefore indistinguishable to a reader — ``principles.md`` §2.7. The only
path that reached anyone was an Overseer playbook that fires when the engine
RAISES, which says nothing at all about an engine that has stopped.

NOT THE SAME SLOT AS "ABLATIONS → CHAMPION LOOP"
------------------------------------------------
``views/46_Experiments.py``'s Champion-loop tab is the BACKTESTER's executor
selection-path switch (``scanner_predictor_direct`` vs ``thinktank_coverage``).
This page is the SCANNER CUT — which universe cut the sector-team feed resolves
from. Separate axes; ``champion-challenger-policy.md`` §2 forbids conflating
them, and they have separate pointers, separate audit prefixes and separate
producers.

WHAT IS RENDERED, AND FROM WHERE
--------------------------------
Everything on this page is a projection of an artifact crucible-research
already writes. Nothing is re-derived here (crucible-dashboard AGENTS.md: the
dashboard is a view, not a measurement layer) — with one exception, the
LIVENESS CLAIM, which is a statement about an artifact's own age and is the
one fact only the reader can make.

  live pointer + champion_before   config/{slot}_champion.json
  promote/hold series              config/apply_audit/{slot}/{date}.json
  liveness (>8d ⇒ RED, with age)   config/apply_audit/{slot}/latest.json
  per-arm evidence                 the record's own `arms` block
  weekly per-arm performance       research/cuts_weekly_ledger/ledger.parquet

TWO SLOTS, ONE RENDERER
-----------------------
``loaders/scanner_champion.py::SLOTS`` binds each slot to its keys. The SPEC
slot (alpha-engine-config-I9273, producer ``scoring/spec_promotion.py``) has
written nothing yet: it renders NEVER_RAN, explicitly, naming the keys it is
waiting on — and it begins rendering its decisions with **no change to this
repo** the moment the producer writes its first record.

Reads only recorded S3 artifacts — no LLM call, no cost. Native Streamlit
chrome — no set_page_config (app.py's st.navigation owns it).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from loaders.s3_loader import (
    list_slot_audit_dates,
    load_cuts_weekly_ledger,
    load_slot_audit,
    load_slot_champion_pointer,
)
from loaders.scanner_champion import (
    LEDGER_KEY,
    SLOTS,
    STALE_AFTER_DAYS,
    arm_table,
    basis_row,
    decision_series,
    fmt_optional,
    ledger_arms,
    ledger_view,
    load_slot_view,
    schema_version_note,
)

# State → (badge glyph, renderer). Colour carries exactly ONE axis — the §8.3
# state — and every state is ALSO carried by a label, so colour is never the
# sole encoding (console-policy.md §5.7).
_STATE_RENDER = {
    "HEALTHY": ("🟢", st.success),
    "MISSED": ("🔴", st.error),
    "FAILED": ("🔴", st.error),
    "UNREPORTED": ("🟠", st.warning),
    "NEVER_RAN": ("⚪", st.info),
}


def _render_liveness(claim: dict) -> None:
    """The liveness claim, first and largest — it BOUNDS every other fact on
    the slot's card, so it is rendered before them, never beside them."""
    glyph, box = _STATE_RENDER.get(claim["state"], ("🟠", st.warning))
    box(f"{glyph} **{claim['state']}** — {claim['headline']}")
    age = claim["age_days"]
    st.caption(
        f"as-of **{claim['as_of'] or '—'}** (source: {claim['as_of_source']}) · "
        f"age **{age if age is not None else '—'}d** · "
        f"stale bound **{claim['stale_after_days']}d**"
    )
    st.caption(claim["detail"])


def _render_pointer(view: dict) -> None:
    pointer = view["pointer"]
    if not pointer["present"]:
        st.warning(
            f"⚪ **No live pointer.** `{view['sources']['pointer']}` does not exist. "
            "The feed therefore resolves from the producer's own default — this "
            "page does not guess which cut that is, because a guessed pointer is "
            "the 2026-07-22 drift (alpha-engine-config-I7808) rendered as fact."
        )
        return

    disposition = pointer["reason_code_disposition"]
    cols = st.columns(4)
    cols[0].metric("Champion (live)", fmt_optional(pointer["champion"]))
    cols[1].metric("Champion before", fmt_optional(pointer["champion_before"]))
    cols[2].metric("Decision", fmt_optional(pointer["decision"]))
    cols[3].metric("Decided on", fmt_optional(pointer["decided_on"]))

    reason = fmt_optional(pointer["reason_code"])
    if disposition == "defect":
        st.error(f"🔴 reason_code **{reason}** — a DEFECT. The engine wrote this record and then raised.")
    elif disposition == "normal":
        st.caption(
            f"reason_code **{reason}** — an expected steady state for this slot, "
            "not a warning. `weekly_ledger_missing`, `insufficient_weeks` and "
            "`no_promotable_challenger` all mean the engine ran correctly and had "
            "nothing to promote. Only `board_defective` is a defect."
        )
    else:
        st.warning(
            f"🟠 reason_code **{reason}** is not in this console's declared "
            "taxonomy (crucible-research/contracts/scanner_cut_champion.schema.json). "
            "Rendered rather than dropped, and counted as unrecognised rather than "
            "assumed benign."
        )

    st.caption(
        f"**Earliest possible decision:** {fmt_optional(pointer['decision_earliest_on'])} "
        "— a CEILING, not a promise: it says the evidence cannot exist before this "
        "date, never that it will exist on it. `n_weeks_paired` below is what says "
        "where the evidence actually stands."
    )
    st.caption(f"Record schema: {pointer['record_schema_note']}")


def _render_arms(record: dict | None) -> None:
    """Per-arm evidence, in the RECORD'S OWN field vocabulary.

    A v1 record's ``topn_alpha_vs_population_mean`` is a 126-session
    forward-window mean; a v2 record's ``mean_paired_log_return`` is a paired
    weekly net difference against the serving champion. Rendering the first
    under the second's label would be a fabricated fact, so the columns come
    from the record's own ``schema_version`` and the two vocabularies are never
    merged."""
    frame, specs, version = arm_table(record)
    if frame.empty:
        st.info(
            "⚪ **No arms block on this record.** Not zero arms — no arms block. "
            "The record exists and does not carry one."
        )
        return
    st.caption(f"Arm evidence · {schema_version_note(version)}")
    st.dataframe(frame, hide_index=True, width="stretch")
    if specs:
        st.caption(
            " · ".join(f"**{s.label}** — {s.unit}" for s in specs)
            + "  ·  a null renders as an em dash: *produced no comparable evidence* "
            "and *scored zero* are different facts."
        )
    else:
        st.warning(
            "🟠 No field descriptors for this record's schema version — arm fields "
            "are rendered under their RAW names. Nothing is dropped and nothing is "
            "relabelled."
        )


def _render_decision_series(view: dict) -> None:
    frame = decision_series(view["decisions"])
    listed = view["decision_count_listed"]
    if frame.empty:
        st.info(
            f"⚪ **No decision records.** `{view['sources']['audit_prefix']}` lists "
            f"{listed} dated key(s). An empty history is stated, never drawn as an "
            "empty table with no explanation."
        )
        return
    st.caption(
        f"Newest first · showing {len(frame)} of {listed} record(s) listed under "
        f"`{view['sources']['audit_prefix']}` · one row per evaluation, promote or hold"
    )
    st.dataframe(frame, hide_index=True, width="stretch")
    versions = sorted({v for v in frame["schema_version"].tolist() if v is not None})
    if len(versions) > 1:
        st.warning(
            "🟠 This history spans schema versions "
            + ", ".join(f"v{v}" for v in versions)
            + ". The columns above are version-independent by construction. The "
            "per-arm NUMBERS are not, and are never shown side by side across the "
            "boundary — open a single record below to see them under their own "
            "version's labels."
        )


def _render_slot(slot, view: dict) -> None:
    st.subheader(f"{slot.label} — `{slot.slot_id}`")
    st.caption(
        f"Decides {slot.what_it_decides}. Producer: `{slot.producer}`. "
        f"Pointer: `{slot.pointer_key}` · audit: `{slot.audit_prefix}`."
    )
    _render_liveness(view["liveness"])

    if view["liveness"]["state"] == "NEVER_RAN":
        st.caption(
            "This slot is onboarded and waiting. It is listed here BECAUSE it has "
            "written nothing — a component absent from a list because it reported "
            "nothing is the failure mode this pane exists to prevent."
        )
        return

    _render_pointer(view)

    st.markdown("**Decision basis**")
    basis = basis_row(view.get("_pointer_record"))
    if basis:
        st.dataframe(
            pd.DataFrame(basis, columns=["Field", "Value", "Unit"]),
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("— no decision-basis fields on this record.")

    st.markdown("**Per-arm evidence (live pointer record)**")
    _render_arms(view.get("_pointer_record"))

    st.markdown("**Decision history**")
    _render_decision_series(view)

    dates = list(reversed(list_slot_audit_dates(slot.audit_prefix)))
    if dates:
        with st.expander("Open one record"):
            chosen = st.selectbox(
                "decided_on", dates, key=f"{slot.slot_id}_record_pick"
            )
            record = load_slot_audit(slot.audit_prefix, chosen)
            if record is None:
                st.error(
                    f"🔴 `{slot.audit_prefix}{chosen}.json` was listed but could not "
                    "be read. A listed key that will not load is a finding, not an "
                    "empty record."
                )
            else:
                st.caption(schema_version_note(record.get("schema_version")))
                st.caption(f"reason: {fmt_optional(record.get('reason'))}")
                _render_arms(record)
                st.json(record, expanded=False)

    with st.expander("Machine-readable projection (console-policy.md §3.8)"):
        st.caption(
            "The same query this page renders, verbatim. An agent and an operator "
            "read the same structure, so they cannot diverge."
        )
        st.json(view["_json"], expanded=False)


def _render_ledger() -> None:
    st.subheader("Weekly ledger — `research/cuts_weekly_ledger/ledger.parquet`")
    st.caption(
        "The scanner-cut slot's append-only weekly performance record: one row per "
        "(arm, week held). ONE object holding the whole series — read whole, never "
        "per-week keys, and never written from this repo. Producer: "
        "`crucible-research/scoring/weekly_ledger.py`."
    )
    frame = load_cuts_weekly_ledger()
    if frame is None:
        st.warning(
            f"🟠 **`{LEDGER_KEY}` is absent or unreadable.** That is an artifact "
            "state, not a performance of zero — and it is the expected steady state "
            "until the ledger's producer has run (`weekly_ledger_missing`)."
        )
        return
    if frame.empty:
        st.warning(
            f"🟠 **`{LEDGER_KEY}` exists and holds no rows.** Distinct from the "
            "object being absent: the producer has run and written nothing. "
            "champion-challenger-policy.md §7.2 forbids rendering those identically."
        )
        return

    view, undeclared = ledger_view(frame)
    arms = ledger_arms(frame)
    weeks = sorted({str(w) for w in frame.get("week_start", pd.Series(dtype=str)).dropna()})
    st.caption(
        f"{len(frame)} row(s) · {len(arms)} arm(s) · {len(weeks)} week(s) "
        f"({weeks[0] if weeks else '—'} → {weeks[-1] if weeks else '—'}). "
        "Arms are read from the ledger's own `arm` column — never from a list in "
        "this repo."
    )
    st.dataframe(view, hide_index=True, width="stretch")
    st.caption(
        "**Net** is after transaction cost; a blank **Net** carries a **Why net is "
        "absent** beside it and is NEVER filled in from Gross. **Turnover** is a "
        "fraction of the basket. A null renders as an em dash."
    )
    if "net_log_return" in frame.columns:
        missing_net = int(frame["net_log_return"].isna().sum())
        st.caption(
            f"Net available on {len(frame) - missing_net} / {len(frame)} row(s) — "
            "the denominator is stated inline, because a mean over rows whose net "
            "could not be computed would be an aggregate over incomplete input."
        )
    if undeclared:
        with st.expander(f"{len(undeclared)} further ledger column(s), undeclared here"):
            st.caption(
                "Present on the parquet, with no descriptor on this page. Rendered "
                "rather than dropped: a dropped field is a fact the producer "
                "believes is on the surface and is not."
            )
            st.dataframe(frame.loc[:, undeclared], hide_index=True, width="stretch")


def main() -> None:
    st.title("⚖️ Scanner champion/challenger")
    st.caption(
        "Which cut feeds the sector teams, what the promotion engine decided this "
        "cycle, and whether the engine is still running at all. "
        f"A slot whose `latest.json` is older than {STALE_AFTER_DAYS} days renders "
        "RED with its age — a dead engine must not read as an engine that held "
        "(alpha-engine-config-I9278)."
    )
    st.caption(
        "Not the same slot as **Ablations → Champion loop**, which is the "
        "backtester's executor selection-path switch."
    )

    now = datetime.now(timezone.utc)
    for slot in SLOTS:
        view = load_slot_view(slot, now=now)
        # The pointer record itself, kept beside the projection so the page can
        # render version-specific blocks from it (TTL-cached, so no second GET).
        view["_pointer_record"] = load_slot_champion_pointer(slot.pointer_key)
        view["_json"] = json.loads(json.dumps(
            {k: v for k, v in view.items() if not k.startswith("_")}, default=str
        ))
        with st.container(border=True):
            _render_slot(slot, view)

    _render_ledger()

    st.caption(
        f"Page rendered {now.isoformat()} · every figure above is a projection of a "
        "durable S3 artifact; the console owns none of them."
    )


main()
