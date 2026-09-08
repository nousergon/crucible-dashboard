"""Promotion-record presentation logic — the PURE core behind
``live/pages/promotion_decisions.py`` (alpha-engine-config-I10218).

No streamlit, no boto3, no S3. Everything here turns one recorded artifact
(a weekly champion-promotion audit, or a weekly model-zoo leaderboard) into
the rows and sentences the page renders, so the rules that govern this
surface are unit-testable rather than asserted by reading Streamlit calls.

The rules, from ``architecture.d/069`` and Brian's ruling on
``alpha-engine-config-I10218``:

1. **Every grader verdict renders with its reason** (point 3). There is no
   code path here that produces a verdict without one — ``verdict_line``
   substitutes an explicit "reason not recorded" sentence naming the schema
   version rather than emitting a bare verdict, because a bare verdict reads
   as breakage while an honest RED that explains itself reads as a rigorous
   grader.
2. **No status colour on a performance outcome** (point 1). Scores are
   returned as numbers with their estimator and sample size beside them;
   nothing here maps a score to a status, a colour or a letter.
3. **No ops content** (point 4). The fields this module reads are named
   explicitly; process/guardrail telemetry (PBO, chasing-noise, feed-liveness
   probes, freshness sinks) is not among them and is not passed through.
4. **Awaiting-data names the missing component** (principles §2.7).
   ``missing_components`` returns the artifact names, never an empty success.
5. **Nothing benchmark-relative.** No field read here is a market-benchmark
   comparison. The arm scores ARE the promotion gate's own measurement — an
   arm's lift over the *named baseline arm* it must beat, which is the
   positioning §3 claim rendered literally — and they are labelled as such,
   never as alpha and never against an index.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Selection-producer arms. Mirrors crucible-backtester
# optimizer/champion_promotion.py::VALID_CHAMPIONS. Unknown slugs render
# as themselves rather than being dropped — a slug we cannot label is still
# a decision the record made.
CHAMPION_ARM_LABELS: dict[str, str] = {
    "scanner_predictor_direct": "Scanner → predictor (no agent)",
    "scanner_top20_predictor": "Scanner top-20 → predictor",
    "thinktank_coverage": "Think Tank coverage (per-ticker theses)",
    "agentic": "Agentic (retired 2026-07-14)",
}

# ``outcome`` values from producer_champion_audit.schema.json. Wording is
# deliberately free of "failed"/"broken": a held week is the gate working.
OUTCOME_LABELS: dict[str, str] = {
    "promoted": "Promoted",
    "demoted": "Demoted",
    "no_contest": "Held — no contest",
    "unchanged_winner_already_champion": "Held — champion defended",
    "held_shadow_only": "Held — shadow-only arm won",
    "held_insufficient_data": "Held — insufficient data",
    "held_cooldown": "Held — cooldown",
    "held_not_significant": "Held — not significant",
    "error": "Could not decide",
}

# ``blocked_by`` slugs → the reason sentence a visitor can read.
BLOCKED_BY_LABELS: dict[str, str] = {
    "no_valid_scanner_predictor_direct_selections":
        "no valid scanner → predictor selections this week",
    "no_valid_thinktank_coverage_selections":
        "no valid Think Tank coverage selections this week",
    "scanner_predictor_direct_counterfactual_unavailable":
        "the scanner → predictor counterfactual could not be computed",
    "scanner_top20_predictor_counterfactual_unavailable":
        "the scanner top-20 → predictor counterfactual could not be computed",
    "thinktank_coverage_not_in_leaderboard":
        "Think Tank coverage was not scored in this week's leaderboard",
    "thinktank_coverage_no_resolved_outcomes":
        "Think Tank coverage has no resolved outcomes yet",
    "leaderboard_unavailable": "the scoring leaderboard was unavailable",
    "leaderboard_stale_gt_8d":
        "the newest available leaderboard was more than 8 days old",
    "arm_score_unavailable": "one arm produced no comparable score this week",
    "feed_producer_dead":
        "the winning arm's upstream data feed had stopped producing",
    "thinktank_coverage_thin_evidence":
        "Think Tank coverage was scored on too few dates to compare "
        "(evidence thin)",
    "thinktank_coverage_confidence_unknown":
        "Think Tank coverage evidence carried no confidence rating",
    "scanner_predictor_direct_thin_evidence":
        "scanner → predictor was scored on too few cycles to compare "
        "(evidence thin)",
    "scanner_predictor_direct_confidence_unknown":
        "scanner → predictor evidence carried no confidence rating",
    "scanner_top20_predictor_thin_evidence":
        "scanner top-20 → predictor was scored on too few cycles to compare "
        "(evidence thin)",
    "scanner_top20_predictor_confidence_unknown":
        "scanner top-20 → predictor evidence carried no confidence rating",
    "leaderboard_horizon_mismatch":
        "the leaderboard's primary horizon differed from the horizon this "
        "gate decides on",
    "shadow_only_arm":
        "the arm that won is measured but not eligible for promotion "
        "(ruling 2026-08-20)",
    "frozen": "the run was invoked with --freeze, so no pointer write was made",
    "unclassified_error": "the run could not classify its own failure",
    # Retired pre-2026-07-14 engine slugs — read-tolerated so a historical
    # record still explains itself rather than showing a bare slug.
    "insufficient_matured_cohorts": "not enough matured cohorts (retired engine)",
    "cooldown_active": "a promotion cooldown was active (retired engine)",
    "not_significant_hac_adjusted":
        "the lift was not significant once HAC-adjusted (retired engine)",
    "hysteresis_not_satisfied":
        "the challenger had not won enough consecutive weeks (retired engine)",
    "leaderboard_stale": "the leaderboard was stale (retired engine)",
}

#: Rendered in place of a missing reason. Never an empty string, and never
#: silently omitted — see the module docstring, rule 1.
NO_REASON_RECORDED = (
    "reason not recorded in this artifact — the record states the outcome "
    "but not its cause"
)

#: The two producers this page reads, by the name a visitor is shown when
#: one of them has produced nothing.
CHAMPION_COMPONENT = "Selection-producer promotion audit (weekly)"
MODEL_ZOO_COMPONENT = "Model-zoo promotion leaderboard (weekly)"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def arm_label(arm: Any) -> str:
    """Human label for a selection-producer arm. Unknown slugs pass through;
    a missing arm renders as an em dash rather than the word 'None'."""
    if not isinstance(arm, str) or not arm:
        return "—"
    return CHAMPION_ARM_LABELS.get(arm, arm)


def fmt_score(value: Any, places: int = 4) -> str:
    """A score as a plain number, or an em dash when it is absent.

    Absent is rendered as absent — never as zero, and never with a status
    marker. A bool is not a score (``isinstance(True, int)`` is True in
    Python, and a leaked flag formatted as ``1.0000`` would read as a
    measurement)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{value:.{places}f}"


