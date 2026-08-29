"""Scanner champion/challenger slots — the console's view model
(alpha-engine-config-I9278).

WHY THIS MODULE EXISTS
----------------------
The scanner-cut slot writes a decision record on EVERY weekly evaluation,
promote or hold, and until now nothing rendered any of them. A run of 52
consecutive ``hold`` decisions and a run of 52 silent failures were therefore
indistinguishable to a reader — ``principles.md`` §2.7's "a component emitting
nothing is not healthy, it is unobserved", at the one slot whose whole purpose
is to decide which universe cut feeds the sector teams.

This module turns the recorded artifacts into rows. It computes nothing the
engine did not already decide (crucible-dashboard AGENTS.md: *the dashboard is
a view, not a measurement layer*) — with one deliberate exception, the
LIVENESS CLAIM below, which is a fact about the artifact's own age and can only
be made by the reader.

THREE THINGS THIS MODULE REFUSES TO DO
--------------------------------------
1. **Render a v1 record under v2 field labels.** Records before 2026-08-28
   carry ``horizon_days`` / ``primary_metric`` / ``topn_alpha_vs_population_mean``
   — a 126-session forward-window mean. v2 carries ``decision_metric`` /
   ``mean_paired_log_return`` — a paired weekly net difference against the
   serving champion. Those are different numbers about different things, and
   the version was bumped precisely so a reader could not confuse them
   (contract ``description``, alpha-engine-config-I8261). :func:`arm_table`
   therefore derives its columns from the RECORD'S OWN version and never from
   a union, so a v1 number can never appear beneath a v2 label.
2. **Render ``None`` as ``0``.** "Produced no comparable evidence" and "scored
   zero" are different facts (champion-challenger-policy.md §3). A null renders
   as an em dash.
3. **Resolve the arm list from a constant.** The promotable set has already
   changed twice (alpha-engine-config-I8060 shrank it, I9278's sibling work
   widens it again) and a hardcoded arm list in this repo is exactly the defect
   crucible-dashboard-PR803 fixed on the neighbouring ablation page. Arms come
   from the record's own ``arms`` block and from the ledger's own ``arm``
   column, always.

SLOTS ARE DATA, NOT CODE PATHS
------------------------------
:data:`SLOTS` binds each slot to the keys its facts already live at
(``console-policy.md`` §2.6 — the per-component descriptor points at where the
facts are; §2.7 — one driver per source shape, here the object store). The spec
slot (alpha-engine-config-I9273, producer ``crucible-research
scoring/spec_promotion.py``) has written nothing yet: it renders NEVER_RAN with
its keys named, and it starts rendering its records with NO further change to
this repo the moment the producer writes one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Slot bindings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotSpec:
    """Where one champion/challenger slot's facts already live."""

    slot_id: str
    label: str
    pointer_key: str
    audit_prefix: str
    producer: str
    what_it_decides: str


SLOTS: tuple[SlotSpec, ...] = (
    SlotSpec(
        slot_id="scanner_cut",
        label="Scanner cut",
        pointer_key="config/scanner_cut_champion.json",
        audit_prefix="config/apply_audit/scanner_cut_champion/",
        producer="crucible-research/scoring/cut_promotion.py",
        what_it_decides=(
            "which universe cut the sector-team feed resolves from "
            "(scoring/universe_membership.py::live_cut_champion)"
        ),
    ),
    SlotSpec(
        slot_id="scanner_spec",
        label="Scanner spec",
        pointer_key="config/scanner_spec_champion.json",
        audit_prefix="config/apply_audit/scanner_spec_champion/",
        producer="crucible-research/scoring/spec_promotion.py",
        what_it_decides="which scanner spec builds the candidate universe",
    ),
)

# The liveness bound. The slot evaluates weekly, so anything past 8 days has
# missed at least one scheduled evaluation. Mirrors the `leaderboard_stale_gt_8d`
# bound the neighbouring promotion engine already uses, deliberately: one
# staleness bound for one weekly cadence.
STALE_AFTER_DAYS = 8

LEDGER_KEY = "research/cuts_weekly_ledger/ledger.parquet"

