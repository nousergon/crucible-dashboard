import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

# One Universe surface (console-IA phase 2b, config#1988): all five tabs are
# lenses on the same Saturday scan — the Board anchors (it owns the
# scanner/universe artifact the Funnel borrows thresholds from), the Funnel
# shows the ~900→~60 cut, Trends the time axis of the same history parquet,
# Churn the membership axis across cycles (config-I4819 — the only tab that
# spans MULTIPLE cycles, which is why it reads the universe_membership contract
# rather than any single-snapshot artifact), and Focus Audit the
# scanner_evaluations shadow-audit (moved here from the Signals host — same
# data source, same cycle).
render_host(
    [
        ("Universe Board", "39_Universe_Board.py"),
        ("Funnel", "34_Scanner.py"),
        ("Trends", "40_Attractiveness_Trends.py"),
        ("Churn", "55_Universe_Churn.py"),
        ("Focus Audit", "5_Focus_List.py"),
    ],
    key="host_universe_scanner",
)
