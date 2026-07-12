import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.view_host import render_host

# Unified Executor-stage front page. The portfolio optimizer is not its own
# module — it is the executor's planning stage (lives in crucible-executor:
# portfolio_optimizer → optimizer_shadow → optimizer_cutover; its output IS
# the order book). So its surfaces belong here under Execution, not scattered
# across Research & Signals (Order Book) and Backtester & Eval (Optimizer
# Risk/Decision) where they had drifted. Order Book leads — it carries the
# daily book_status banner answering "did/why the book move today".
render_host(
    [
        ("Order Book", "16_Order_Book_Rationale.py"),
        ("Execution", "6_Execution.py"),
        # Decision + Risk merged into one Optimizer tab (two lenses on the
        # same optimizer_shadow artifact) — console-IA phase 1, config#1990.
        ("Optimizer", "Optimizer.py"),
    ],
    key="host_execution",
)
