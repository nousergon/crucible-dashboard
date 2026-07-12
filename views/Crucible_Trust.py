"""Crucible Results — Trust, the battery surface (config#1958 deliverable 10).

"How we know the grader isn't lying": the named validation legs that guard
the backtest engine and the evaluator, each vouched for by its repo's LIVE
main-branch CI verdict (read from the GitHub API — no hand-kept results), and
the ledger of real defects the battery has caught, each anchored to a merged
PR. Honesty discipline: caveats and observe-mode limits are printed next to
the claims they qualify, and the "what this does NOT prove" section is part
of the surface, not a footnote.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from loaders.trust_battery_loader import load_ci_verdicts  # noqa: E402
from results import view_model as vm  # noqa: E402
from results.battery_registry import BATTERY_FINDINGS, BATTERY_LEGS  # noqa: E402

st.title("Trust — the validation battery")
st.caption(
    "Backtests are easy to flatter and graders are easy to game. These are the named, "
    "continuously-run checks that make that hard here — each vouched for by the repo's "
    "live main-branch CI, not by this page's author."
)

repos = tuple(sorted({leg["repo"] for leg in BATTERY_LEGS}))
verdicts = load_ci_verdicts(repos)
rows = vm.trust_rows(BATTERY_LEGS, verdicts)

_ICON = {"SUCCESS": "🟢", "FAILURE": "🔴", "UNAVAILABLE": "⚪"}
for row in rows:
    icon = _ICON.get(row["ci"], "🟡")
    with st.container(border=True):
        head, meta = st.columns([3, 2])
        with head:
            st.markdown(f"**{icon} {row['leg']}**  ·  `{row['repo']}`")
            st.markdown(row["proves"])
            if row["caveat"]:
                st.caption(f"⚠ {row['caveat']}")
        with meta:
            if row["ci"] == "SUCCESS":
                st.markdown(f"CI **pass** · `{row['commit']}` · {row['verified']} UTC")
            elif row["ci"] == "UNAVAILABLE":
                st.markdown(f"CI status unavailable — {row['error']}")
            else:
                st.markdown(f"CI **{row['ci']}** · `{row['commit']}` · {row['verified']} UTC")
            if row["link"]:
                st.markdown(f"[latest main run]({row['link']})")
            st.caption(row["tests"])

st.subheader("What the battery has caught")
st.caption("A validation battery that never finds anything is decoration. Each finding links to its merged fix.")
st.dataframe(
    pd.DataFrame(BATTERY_FINDINGS)[["date", "found_by", "finding", "fix"]],
    use_container_width=True, hide_index=True,
    column_config={"finding": st.column_config.TextColumn("finding", width="large")},
)

st.subheader("What this does not prove")
st.markdown(
    "- **No live-money results.** Everything on this surface is paper-traded and illustrative only.\n"
    "- **Green checks bound the engine's honesty, not the strategy's edge** — a correctly-measured "
    "strategy can still have negative alpha, and this dashboard will show it when it does.\n"
    "- **Coverage is explicit, not total:** legs carry their caveats inline (observe-mode paging, "
    "partial tile coverage), and extending them is tracked, public work."
)
