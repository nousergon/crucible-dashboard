"""Optimizer — unified MVO surface (Cycle + History lenses).

Consolidates the former Optimizer Decision (per-name sizing microscope for one
cycle) and Optimizer Risk (time-series of deployed levers + realized book risk)
pages. Both are lenses on the SAME artifact — the daily optimizer shadow log
(``predictor/optimizer_shadow/{date}.json``) — so they share one tab on the
Execution front page; an internal selector execs only the chosen lens (lazy,
per ARCH §46). Console-IA redesign phase 1, alpha-engine-config#1990.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from shared.view_host import _exec_view

_LENSES = [
    ("Cycle — per-stock decision", "32_Optimizer_Decision.py"),
    ("History — levers & realized risk", "30_Optimizer_Risk.py"),
]
_labels = [label for label, _ in _LENSES]
_by_label = dict(_LENSES)

_active = st.segmented_control(
    "Lens", _labels, default=_labels[0], key="optimizer_lens"
) or _labels[0]
_exec_view(_by_label[_active])
