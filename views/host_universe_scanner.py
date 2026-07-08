import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

# One Universe surface (console-IA phase 2b, config#1988): all four tabs are
# lenses on the same Saturday scan — the Board anchors (it owns the
# scanner/universe artifact the Funnel borrows thresholds from), the Funnel
# shows the ~900→~60 cut, Trends the time axis of the same history parquet,
# and Focus Audit the scanner_evaluations shadow-audit (moved here from the
# Signals host — same data source, same cycle).
render_host(
    [
        ("Universe Board", "39_Universe_Board.py"),
        ("Funnel", "34_Scanner.py"),
        ("Trends", "40_Attractiveness_Trends.py"),
        ("Focus Audit", "5_Focus_List.py"),
    ],
    key="host_universe_scanner",
)
