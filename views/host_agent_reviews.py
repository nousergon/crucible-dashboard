import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Decision Review", "29_Decision_Review.py"),
        ("Sector Team", "33_Sector_Team_Review.py"),
        ("CIO Review", "31_CIO_Review.py"),
    ],
    key="host_agent_reviews",
)