# ---------------------------------------------------------------------------
# Reason-code disposition
# ---------------------------------------------------------------------------
#
# Source of truth: crucible-research/contracts/scanner_cut_champion.schema.json
# (`reason_code.enum`, v2) plus the v1 slugs the same file records as RETIRED
# and read-tolerated. A hold is an OUTCOME; most reason codes describe an
# expected steady state and MUST NOT render as a warning, or the pane trains
# its reader to ignore it — which is how a real defect gets missed.

_NORMAL_REASON_CODES: frozenset[str] = frozenset({
    # v2 (current)
    "promoted",
    "champion_already_leads",
    "no_promotable_challenger",   # registry state — one promotable arm
    "weekly_ledger_missing",      # expected until the ledger producer is wired
    "weekly_ledger_arm_missing",
    "insufficient_weeks",         # expected for ~5 weeks after that
    "margin_not_met",
    "cooldown_active",
    "corroborating_horizon_disagrees",
    # v1 (RETIRED, never re-minted — read-tolerated so history stays readable)
    "board_missing",
    "board_unmeasurable",
    "decision_horizon_immature",
    "decision_horizon_unmeasurable",
    "arm_row_missing",
    "arm_metric_missing",
    "insufficient_dates",
})

# The ONLY defect slug. The record is written FIRST and the engine then raises,
# so a `board_defective` record is evidence of a run that failed — not of a
# hold (contract: "a defect must never erase the evidence of itself").
_DEFECT_REASON_CODES: frozenset[str] = frozenset({"board_defective"})


def reason_code_disposition(reason_code: Any) -> str:
    """``"normal"`` | ``"defect"`` | ``"unrecognised"``.

    An unknown slug is NEVER quietly folded into "normal": a new reason code
    the console has not seen is a fact about the producer, and §5.8 counts an
    undeclared value rather than dropping it."""
    if reason_code in _DEFECT_REASON_CODES:
        return "defect"
    if reason_code in _NORMAL_REASON_CODES:
        return "normal"
    return "unrecognised"


# ---------------------------------------------------------------------------
# Schema versions — the v1/v2 firewall
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """A declared field: name, label, unit and one render hint from
    console-policy.md §5.8's closed vocabulary."""

    key: str
    label: str
    unit: str
    render: str  # value | duration | bytes | ratio | count | timeseries | link | text


_RENDER_VOCABULARY = frozenset(
    {"value", "duration", "bytes", "ratio", "count", "timeseries", "link", "text"}
)

# Per-version ARM field sets. Deliberately disjoint where the meaning changed:
# a v1 record has no `mean_paired_log_return` and a v2 record has no
# `topn_alpha_vs_population_mean`, and neither is ever rendered under the
# other's label.
_ARM_FIELDS: dict[int, tuple[FieldSpec, ...]] = {
    1: (
        FieldSpec("n_dates_scored", "Cohort dates scored", "count", "count"),
        FieldSpec(
            "topn_alpha_vs_population_mean",
            "Top-N alpha vs population (mean)",
            "log-return over the record's horizon_days forward window",
            "ratio",
        ),
        FieldSpec("t_stat", "t-stat (iid SE, overlapping windows)", "t", "value"),
        FieldSpec("horizon_days", "Horizon", "sessions", "count"),
        FieldSpec("confidence", "Evidence", "ok | thin | insufficient", "text"),
    ),
    2: (
        FieldSpec("n_weeks_scored", "Weeks scored", "count", "count"),
        FieldSpec("n_weeks_paired", "Weeks paired", "count", "count"),
        FieldSpec(
            "mean_paired_log_return",
            "Mean paired net log-return vs champion",
            "log-return per week, net of transaction cost",
            "ratio",
        ),
        FieldSpec(
            "chained_paired_log_return",
            "Chained paired log-return",
            "log-return over first_week→last_week",
            "ratio",
        ),
        FieldSpec("t_stat", "t-stat (clustered SE, abutting weeks)", "t", "value"),
        FieldSpec("confidence", "Evidence", "ok | thin | insufficient", "text"),
    ),
}

