"""
Fleet Checks — Alpha Engine (private console)

The operator surface for every SCHEDULED check in the fleet: IAM grant usage,
scheduled-workflow health, deploy-release standard conformance, lib-pin drift,
and anything added later.

WHY THIS PAGE EXISTS
--------------------
Brian, 2026-07-29: *"we need to build all these checks into console for easier
and persistent monitoring."*

Before this page, scheduled checks reported to Telegram and a workflow log.
Neither is a surface. A Telegram message is unqueryable, scrolls away, and —
the part that actually cost us — its ABSENCE is indistinguishable from
"nothing is wrong". On 2026-07-29 four scheduled workflows were found failing
simultaneously, one of them having failed *every run since it shipped*, with no
signal anywhere (alpha-engine-config-I5507).

DATA SOURCE AND STALENESS (Status-Surface Standard §118 rule 3)
---------------------------------------------------------------
Every check writes `s3://alpha-engine-research/ops/checks/{check_id}/latest.json`
in a common envelope. This page discovers them by S3 prefix, so a NEW check
appears the first time it runs — no console deploy in the loop, which is
precisely why checks used to settle for Telegram.

**A check that stopped running is never green here.** `loaders.fleet_checks_loader`
overrides the reported status with STALE when the artifact is older than ~2.5×
the check's own declared cadence, because the last thing a dying check writes
is usually "ok".
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.fleet_checks_loader import (  # noqa: E402
    CHECKS_PREFIX,
    STALE_CADENCE_MULTIPLE,
    STATUS_ATTENTION,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_STALE,
    STATUS_UNREADABLE,
    load_check_results,
)

_ICON = {
    STATUS_OK: "🟢",
    STATUS_ATTENTION: "🟡",
    STATUS_STALE: "🔴",
    STATUS_ERROR: "🔴",
    STATUS_UNREADABLE: "⚪",
}

st.title("✅ Fleet Checks")

results = load_check_results()

if not results:
    st.warning(
        f"No check results found under `{CHECKS_PREFIX}`. Either no check has "
        "published yet, or the S3 probe is unavailable — this is deliberately "
        "not rendered as 'all clear'."
    )
    st.stop()

bad = [c for c in results if c.status in (STATUS_ERROR, STATUS_STALE, STATUS_UNREADABLE)]
attention = [c for c in results if c.status == STATUS_ATTENTION]

c1, c2, c3 = st.columns(3)
c1.metric("Checks", len(results))
c2.metric("Failing / stale", len(bad))
c3.metric("Want attention", len(attention))

st.dataframe(
    pd.DataFrame([
        {
            "": _ICON.get(c.status, "⚪"),
            "Check": c.label,
            "Status": c.status,
            "Summary": c.summary,
            "Last ran (UTC)": c.ran_at.strftime("%Y-%m-%d %H:%M") if c.ran_at else "—",
            "Cadence": f"{c.cadence_minutes / 60:.0f}h",
            "Findings": len(c.findings),
        }
        for c in results
    ]),
    hide_index=True,
    use_container_width=True,
)

for c in results:
    if not c.findings:
        continue
    with st.expander(f"{_ICON.get(c.status, '⚪')} {c.label} — {len(c.findings)} finding(s)"):
        if c.deep_link:
            st.markdown(f"[Evidence / tracking →]({c.deep_link})")
        st.dataframe(pd.DataFrame(list(c.findings)), hide_index=True,
                     use_container_width=True)

now = datetime.now(timezone.utc)
st.caption(
    f"Source: `s3://alpha-engine-research/{CHECKS_PREFIX}*/latest.json`, discovered "
    f"by prefix. A check is marked **stale** once its artifact is older than "
    f"{STALE_CADENCE_MULTIPLE}× its own declared cadence, whatever status it last "
    f"reported. Page rendered {now.strftime('%Y-%m-%d %H:%M:%S UTC')}."
)