def verdict_line(verdict: str, reason: str | None) -> str:
    """Join a verdict to its reason. The single chokepoint enforcing rule 1:
    an absent or blank reason becomes :data:`NO_REASON_RECORDED`, so no
    caller can emit a bare verdict."""
    text = (reason or "").strip()
    if not text:
        text = NO_REASON_RECORDED
    return f"{verdict} — {text}"


# ---------------------------------------------------------------------------
# Section 1 — selection-producer champion/challenger ledger
# ---------------------------------------------------------------------------


def champion_blocked_reasons(audit: dict[str, Any]) -> list[str]:
    """Reason sentences for a record's ``blocked_by`` slugs, in order.

    An unmapped slug renders as itself: a slug we have no sentence for is
    still evidence, and dropping it would silently shrink the reason."""
    slugs = audit.get("blocked_by") or []
    if not isinstance(slugs, list):
        return []
    out: list[str] = []
    for slug in slugs:
        if not isinstance(slug, str) or not slug:
            continue
        out.append(BLOCKED_BY_LABELS.get(slug, slug))
    return out


def champion_reason(audit: dict[str, Any]) -> str:
    """The reason sentence for one weekly champion-promotion record.

    Built from, in order of precedence: an ``error`` record's own detail ·
    the shadow-only policy hold (which is a POLICY decision, not an evidence
    failure, and says so) · the ``blocked_by`` slugs · the plain
    score comparison that a promotion or a defended incumbency turns on.
    Returns "" only when the record carries none of those, which
    :func:`verdict_line` then renders as :data:`NO_REASON_RECORDED`.
    """
    if not isinstance(audit, dict) or not audit:
        return ""
    outcome = audit.get("outcome")

    if outcome == "error":
        detail = audit.get("detail")
        blocked = champion_blocked_reasons(audit)
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if blocked:
            return "; ".join(blocked)
        return ""

    blocked = champion_blocked_reasons(audit)

    if outcome == "held_shadow_only":
        winner = audit.get("counterfactual_winner")
        base = (
            "the arm that scored highest this week is measured but not "
            "eligible for promotion, so the live pointer was deliberately "
            "held (ruling 2026-08-20)"
        )
        if isinstance(winner, str) and winner:
            base += f"; it would otherwise have promoted {arm_label(winner)}"
        return base

    if blocked:
        return "; ".join(blocked)

    champ = audit.get("champion_score")
    chall = audit.get("challenger_score")
    champ_txt, chall_txt = fmt_score(champ), fmt_score(chall)
    if champ_txt == "—" and chall_txt == "—":
        return ""

    if outcome == "promoted":
        return (
            f"the challenger scored {chall_txt} against the incumbent's "
            f"{champ_txt} on the weekly gate, so the pointer moved"
        )
    if outcome == "unchanged_winner_already_champion":
        return (
            f"the incumbent scored {champ_txt} against the best challenger's "
            f"{chall_txt}, so the seat was defended"
        )
    return (
        f"incumbent {champ_txt} vs best challenger {chall_txt} on the weekly "
        f"gate"
    )