# Per-version DECISION-LEVEL fields that describe the decision BASIS. The two
# versions answer "what was this decided on?" with different fields, and that
# is the whole content of the v1→v2 bump.
_BASIS_FIELDS: dict[int, tuple[FieldSpec, ...]] = {
    1: (
        FieldSpec("primary_metric", "Decision metric (v1)", "metric name", "text"),
        FieldSpec("horizon_days", "Decision horizon", "sessions", "count"),
    ),
    2: (
        FieldSpec("decision_metric", "Decision metric (v2)", "metric name", "text"),
        FieldSpec("decision_cadence", "Cadence", "cadence name", "text"),
        FieldSpec("decision_source", "Decision source", "S3 key", "text"),
        FieldSpec("decision_column", "Decision column", "ledger column", "text"),
    ),
}

_VERSION_NOTE: dict[int, str] = {
    1: (
        "schema v1 — decided on a 126-session forward-window mean "
        "(topn_alpha_vs_population_mean). NOT comparable to a v2 record's "
        "mean_paired_log_return, which is a paired weekly net difference "
        "against the serving champion (alpha-engine-config-I8261)."
    ),
    2: (
        "schema v2 — decided on the chained weekly paired net log-return vs "
        "the serving champion, from research/cuts_weekly_ledger/ledger.parquet. "
        "126/252-session horizons hold a VETO only."
    ),
}

KNOWN_SCHEMA_VERSIONS: tuple[int, ...] = (1, 2)


def record_schema_version(record: Any) -> int | None:
    """The record's own declared ``schema_version``, or None when it carries
    none. Never guessed from which fields happen to be present — a record whose
    version cannot be read is rendered as unversioned, not as the latest."""
    if not isinstance(record, dict):
        return None
    version = record.get("schema_version")
    return version if isinstance(version, int) else None


def schema_version_note(version: int | None) -> str:
    if version in _VERSION_NOTE:
        return _VERSION_NOTE[version]
    return (
        f"schema v{version} — this console has no field descriptors for this "
        "version. Fields are rendered under their RAW names so nothing is "
        "dropped and nothing is relabelled (console-policy.md §5.8)."
        if version is not None
        else "no schema_version on this record — fields rendered under their "
        "RAW names. A record whose version cannot be read is not assumed to be "
        "the latest."
    )


def basis_fields(version: int | None) -> tuple[FieldSpec, ...]:
    """Decision-basis descriptors for exactly this record's version."""
    return _BASIS_FIELDS.get(version, ()) if version is not None else ()


def arm_fields(version: int | None) -> tuple[FieldSpec, ...]:
    """Arm-level descriptors for exactly this record's version."""
    return _ARM_FIELDS.get(version, ()) if version is not None else ()


# ---------------------------------------------------------------------------
# Rendering primitives
# ---------------------------------------------------------------------------

EM_DASH = "—"


def fmt_optional(value: Any, *, digits: int = 6) -> str:
    """``None`` renders as an em dash, ALWAYS.

    A null score means "produced no comparable evidence"; ``0`` means "scored
    zero". Collapsing them is the fleet's dominant bug class wearing a
    formatter's clothes (champion-challenger-policy.md §7.2)."""
    if value is None:
        return EM_DASH
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if pd.isna(value):
            return EM_DASH
        return f"{value:+.{digits}f}"
    if isinstance(value, int):
        return f"{value:d}"
    text = str(value).strip()
    return text if text else EM_DASH


# ---------------------------------------------------------------------------
# The liveness claim
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Liveness:
    """A claim about whether the ENGINE is alive, distinct from what it decided.

    ``state`` is one member of observability-policy.md §8.3's closed
    vocabulary. There is no fall-through and no ``UNKNOWN``: a slot the
    classifier cannot place is ``UNREPORTED``, which is a finding."""

    state: str
    as_of: str | None
    as_of_source: str
    age_days: int | None
    headline: str
    detail: str
    source_key: str


