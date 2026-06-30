import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

render_host(
    [
        ("Architecture", "10_Architecture.py"),
        ("Signal Lifecycle", "11_Signal_Lifecycle.py"),
        ("RAG Inventory", "14_RAG_Inventory.py"),
    ],
    key="host_reference",
)