def champion_verdict(audit: dict[str, Any] | None) -> str:
    """One weekly champion-promotion record as a verdict + its reason.

    Never returns a bare verdict — see :func:`verdict_line`."""
    if not isinstance(audit, dict) or not audit:
        return verdict_line("No promotion audit recorded", None)
    outcome = audit.get("outcome")
    label = OUTCOME_LABELS.get(outcome, str(outcome) if outcome else "Unknown outcome")
    if audit.get("freeze") is True and outcome != "error":
        label += " (run frozen)"
    return verdict_line(label, champion_reason(audit))


def champion_evidence_notes(audit: dict[str, Any] | None) -> list[str]:
    """Per-arm evidence verdicts (``arm_confidence``) as ``"arm: verdict"``.

    Rendered BESIDE a score, never instead of it: a no-contest, a defended
    incumbency and a week whose evidence was too thin to compare all leave
    the pointer where it was, and without this they are indistinguishable.
    Empty on a record that carries no such block — never invented as "ok",
    which would be a claim the artifact does not make."""
    if not isinstance(audit, dict):
        return []
    verdicts = audit.get("arm_confidence")
    if not isinstance(verdicts, dict) or not verdicts:
        return []
    out: list[str] = []
    for arm, verdict in verdicts.items():
        if not isinstance(verdict, str) or not verdict:
            continue
        out.append(f"{arm_label(arm)}: {verdict}")
    return out


