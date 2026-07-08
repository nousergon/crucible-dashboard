import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("LLM Cost", "23_LLM_Cost.py"),
        ("LLM Usage", "36_LLM_Usage.py"),
    ],
    key="host_cost_usage",
)
