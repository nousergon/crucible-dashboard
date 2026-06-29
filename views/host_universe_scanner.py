import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Scanner", "34_Scanner.py"),
        ("Universe Board", "39_Universe_Board.py"),
        ("Attractiveness Trends", "40_Attractiveness_Trends.py"),
    ],
    key="host_universe_scanner",
)
