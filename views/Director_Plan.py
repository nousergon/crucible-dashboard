"""Director — the weekly advisory action plan (Layer C).

Reads ``director/{date}/action_plan.json`` + ``director/carryover_ledger.json``
(produced by the alpha-engine-evaluator-director Lambda, the final Saturday-
pipeline task once `DIRECTOR_ENABLED` is on). The Director weighs the Report
Card and *proposes* a structured action plan with carry-over — it never writes
live trading config and never self-merges. This page is read-only observability
for the observe-mode soak.
"""

import streamlit as st

from components.director_plan import render_overview
from loaders.s3_loader import (
    list_director_dates,
    load_action_plan,
    load_carryover_ledger,
)

st.title("🧭 Director — Weekly Action Plan")
st.caption(
    "The slow loop: a single Opus call over the Report Card proposes the week's "
    "structured action plan (owners, priorities, horizons) with carry-over. "
    "Advisory only — it proposes; Brian disposes. Dormant until `DIRECTOR_ENABLED`."
)

# Honor the ?date= deep-link from the Director digest email
# (…/director?date=YYYY-MM-DD — the run trading-day key, e.g. Friday for a
# Saturday run), defaulting to the latest plan. Mirrors the EOD Report page.
_dates = list_director_dates()
_selected = None
if _dates:
    _qp_date = st.query_params.get("date")
    _default_idx = _dates.index(_qp_date) if _qp_date in _dates else 0
    _selected = st.selectbox("Director run (date)", _dates, index=_default_idx)
    st.query_params["date"] = _selected

render_overview(load_action_plan(_selected), load_carryover_ledger())
