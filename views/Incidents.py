"""Incidents — unified failure surface (event lake + retros + quarantine).

Consolidates three lenses on the same changelog corpus into one Observability
tab: the raw event-lake mining page (Changelog), the deterministic retro feed
(Retros), and the vocab-reject triage table (Quarantine — empty is the healthy
state, so it never deserved its own tab). An internal selector execs only the
chosen lens (lazy, per ARCH §46). Console-IA redesign phase 1,
alpha-engine-config#1990.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from shared.view_host import _exec_view

_LENSES = [
    ("Event Lake", "38_Changelog.py"),
    ("Retros", "28_Retros.py"),
    ("Quarantine", "41_Quarantine.py"),
]
_labels = [label for label, _ in _LENSES]
_by_label = dict(_LENSES)

_active = st.segmented_control(
    "Lens", _labels, default=_labels[0], key="incidents_lens"
) or _labels[0]
_exec_view(_by_label[_active])
