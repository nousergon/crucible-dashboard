"""Unit tests for the Data Integrity rollup logic (data_integrity_status.py).

Pure module — no streamlit, no boto3, no clock — so the full green/amber/red
matrix is exercised directly against synthetic daily_closes row dicts,
satisfying config#2458's closes-when criterion ("verify by forcing a
synthetic quarantine/divergence... confirming the tile goes amber/red") for
the L1 slice this issue actually scopes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_integrity_status import (  # noqa: E402
    AMBER,
    GREEN,
    RED,
    GateSignal,
    l1_signal_from_rows,
    rollup_dot,
)


def _row(ticker="SPY", status="agreed", flagged=False, bps=1.2, provenance="SPY@2026-07-13: polygon=734.30 yfinance=734.31 agree@0.14bps"):
    return {
        "ticker": ticker,
        "xsource_status": status,
        "xsource_flagged": flagged,
        "xsource_agreement_bps": bps,
        "xsource_provenance": provenance,
    }


class TestL1SignalFromRows:
    def test_all_agreed_rows_roll_up_green(self):
        rows = [_row(), _row(ticker="SPY", status="agreed", flagged=False)]
        sig = l1_signal_from_rows(rows)
        assert sig.dot == GREEN
        assert sig.flagged_count == 0
        assert sig.total_count == 2
        assert sig.note is None

    def test_no_rows_rolls_up_green(self):
        sig = l1_signal_from_rows([])
        assert sig.dot == GREEN
        assert sig.total_count == 0

    def test_single_source_provisional_flagged_rolls_up_amber(self):
        rows = [
            _row(),
            _row(
                ticker="AAPL", status="single_source_provisional", flagged=True,
                bps=None, provenance="AAPL@2026-07-13: polygon=210.11 (single source)",
            ),
        ]
        sig = l1_signal_from_rows(rows)
        assert sig.dot == AMBER
        assert sig.flagged_count == 1
        assert len(sig.detail) == 1

    def test_quarantined_row_rolls_up_red_even_with_other_agreed_rows(self):
        rows = [
            _row(),
            _row(
                ticker="SPY", status="quarantined", flagged=True, bps=45.0,
                provenance="SPY@2026-07-13: polygon=734.30 yfinance=736.60 DISAGREE@31.30bps QUARANTINED",
            ),
        ]
        sig = l1_signal_from_rows(rows)
        assert sig.dot == RED
        assert sig.flagged_count == 1

    def test_quarantine_outranks_amber_when_both_present(self):
        rows = [
            _row(ticker="AAPL", status="single_source_provisional", flagged=True, bps=None),
            _row(ticker="SPY", status="quarantined", flagged=True, bps=50.0),
        ]
        sig = l1_signal_from_rows(rows)
        assert sig.dot == RED
        assert sig.flagged_count == 2

    def test_unannotated_rows_excluded_and_noted_not_counted_as_clean(self):
        rows = [
            {"ticker": "MSFT", "Close": 410.0},  # no xsource_* columns at all
            _row(),
        ]
        sig = l1_signal_from_rows(rows)
        assert sig.dot == GREEN
        assert sig.total_count == 2
        assert sig.note is not None
        assert "1 of 2" in sig.note


class TestRollupDot:
    def test_empty_signal_list_is_green(self):
        assert rollup_dot([]) == GREEN

    def test_single_green_signal_is_green(self):
        sig = GateSignal(phase="L1", label="x", dot=GREEN)
        assert rollup_dot([sig]) == GREEN

    def test_takes_most_severe_signal(self):
        green = GateSignal(phase="L1", label="x", dot=GREEN)
        amber = GateSignal(phase="L2", label="y", dot=AMBER)
        assert rollup_dot([green, amber]) == AMBER

    def test_red_outranks_amber_and_green(self):
        green = GateSignal(phase="L1", label="x", dot=GREEN)
        amber = GateSignal(phase="L2", label="y", dot=AMBER)
        red = GateSignal(phase="L3", label="z", dot=RED)
        assert rollup_dot([green, amber, red]) == RED
