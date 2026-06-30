import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Report Card", "Report_Card.py"),
        ("Component Detail", "Report_Card_Detail.py"),
    ],
    key="host_report_card",
)
