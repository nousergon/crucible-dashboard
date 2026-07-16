"""
data_integrity_loader.py — input gathering for the Data Integrity status tile
(``views/50_Data_Integrity.py``, config#2458 / market-value-integrity L4).

Gathers the list of :class:`data_integrity_status.GateSignal` the resolver
(``data_integrity_status.rollup_dot``) reduces to one tile-level dot.

Wired in TODAY:
  - L1 cross-source agreement — ``loaders.s3_loader.load_latest_daily_closes``
    read through ``data_integrity_status.l1_signal_from_rows``.

NOT wired in yet (alpha-engine-config#1277 phases L2/L3 have not shipped —
no decomposed sub-issue exists for either as of this writing): this loader
deliberately does NOT append a placeholder/fake signal for them. When L2
(data-quality validation gates) or L3 (T+1 NAV reconciliation divergence)
ship a queryable current-gate-status surface, add a
``gather_l2_signal()``/``gather_l3_signal()`` here and append its result to
the list ``gather_data_integrity_signals()`` returns — ``rollup_dot`` and the
view need no change to pick up the new phase.
"""

from __future__ import annotations

from data_integrity_status import GateSignal, l1_signal_from_rows
from loaders.s3_loader import load_latest_daily_closes


def gather_l1_signal() -> GateSignal:
    """Build the L1 cross-source-agreement signal from the most recent
    available daily_closes partition. Every row in that partition is
    considered (not just the bounded cross-check set) — single-source rows
    correctly classify as flagged/amber via
    ``data_integrity_status.l1_signal_from_rows``."""
    df = load_latest_daily_closes()
    rows = df.to_dict("records") if not df.empty else []
    return l1_signal_from_rows(rows)


def gather_data_integrity_signals() -> list[GateSignal]:
    """Return every phase signal currently wired in. Today: L1 only."""
    return [gather_l1_signal()]