def champion_score_rows(audit: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Per-arm score rows for one weekly record.

    Prefers the N-arm ``arm_scores`` block; falls back to the incumbent /
    best-challenger pair on a record written before that field existed. Each
    row carries the arm, its role this week, and its score as a number — no
    status, no colour, no ranking badge."""
    if not isinstance(audit, dict) or not audit:
        return []
    before = audit.get("champion_before")
    challenger = audit.get("challenger")
    scores = audit.get("arm_scores")

    def _role(arm: str) -> str:
        if arm == before:
            return "champion (incumbent)"
        if arm == challenger:
            return "challenger (best)"
        return "challenger"

    rows: list[dict[str, Any]] = []
    if isinstance(scores, dict) and scores:
        for arm in sorted(scores):
            rows.append({
                "Arm": arm_label(arm),
                "Role this week": _role(arm),
                "Weekly gate score": fmt_score(scores.get(arm)),
            })
        return rows

    for arm, role, value in (
        (before, "champion (incumbent)", audit.get("champion_score")),
        (challenger, "challenger (best)", audit.get("challenger_score")),
    ):
        if not isinstance(arm, str) or not arm:
            continue
        rows.append({
            "Arm": arm_label(arm),
            "Role this week": role,
            "Weekly gate score": fmt_score(value),
        })
    return rows


def champion_history_rows(audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Multi-week promotion ledger, one row per recorded weekly decision.

    Each row carries its own reason: the ledger is the record of decisions,
    and a decision without its reason is exactly the bare-red-dot failure
    mode this page exists to avoid."""
    rows: list[dict[str, Any]] = []
    for audit in audits:
        if not isinstance(audit, dict) or not audit:
            continue
        outcome = audit.get("outcome")
        rows.append({
            "Week": audit.get("date") or "—",
            "Decision": OUTCOME_LABELS.get(
                outcome, str(outcome) if outcome else "Unknown"
            ),
            "Champion after": arm_label(audit.get("champion_after")),
            "Why": champion_reason(audit) or NO_REASON_RECORDED,
        })
    return rows


# ---------------------------------------------------------------------------
# Section 2 — model-zoo (M-slot) weekly promotion leaderboard
# ---------------------------------------------------------------------------

#: Candidate fields rendered on this surface. An allowlist, not a denylist:
#: a new producer-side field cannot leak onto a public page by being added
#: upstream (architecture.d/069 point 4 is enforced by what we name, not by
#: what we remember to strip).
MODEL_ZOO_CANDIDATE_COLUMNS: dict[str, str] = {
    "spec_id": "Candidate",
    "version_id": "Version",
    "forward_days": "Horizon (days)",
    "cpcv_mean_ic": "Out-of-sample CPCV mean IC",
    "passes_gate": "Passes gate",
    "eligible": "Eligible",
    "reason": "Why",
}


def model_zoo_reason(leaderboard: dict[str, Any]) -> str:
    """The reason sentence for one weekly model-zoo rotation.

    A rotation that promoted nothing is the ordinary case and is stated as a
    decision, not an absence: observe mode is a deliberate no-cutover, and a
    cleared-nobody week means no candidate beat the champion architecture by
    the required margin."""
    if not isinstance(leaderboard, dict) or not leaderboard:
        return ""
    if leaderboard.get("promoted"):
        kind = leaderboard.get("promoted_kind")
        base = (
            f"the winning candidate cleared the champion architecture by the "
            f"required margin ({fmt_score(leaderboard.get('margin'))})"
        )
        if isinstance(kind, str) and kind:
            base += f"; promotion kind: {kind}"
        reverted = leaderboard.get("reverted_from")
        if isinstance(reverted, str) and reverted:
            base += f"; replaced {reverted}"
        return base
    if leaderboard.get("mode") == "observe":
        return (
            "observe mode — the rotation ranked every candidate but is not "
            "permitted to cut over to a live model this cycle"
        )
    return (
        "no candidate cleared the champion architecture by the required "
        f"margin ({fmt_score(leaderboard.get('margin'))}), so the serving "
        "champion was retrained and kept"
    )


def model_zoo_verdict(leaderboard: dict[str, Any] | None) -> str:
    """One weekly model-zoo rotation as a verdict + its reason."""
    if not isinstance(leaderboard, dict) or not leaderboard:
        return verdict_line("No rotation recorded", None)
    promoted = leaderboard.get("promoted")
    if promoted:
        verdict = f"Promoted {promoted}"
    else:
        verdict = "Held — no promotion this cycle"
    return verdict_line(verdict, model_zoo_reason(leaderboard))


def model_zoo_candidate_rows(leaderboard: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Candidate rows for one rotation, restricted to
    :data:`MODEL_ZOO_CANDIDATE_COLUMNS`.

    A candidate whose record carries no ``reason`` gets
    :data:`NO_REASON_RECORDED` rather than a blank cell: the gate verdict
    (``passes_gate``) never renders on its own."""
    if not isinstance(leaderboard, dict) or not leaderboard:
        return []
    winner = leaderboard.get("winner_version_id")
    rows: list[dict[str, Any]] = []
    for cand in leaderboard.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        row: dict[str, Any] = {}
        for field, label in MODEL_ZOO_CANDIDATE_COLUMNS.items():
            value = cand.get(field)
            if field == "cpcv_mean_ic":
                row[label] = fmt_score(value)
            elif field == "reason":
                row[label] = (
                    value.strip()
                    if isinstance(value, str) and value.strip()
                    else NO_REASON_RECORDED
                )
            elif field in ("passes_gate", "eligible"):
                row[label] = (
                    "yes" if value is True else "no" if value is False else "—"
                )
            else:
                row[label] = value if value not in (None, "") else "—"
        row["Winner"] = (
            "yes"
            if winner is not None and cand.get("version_id") == winner
            else "no"
        )
        rows.append(row)
    return rows


def model_zoo_history_rows(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Multi-week model-zoo promotion ledger from
    ``load_model_zoo_history`` rows."""
    rows: list[dict[str, Any]] = []
    for rec in history or []:
        if not isinstance(rec, dict):
            continue
        rows.append({
            "Week": rec.get("date") or "—",
            "Mode": rec.get("mode") or "—",
            "Baseline CPCV mean IC": fmt_score(rec.get("baseline_ic")),
            "Winner CPCV mean IC": fmt_score(rec.get("winner_ic")),
            "Margin": fmt_score(rec.get("margin")),
            "Candidates": rec.get("n_candidates", "—"),
            "Eligible": rec.get("n_eligible", "—"),
            "Decision": (
                f"promoted {rec.get('promoted')}"
                if rec.get("promoted")
                else "held"
            ),
        })
    return rows


def model_zoo_estimator_note(leaderboard: dict[str, Any] | None) -> str:
    """The estimator/sample sentence printed beside the CPCV numbers.

    Point 1 of architecture.d/069 requires a figure to carry its estimator
    and N. Names the baseline source when the artifact records one, so the
    "beat a NAMED baseline" claim is checkable rather than asserted."""
    lb = leaderboard if isinstance(leaderboard, dict) else {}
    source = lb.get("promotion_baseline_source")
    n_cand = len([c for c in (lb.get("candidates") or []) if isinstance(c, dict)])
    parts = [
        "Estimator: mean information coefficient over leak-free combinatorial "
        "purged cross-validation folds, measured out of sample.",
        f"Candidates scored this cycle: {n_cand}.",
    ]
    if isinstance(source, str) and source:
        parts.append(f"Baseline the gate compares against: {source}.")
    else:
        parts.append("Baseline source not recorded in this artifact.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Awaiting-data and staleness
# ---------------------------------------------------------------------------


def missing_components(
    champion_audit: dict[str, Any] | None,
    model_zoo_leaderboard: dict[str, Any] | None,
) -> list[str]:
    """Names of the producers that have emitted nothing.

    principles.md §2.7: a component emitting nothing is unobserved, not
    healthy. The page renders this list verbatim; it never renders an empty
    panel, and never renders absence as success."""
    missing: list[str] = []
    if not isinstance(champion_audit, dict) or not champion_audit:
        missing.append(CHAMPION_COMPONENT)
    if not isinstance(model_zoo_leaderboard, dict) or not model_zoo_leaderboard:
        missing.append(MODEL_ZOO_COMPONENT)
    return missing


def _parse_iso(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


#: Both producers write weekly (Saturday). Two missed cycles plus slack is
#: the point at which "the last record is old" stops being ordinary.
STALE_AFTER_DAYS = 16


def staleness_note(
    component: str,
    measured_write_date: Any,
    payload_date: Any = None,
    today: date | None = None,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> str | None:
    """A sentence when the newest record for *component* is old, else None.

    Ages the MEASURED S3 write date, falling back to the payload's own date
    only when no measured date is available — and saying which it used. A
    payload date is producer-asserted and freezes silently when the writer
    stops, so a page that ages it alone reads a dead producer as current.
    """
    today = today or date.today()
    measured = _parse_iso(measured_write_date)
    asserted = _parse_iso(payload_date)
    basis = measured or asserted
    if basis is None:
        return (
            f"{component}: the age of the newest record could not be "
            f"determined — treat what is shown below as of unknown vintage."
        )
    age = (today - basis).days
    if age <= stale_after_days:
        return None
    source = (
        "measured write date"
        if measured is not None
        else "the record's own self-reported date (no measured write date "
        "was available)"
    )
    return (
        f"{component}: the newest record is {age} days old by {source} "
        f"({basis.isoformat()}). This producer writes weekly, so it has "
        f"missed at least one cycle — what is shown below is that last "
        f"record, not a current one."
    )
