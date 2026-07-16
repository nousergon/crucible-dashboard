"""
data_integrity_status.py — pure rollup logic for the Data Integrity status tile.

Market-value integrity is a phased framework (alpha-engine-config#1277):

  L1 — cross-source agreement observer (SHIPPED, nousergon-data#728,
       2026-07-10): ``collectors/cross_source_observer.py`` additively
       annotates the daily_closes parquet with ``xsource_status`` /
       ``xsource_flagged`` / ``xsource_agreement_bps`` / ``xsource_provenance``
       for each settled cell. Observer-mode only — it flags disagreement, it
       does not withhold or override a value.
  L2 — data-quality validation gates (NOT SHIPPED). Future signal.
  L3 — T+1 NAV reconciliation divergence (NOT SHIPPED). Future signal.
  L4 — per-number provenance annotations + this status tile (config#2458,
       THIS module + ``loaders/data_integrity_loader.py`` +
       ``views/50_Data_Integrity.py``).

This module is PURE (no streamlit, no boto3): it reduces a list of
:class:`GateSignal` inputs to one green/amber/red rollup dot. Today only the
L1 signal is wired in by the loader — ``resolve_data_integrity`` itself is
phase-agnostic, so L2 (quarantine) and L3 (NAV divergence) signals can be
appended to the list the loader gathers without any change to the reduction
logic here. Un-wired phases are represented honestly: the loader simply does
not produce a signal for them yet (no fake/placeholder GateSignal), and the
view says so in its caption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

GREEN = "green"
AMBER = "amber"
RED = "red"

_DOT_ICONS = {GREEN: "🟢", AMBER: "🟡", RED: "🔴"}
_DOT_SEVERITY = {RED: 2, AMBER: 1, GREEN: 0}


@dataclass(frozen=True)
class GateSignal:
    """One phase's rollup contribution (e.g. "L1 cross-source agreement").

    ``dot`` is this signal's own green/amber/red verdict. ``flagged_count`` /
    ``total_count`` are the raw tallies behind it (for display), and
    ``detail`` is a list of per-row dicts (e.g. one per flagged ticker/date)
    suitable for a ``pd.DataFrame`` drill-down table.
    """

    phase: str
    label: str
    dot: str
    flagged_count: int = 0
    total_count: int = 0
    detail: tuple = field(default_factory=tuple)
    note: Optional[str] = None


def rollup_dot(signals: list[GateSignal]) -> str:
    """Reduce a list of phase signals to one overall dot.

    Empty input (no signals wired yet, or none produced any rows) rolls up
    to green — "nothing flagged" is the honest default; it is NOT the same
    claim as "L1+L2+L3 all clean", which is why the view must caption which
    phases are actually wired. Otherwise the overall dot is the MOST SEVERE
    of the per-signal dots (red > amber > green), matching the fleet-status
    severity-ordered rollup convention (``fleet_status.py``).
    """
    if not signals:
        return GREEN
    return max((s.dot for s in signals), key=lambda d: _DOT_SEVERITY.get(d, 0))


def dot_icon(dot: str) -> str:
    return _DOT_ICONS.get(dot, "⚪")


def format_provenance_annotation(row: Optional[dict], *, ticker: str, close: Optional[float] = None) -> Optional[str]:
    """Format a single-line, per-number provenance annotation from a
    daily_closes row dict (as produced by
    ``collectors/cross_source_observer.py`` in nousergon-data).

    Returns ``None`` when *row* is ``None`` or carries no ``xsource_*``
    annotation — callers must render their own "no L1 annotation available"
    fallback rather than fabricate one, per the fail-soft/never-silent
    convention this module follows throughout.

    The compact ``xsource_provenance`` string is itself already
    human-readable (produced by ``sources/cross_source_gate.py``, e.g.
    ``"SPY@2026-07-13: polygon=734.30 yfinance=734.31 agree@0.14bps"``); this
    just prefixes it with the ticker/close and a status glyph so it reads
    consistently with the tile's own dot vocabulary.
    """
    if not row:
        return None
    provenance = row.get("xsource_provenance")
    status = row.get("xsource_status")
    if provenance is None and status is None:
        return None
    glyph = {
        "agreed": "✓",
        "quarantined": "✗ QUARANTINED",
        "single_source_provisional": "⏳ single-source",
        "no_data": "— no data",
    }.get(status, status or "?")
    close_str = f"{close:.2f}" if close is not None else str(row.get("Close", "?"))
    if provenance:
        return f"{ticker} {close_str} — {glyph} — {provenance}"
    return f"{ticker} {close_str} — {glyph}"


def l1_signal_from_rows(rows: list[dict]) -> GateSignal:
    """Build the L1 (cross-source agreement) :class:`GateSignal` from a list
    of daily_closes row dicts already carrying the ``xsource_*`` columns
    (see ``collectors/cross_source_observer.py`` in nousergon-data for the
    producer and exact column semantics).

    Classification (deliberately conservative — L1 is observer-mode, so a
    quarantine is a real cross-source disagreement, never a withheld value):
      - any row with ``xsource_status == "quarantined"`` -> RED
      - else any row with ``xsource_flagged`` True (e.g. single-source-
        provisional or otherwise not a clean >=2-source agreement) -> AMBER
      - else (every row a clean >=2-source AGREED, or no rows at all) -> GREEN

    Rows missing the xsource_* columns entirely (pre-L1 parquet, or a
    partition the observer failed to annotate) are excluded from both the
    flagged tally and the dot decision — an absent annotation is "not yet
    observed", not "clean disagreement-free data"; it must not silently read
    as green. Such rows are counted in ``note`` instead so the gap is honest
    and visible rather than hidden inside a falsely-green tile.
    """
    quarantined: list[dict] = []
    flagged: list[dict] = []
    unannotated = 0
    total = 0

    for row in rows:
        total += 1
        status = row.get("xsource_status")
        if status is None:
            unannotated += 1
            continue
        if status == "quarantined":
            quarantined.append(row)
        elif row.get("xsource_flagged"):
            flagged.append(row)

    if quarantined:
        dot = RED
    elif flagged:
        dot = AMBER
    else:
        dot = GREEN

    note = None
    if unannotated:
        note = (
            f"{unannotated} of {total} row(s) carry no xsource_* annotation "
            "(pre-L1 partition or an observer miss) — excluded from this "
            "verdict, not counted as clean."
        )

    return GateSignal(
        phase="L1",
        label="Cross-source agreement (settled closes)",
        dot=dot,
        flagged_count=len(quarantined) + len(flagged),
        total_count=total,
        detail=tuple(quarantined + flagged),
        note=note,
    )
