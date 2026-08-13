"""
director_plan.py — render the Director's weekly action plan (Layer C advisory).

The Director is a single structured Opus call over the Report Card v2 that emits
a ``DirectorWeeklyActionPlan`` (``director/{date}/action_plan.json``) plus an
upsert-by-id carry-over ledger (``director/carryover_ledger.json``). It
*proposes* — it never writes live trading config and never self-merges. This
surface is read-only observability for the observe-mode soak.

Two entry points:
  - ``render_overview(plan, ledger)``  system summary + top risks + this-week
                                       action items + carry-over review + self-grade.
  - ``render_ledger(ledger)``          the full carry-over ledger table (every id,
                                       its status, and first/last-seen window).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

# Priority drives ranking + colour; carries the same P0-P3 vocabulary as ROADMAP.
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# Item lifecycle status → chip. Mirrors the carryover ledger's status vocabulary.
_ITEM_STATUS_EMOJI = {
    "proposed": "🆕",
    "carried_over": "🔁",
    "resolved": "✅",
    "dropped": "🗑",
}

# Horizon → short label for the table.
_HORIZON_LABEL = {
    "this_week": "this week",
    "carryover": "carry-over",
    "watch": "watch",
}


def _priority_rank(item: dict) -> int:
    return _PRIORITY_RANK.get(item.get("priority"), 9)


def _status_chip(status: str | None) -> str:
    status = status or "proposed"
    return f"{_ITEM_STATUS_EMOJI.get(status, '•')} {status}"


def _provenance_caption(plan: dict) -> str:
    rd = plan.get("run_date", "?")
    return (
        f"Director weekly action plan · run date **{rd}** · "
        f"source `director/{rd}/action_plan.json` · advisory only "
        "(proposes; never writes live config, never self-merges)."
    )


def _items_table(items: list[dict]) -> pd.DataFrame:
    rows = []
    for it in sorted(items, key=_priority_rank):
        rows.append({
            "Pri": it.get("priority", "—"),
            "Status": _status_chip(it.get("status")),
            "Action": it.get("title", ""),
            "Owner": it.get("proposed_owner", "—"),
            "Horizon": _HORIZON_LABEL.get(it.get("horizon"), it.get("horizon", "—")),
            "Type": it.get("suggested_change_type", "—"),
            "Conf": f"{it.get('confidence', 0)}",
            "Rationale": it.get("rationale", ""),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_plan_attestation(plan: dict | None) -> str:
    """Render the correctness verdict the Director stamped on this plan.

    ``sf-pipeline-policy.md`` §2.3a rule 3 — every surface presenting the run's
    results carries the verdict state. A weekly action plan is the run's results
    turned into proposals; rendering the proposals without the verdict asserts a
    guarantee nobody established, and does it on the page an operator is most
    likely to act from.

    The **absent** case is what this exists for. A plan written before the
    Director consumed the verdict, and a plan from a cycle where the verdict
    producer died, both arrive with no ``attestation`` key — and are otherwise
    indistinguishable from a fully-attested one. Both render UNKNOWN.
    """
    if not plan:
        return "UNKNOWN"

    from components.report_card_v2 import (
        ATTESTATION_FAIL,
        ATTESTATION_PASS,
        ATTESTATION_UNKNOWN,
        _as_of_line,
    )

    block = plan.get("attestation")
    block = block if isinstance(block, dict) else {}
    raw = block.get("verdict")
    verdict = raw if raw in (ATTESTATION_PASS, ATTESTATION_FAIL, ATTESTATION_UNKNOWN) \
        else ATTESTATION_UNKNOWN

    if verdict == ATTESTATION_PASS:
        st.success(
            "✅ **Correctness attestation: PASS** — the Report Card numbers this "
            "plan reasons over were verified against hand-derived known answers.\n\n"
            + _as_of_line(block)
        )
        return verdict

    withheld = plan.get("actions_withheld") or []
    banner = st.error if verdict == ATTESTATION_FAIL else st.warning
    banner(
        (
            "🔴 **Correctness attestation: FAIL — the numbers this plan reasons "
            "over are WRONG, not merely unverified.**"
            if verdict == ATTESTATION_FAIL else
            "⚠️ **Correctness attestation: UNKNOWN — the numbers this plan reasons "
            "over are NOT established as correct.**"
        )
        + " This is advisory diagnosis only.\n\n"
        + (
            "The Director **withheld** " + ", ".join(f"`{a}`" for a in withheld)
            + " this cycle: nothing below was filed, reopened or escalated.\n\n"
            if withheld else
            "The plan carries no attestation block at all — it predates the "
            "verdict consumer, or the producer never ran. Absence of evidence "
            "is never a pass.\n\n"
        )
        + (f"> {block.get('reason')}\n\n" if block.get("reason") else "")
        + _as_of_line(block)
    )
    return verdict


def render_overview(plan: dict | None, ledger: dict | None = None) -> None:
    """Full weekly read: summary + risks + action items + carry-over + self-grade."""
    if not plan:
        st.info(
            "No Director plan has been published yet. The Director is the final "
            "Saturday-pipeline task (a single Opus call over the Report Card), and "
            "it runs only once `DIRECTOR_ENABLED` is flipped on after the "
            "observe-mode soak gate — until then it is dormant by design."
        )
        if ledger and ledger.get("items"):
            st.divider()
            render_ledger(ledger)
        return

    # §2.3a rule 3 — FIRST, above the plan's own provenance. The plan is a
    # derivative of the Report Card's numbers, so it inherits the card's
    # correctness verdict; the Director stamps it onto the artifact
    # (crucible-evaluator `director/verdict.py::stamp_plan_artifact`) precisely
    # so this page does not have to re-read the card to know.
    render_plan_attestation(plan)

    st.caption(_provenance_caption(plan))

    summary = plan.get("system_summary")
    if summary:
        st.markdown(f"> {summary}")

    top_risks = plan.get("top_risks") or []
    if top_risks:
        st.markdown("#### Top risks")
        for r in top_risks:
            st.markdown(f"- {r}")

    items = plan.get("action_items") or []
    st.markdown(f"#### Action items ({len(items)})")
    if items:
        st.dataframe(_items_table(items), use_container_width=True, hide_index=True)
        # Evidence is per-item and verbose — expose it under expanders, not in the grid.
        for it in sorted(items, key=_priority_rank):
            evidence = it.get("evidence") or []
            if not evidence:
                continue
            with st.expander(f"{it.get('priority', '—')} · {it.get('title', '')} — evidence"):
                for e in evidence:
                    st.markdown(f"- `{e}`")
    else:
        st.caption("The Director proposed no action items this cycle.")

    carryover_review = plan.get("carryover_review") or []
    if carryover_review:
        st.markdown("#### Carry-over review (disposition of last week's items)")
        for line in carryover_review:
            st.markdown(f"- {line}")

    self_grade = plan.get("self_grade")
    if self_grade:
        st.markdown("#### Director self-grade")
        c1, c2 = st.columns(2)
        c1.metric("Grounding", f"{self_grade.get('grounding', '—')}/100")
        c2.metric("Actionability", f"{self_grade.get('actionability', '—')}/100")
        notes = self_grade.get("notes")
        if notes:
            st.caption(notes)

    if ledger and ledger.get("items"):
        st.divider()
        render_ledger(ledger)


def render_ledger(ledger: dict | None) -> None:
    """The carry-over ledger: every tracked id, its status, and its age window."""
    st.markdown("#### Carry-over ledger")
    if not ledger or not ledger.get("items"):
        st.caption("The carry-over ledger is empty (no Director plan has run yet).")
        return

    st.caption(
        f"Upsert-by-id across weeks · last updated **{ledger.get('updated', '?')}** · "
        "source `director/carryover_ledger.json`"
    )
    rows = []
    for it in sorted(ledger.get("items", []), key=_priority_rank):
        rows.append({
            "Pri": it.get("priority", "—"),
            "Status": _status_chip(it.get("status")),
            "Item": it.get("title", ""),
            "Owner": it.get("proposed_owner", "—"),
            "First seen": it.get("first_seen", "—"),
            "Last seen": it.get("last_seen", "—"),
            "ID": it.get("id", ""),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