def _parse_as_of(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def liveness(
    slot: SlotSpec,
    *,
    latest: Any,
    dated_dates: list[str] | None,
    latest_written_at: str | None,
    now: datetime,
) -> Liveness:
    """Resolve one slot to exactly one §8.3 state.

    The ordering matters, and each branch is a DIFFERENT fact:

    * nothing written at all             → ``NEVER_RAN``  (the spec slot today)
    * dated records exist, no latest.json → ``UNREPORTED`` (the liveness key the
      engine is contracted to write is missing — the freshness proxy is dark,
      so no age claim can be made at all)
    * latest.json present, no readable as-of → ``UNREPORTED`` (present but it
      cannot say when — an unbounded claim about now)
    * as-of older than STALE_AFTER_DAYS   → ``MISSED``     (a weekly evaluation
      that should have fired and did not; RED, WITH the age)
    * reason_code is a defect slug        → ``FAILED``     (the record was
      written and the engine then raised)
    * otherwise                           → ``HEALTHY``

    Note what is NOT here: no branch reaches ``HEALTHY`` from the absence of
    evidence. Four states mean absence and each renders as itself."""
    key = f"{slot.audit_prefix}latest.json"
    dates = dated_dates or []
    has_latest = isinstance(latest, dict) and bool(latest)

    if not has_latest and not dates:
        return Liveness(
            state="NEVER_RAN",
            as_of=None,
            as_of_source="none",
            age_days=None,
            headline=f"{slot.label}: no records yet",
            detail=(
                f"Neither {slot.pointer_key} nor any key under {slot.audit_prefix} "
                f"exists. The producer ({slot.producer}) has never written a "
                "decision record. This is an absence, not a hold and not a pass — "
                "the pane starts rendering this slot's decisions with no console "
                "change the moment the first record lands."
            ),
            source_key=key,
        )

    if not has_latest:
        return Liveness(
            state="UNREPORTED",
            as_of=None,
            as_of_source="none",
            age_days=None,
            headline=f"{slot.label}: {len(dates)} dated record(s), but latest.json is absent",
            detail=(
                f"{key} does not exist, so the slot's freshness proxy is dark and "
                "NO age claim can be made. The newest dated record is "
                f"{dates[-1]} — which says when a record was last written, not "
                "that the engine is still running. Producer: "
                f"{slot.producer}."
            ),
            source_key=key,
        )

    stamp = _parse_as_of(latest.get("generated_at"))
    source = "record.generated_at"
    if stamp is None:
        stamp = _parse_as_of(latest.get("decided_on"))
        source = "record.decided_on"
    if stamp is None:
        stamp = _parse_as_of(latest_written_at)
        source = "s3.LastModified"
    if stamp is None:
        return Liveness(
            state="UNREPORTED",
            as_of=None,
            as_of_source="none",
            age_days=None,
            headline=f"{slot.label}: latest.json carries no readable as-of",
            detail=(
                f"{key} exists but neither generated_at nor decided_on parsed, and "
                "S3 LastModified was unavailable. Age cannot be bounded, so the "
                "record cannot support a claim about now."
            ),
            source_key=key,
        )

    # Age is compared on the EXACT elapsed span and displayed as whole days.
    # Comparing on the floored value would let a record 8.9 days old clear an
    # 8-day bound — a staleness guard that under-reports at exactly the moment
    # it matters is the guard-that-cannot-fail shape
    # (champion-challenger-policy.md §7.4).
    elapsed_days = (now.astimezone(timezone.utc) - stamp).total_seconds() / 86400.0
    age_days = int(elapsed_days // 1)
    as_of = stamp.isoformat()

    if elapsed_days > STALE_AFTER_DAYS:
        return Liveness(
            state="MISSED",
            as_of=as_of,
            as_of_source=source,
            age_days=age_days,
            headline=(
                f"{slot.label}: STALE — last decision {age_days} days ago "
                f"(bound is {STALE_AFTER_DAYS} days)"
            ),
            detail=(
                f"{key} is {elapsed_days:.1f} days old (as-of {as_of}, from "
                f"{source}). The "
                "slot evaluates weekly, so at least one evaluation has not run. A "
                "dead engine must not read as an engine that held — this is the "
                "condition this key exists to expose. Producer: "
                f"{slot.producer}."
            ),
            source_key=key,
        )

    reason_code = latest.get("reason_code")
    if reason_code_disposition(reason_code) == "defect":
        return Liveness(
            state="FAILED",
            as_of=as_of,
            as_of_source=source,
            age_days=age_days,
            headline=f"{slot.label}: DEFECT — reason_code={reason_code}",
            detail=(
                "The record was written and the engine then raised. "
                f"defect={fmt_optional(latest.get('defect'))}. "
                f"As-of {as_of} ({age_days}d old, from {source})."
            ),
            source_key=key,
        )

    return Liveness(
        state="HEALTHY",
        as_of=as_of,
        as_of_source=source,
        age_days=age_days,
        headline=(
            f"{slot.label}: live — decided {fmt_optional(latest.get('decided_on'))} "
            f"({age_days}d ago), {fmt_optional(latest.get('decision'))}"
        ),
        detail=(
            f"reason_code={fmt_optional(reason_code)} "
            f"({reason_code_disposition(reason_code)}). "
            f"As-of {as_of} from {source}; exact age {elapsed_days:.1f} days, "
            f"bound is {STALE_AFTER_DAYS} days."
        ),
        source_key=key,
    )


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

DECISION_COLUMNS: tuple[str, ...] = (
    "decided_on",
    "champion_before",
    "champion",
    "decision",
    "reason_code",
    "reason_code_disposition",
    "last_promoted_on",
    "schema_version",
)


def decision_row(record: Any) -> dict[str, Any]:
    """One promote/hold cycle, flattened.

    Every field here exists identically on v1 and v2 — which is why the series
    table can span the version boundary at all. Anything version-dependent
    lives in :func:`arm_table` and :func:`basis_row`, never here."""
    if not isinstance(record, dict):
        return {c: None for c in DECISION_COLUMNS}
    return {
        "decided_on": record.get("decided_on"),
        "champion_before": record.get("champion_before"),
        "champion": record.get("champion"),
        "decision": record.get("decision"),
        "reason_code": record.get("reason_code"),
        "reason_code_disposition": reason_code_disposition(record.get("reason_code")),
        "last_promoted_on": record.get("last_promoted_on"),
        "schema_version": record_schema_version(record),
    }


def decision_series(records: list[Any]) -> pd.DataFrame:
    """Newest first. An empty input returns an EMPTY frame with the columns
    present — a table with no rows is a different render from no table."""
    rows = [decision_row(r) for r in records if isinstance(r, dict)]
    frame = pd.DataFrame(rows, columns=list(DECISION_COLUMNS))
    if not frame.empty:
        frame = frame.sort_values("decided_on", ascending=False, na_position="last")
    return frame.reset_index(drop=True)


def basis_row(record: Any) -> list[tuple[str, str, str]]:
    """``(label, value, unit)`` for the decision BASIS, from the record's own
    version's descriptors only. A v1 record yields v1 labels; a record of an
    unknown version yields its raw keys rather than a guess."""
    if not isinstance(record, dict):
        return []
    version = record_schema_version(record)
    specs = basis_fields(version)
    if specs:
        return [(s.label, fmt_optional(record.get(s.key)), s.unit) for s in specs]
    # Unknown version: render every non-container top-level field under its RAW
    # name. Undeclared renders as opaque text and is counted, never dropped.
    return [
        (key, fmt_optional(value), "undeclared")
        for key, value in sorted(record.items())
        if not isinstance(value, (dict, list))
    ]


def arm_table(record: Any) -> tuple[pd.DataFrame, tuple[FieldSpec, ...], int | None]:
    """Per-arm evidence for ONE record, in that record's OWN field vocabulary.

    Returns ``(frame, specs, version)``. The frame's columns are the descriptor
    LABELS, so a v1 number is structurally incapable of appearing under a v2
    label: they are built from ``_ARM_FIELDS[version]`` and nothing merges the
    two vocabularies.

    Arms are resolved from ``record["arms"]`` — never from a constant in this
    repo. Two arms become promotable shortly that are not today; the pane picks
    them up because it never had a list to update."""
    version = record_schema_version(record) if isinstance(record, dict) else None
    specs = arm_fields(version)
    arms = record.get("arms") if isinstance(record, dict) else None
    if not isinstance(arms, dict) or not arms:
        columns = ["Arm", "Champion", "Present"] + [s.label for s in specs]
        return pd.DataFrame(columns=columns), specs, version

    if not specs:
        # Unknown/absent schema_version — render the arm blocks under their RAW
        # keys, union'd across arms, so no field is silently dropped.
        raw_keys: list[str] = []
        for block in arms.values():
            if isinstance(block, dict):
                for k in block:
                    if k not in raw_keys:
                        raw_keys.append(k)
        rows = [
            {"Arm": name, **{k: fmt_optional((block or {}).get(k)) for k in raw_keys}}
            for name, block in sorted(arms.items())
            if isinstance(block, dict)
        ]
        return pd.DataFrame(rows, columns=["Arm", *raw_keys]), specs, version

    rows = []
    for name, block in sorted(arms.items()):
        block = block if isinstance(block, dict) else {}
        row = {
            "Arm": name,
            "Champion": fmt_optional(block.get("is_champion")),
            "Present": fmt_optional(block.get("present")),
        }
        for spec in specs:
            row[spec.label] = fmt_optional(block.get(spec.key))
        rows.append(row)
    columns = ["Arm", "Champion", "Present"] + [s.label for s in specs]
    return pd.DataFrame(rows, columns=columns), specs, version


# ---------------------------------------------------------------------------
# Weekly ledger
# ---------------------------------------------------------------------------

# The columns the pane names explicitly (issue deliverable 1). Every OTHER
# column present on the parquet is still rendered — in the raw expander, under
# its own name — because a dropped field is a fact the producer believes is on
# the surface and is not (console-policy.md §5.8).
LEDGER_DECLARED_COLUMNS: tuple[FieldSpec, ...] = (
    FieldSpec("arm", "Arm", "arm id", "text"),
    FieldSpec("week_start", "Week start", "date", "text"),
    FieldSpec("week_end", "Week end", "date", "text"),
    FieldSpec("n_names", "Names held", "count", "count"),
    FieldSpec("turnover_frac", "Turnover", "fraction of the basket", "ratio"),
    FieldSpec("gross_log_return", "Gross", "log-return over the held week", "ratio"),
    FieldSpec("net_log_return", "Net", "log-return, after transaction cost", "ratio"),
    FieldSpec(
        "net_unavailable_reason",
        "Why net is absent",
        "reason slug",
        "text",
    ),
)


def ledger_view(frame: pd.DataFrame | None) -> tuple[pd.DataFrame, list[str]]:
    """``(declared-column view, columns the parquet has that are undeclared)``.

    A ``net_log_return`` of ``None`` is NEVER filled from ``gross_log_return``
    — the producer writes a ``net_unavailable_reason`` beside it for exactly
    that reason, and the two claims are rendered side by side so the reader can
    see which one this row is making."""
    if frame is None:
        return pd.DataFrame(columns=[s.label for s in LEDGER_DECLARED_COLUMNS]), []
    present = [s for s in LEDGER_DECLARED_COLUMNS if s.key in frame.columns]
    undeclared = [c for c in frame.columns if c not in {s.key for s in LEDGER_DECLARED_COLUMNS}]
    if frame.empty:
        return pd.DataFrame(columns=[s.label for s in present]), undeclared
    view = frame.loc[:, [s.key for s in present]].copy()
    view.columns = [s.label for s in present]
    sort_cols = [c for c in ("Week start", "Arm") if c in view.columns]
    if sort_cols:
        view = view.sort_values(sort_cols, ascending=[False] + [True] * (len(sort_cols) - 1))
    return view.reset_index(drop=True), undeclared


def ledger_arms(frame: pd.DataFrame | None) -> list[str]:
    """The arms the ledger itself carries — resolved from the artifact, never
    from a constant."""
    if frame is None or frame.empty or "arm" not in frame.columns:
        return []
    return sorted({str(a) for a in frame["arm"].dropna().tolist()})


def _assert_render_hints() -> None:
    """Every descriptor's render hint is inside §5.8's closed vocabulary.

    Checked at import rather than in a test alone: a hint outside the
    vocabulary is how a descriptor set quietly becomes a plugin system."""
    for specs in (*_ARM_FIELDS.values(), *_BASIS_FIELDS.values(), LEDGER_DECLARED_COLUMNS):
        for spec in specs:
            if spec.render not in _RENDER_VOCABULARY:
                raise ValueError(
                    f"render hint {spec.render!r} on field {spec.key!r} is outside "
                    f"console-policy.md §5.8's closed vocabulary {sorted(_RENDER_VOCABULARY)}"
                )
            if not spec.unit:
                raise ValueError(f"field {spec.key!r} declares no unit")


_assert_render_hints()


# ---------------------------------------------------------------------------
# The machine-readable projection (console-policy.md §3.8)
# ---------------------------------------------------------------------------
#
# ONE query, two renderings. The pane renders exactly this structure and serves
# it verbatim, so an agent reading the JSON and an operator reading the page
# cannot diverge — and they diverge exactly when something is wrong, which is
# when it matters.

VIEW_SCHEMA_VERSION = 1
DECISION_HISTORY_LIMIT = 8


def build_slot_view(
    slot: SlotSpec,
    *,
    pointer: Any,
    latest: Any,
    latest_written_at: str | None,
    dated_dates: list[str] | None,
    records: list[Any],
    now: datetime,
) -> dict[str, Any]:
    """Pure projection of one slot's already-fetched artifacts.

    No I/O, and no default substituted for a missing artifact: every absence is
    carried through as an explicit ``present: false`` plus the state that says
    which KIND of absence it is."""
    claim = liveness(
        slot,
        latest=latest,
        dated_dates=dated_dates,
        latest_written_at=latest_written_at,
        now=now,
    )
    live_record = pointer if isinstance(pointer, dict) and pointer else None
    version = record_schema_version(live_record)
    arms_block = (live_record or {}).get("arms")
    arms_json: dict[str, Any] = {}
    if isinstance(arms_block, dict):
        specs = arm_fields(version)
        for name, block in arms_block.items():
            block = block if isinstance(block, dict) else {}
            if specs:
                entry = {
                    "is_champion": block.get("is_champion"),
                    "present": block.get("present"),
                }
                for spec in specs:
                    entry[spec.key] = block.get(spec.key)
                arms_json[name] = entry
            else:
                arms_json[name] = dict(block)
    return {
        "schema_version": VIEW_SCHEMA_VERSION,
        "slot_id": slot.slot_id,
        "producer": slot.producer,
        "decides": slot.what_it_decides,
        "sources": {
            "pointer": slot.pointer_key,
            "audit_prefix": slot.audit_prefix,
            "liveness": claim.source_key,
        },
        "liveness": {
            "state": claim.state,
            "as_of": claim.as_of,
            "as_of_source": claim.as_of_source,
            "age_days": claim.age_days,
            "stale_after_days": STALE_AFTER_DAYS,
            "headline": claim.headline,
            "detail": claim.detail,
        },
        "pointer": {
            "present": live_record is not None,
            "record_schema_version": version,
            "record_schema_note": schema_version_note(version),
            "champion": (live_record or {}).get("champion"),
            "champion_before": (live_record or {}).get("champion_before"),
            "decided_on": (live_record or {}).get("decided_on"),
            "decision": (live_record or {}).get("decision"),
            "reason_code": (live_record or {}).get("reason_code"),
            "reason_code_disposition": reason_code_disposition(
                (live_record or {}).get("reason_code")
            ),
            "decision_earliest_on": (live_record or {}).get("decision_earliest_on"),
        },
        "arms": arms_json,
        "decisions": [
            dict(
                decision_row(r),
                record_schema_note=schema_version_note(record_schema_version(r)),
            )
            for r in records[:DECISION_HISTORY_LIMIT]
        ],
        "decision_count_listed": len(dated_dates or []),
    }


def load_slot_view(slot: SlotSpec, *, now: datetime | None = None) -> dict[str, Any]:
    """Fetch one slot's artifacts and project them. The only I/O entrypoint."""
    from loaders import s3_loader  # local import keeps this module import-light

    now = now or datetime.now(timezone.utc)
    dated = s3_loader.list_slot_audit_dates(slot.audit_prefix)
    newest = list(reversed(dated))[:DECISION_HISTORY_LIMIT]
    records = [s3_loader.load_slot_audit(slot.audit_prefix, d) for d in newest]
    return build_slot_view(
        slot,
        pointer=s3_loader.load_slot_champion_pointer(slot.pointer_key),
        latest=s3_loader.load_slot_audit_latest(slot.audit_prefix),
        latest_written_at=s3_loader.load_slot_audit_latest_written_at(slot.audit_prefix),
        dated_dates=dated,
        records=[r for r in records if isinstance(r, dict)],
        now=now,
    )
