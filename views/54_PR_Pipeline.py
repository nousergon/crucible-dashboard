"""
PR Pipeline — Alpha Engine (private console)

Structured view over the deterministic PR-sweep's per-cycle machine counters
(config#2709), instead of only the raw digest markdown the Backlog Groom page
(``views/42_Backlog_Groom.py``) renders for an individual run. Reads the same
``groom/{date}/sweep-*.json`` artifacts (``run_kind == "sweep"``) — this page
adds no new S3 producer, only a new consumer lens: bucket trends, merge
throughput by auto-merge path, and review-gate verdict history over a
trailing 14-day window.

Parsing lives in ``loaders/pr_pipeline.py`` (pure, unit-tested) — see that
module's docstring for the config#2709 issue-text vs. live-artifact
discrepancy this page's data is built around: the issue names a
``PR_SWEEP_CLASSIFY_DONE key=value`` machine line that, verified against 21
real sweep artifacts (2026-07-13..2026-07-19), is never actually persisted
into the artifact (it's an ephemeral stdout line from a mid-loop cycle,
never captured to a log or embedded in the digest). The classify-bucket
counts (conflicts/ci_red/clean_ready/...) this page shows instead come from
the digest's prose bold-header sections, which cover the FINAL quiescence-
loop cycle of each run — same underlying classifier, different (and the
only actually-available) surface. The four other DONE-line families the
issue names (``SCANNER_MERGE_SWEEP_DONE``, ``STANDING_EXCEPTION_MERGE_SWEEP_DONE``,
``GROOM_REVIEWED_MERGE_SWEEP_DONE``, ``STALENESS_FLUSH_DONE``) ARE embedded
verbatim, once per cycle, and are summed per-run below.
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.pr_merge_loader import load_open_prs_by_class  # noqa: E402
from loaders.pr_pipeline import (  # noqa: E402
    merge_throughput_by_path,
    review_gate_verdict_rows,
    sweep_trend_rows,
)
from loaders.s3_loader import list_groom_run_keys_since, load_groom_run  # noqa: E402

st.title("🔀 PR Pipeline")
st.caption(
    "Structured trends over the deterministic PR-sweep's per-cycle machine "
    "counters (config#2709) — fleet open-PR census, classify-bucket trends, "
    "merge throughput by path, and the reviewed-merge gate's arming-decision "
    "evidence (approved vs blocked). Per-run digest markdown still lives on "
    "**Backlog Groom** for the individual-run narrative."
)

_TREND_DAYS = 14

# ── (a) Current fleet open-PR count by class — live gh query ───────────────
st.subheader("Open PRs by class — live")
st.caption(
    "Current org-wide open-PR census (`org:nousergon is:pr is:open`), "
    "classified the same way `scripts/pr_sweep_classify.py` scopes its own "
    "population: dependabot-authored and any `gate:*`-labeled PR are routed "
    "to separate pipelines; what's left is groom-ready."
)
try:
    class_counts = load_open_prs_by_class()
except Exception as exc:
    class_counts = None
    st.error(f"Could not load live open-PR census: {exc}")
    st.info(
        "On the console box, `FLOW_DOCTOR_GITHUB_TOKEN` is hydrated from SSM "
        "at boot. Locally, run `gh auth login` or export `GH_TOKEN`."
    )

if class_counts is not None:
    _CLASS_LABEL = {
        "dependabot": "🤖 Dependabot",
        "gated": "🚧 Gated",
        "groom-ready": "🧹 Groom-ready",
        "other": "❔ Other",
    }
    cols = st.columns(len(_CLASS_LABEL))
    for col, (key, label) in zip(cols, _CLASS_LABEL.items()):
        with col:
            st.metric(label, class_counts.get(key, 0))

st.divider()

# ── Load trailing-window sweep artifacts once; every section below reuses
# this list. Loaders are @st.cache_data'd per key, so repeat renders within
# the TTL cost nothing extra. ─────────────────────────────────────────────
keys = list_groom_run_keys_since(days=_TREND_DAYS)
if not keys:
    st.info(
        "🛈 No groom run artifacts found in the trailing "
        f"{_TREND_DAYS} days yet."
    )
    st.stop()

loaded_runs: list[tuple[str, dict]] = []
for k in keys:
    run = load_groom_run(k)
    if run is not None:
        loaded_runs.append((k, run))

trend_rows = sweep_trend_rows(loaded_runs)

if not trend_rows:
    st.info(
        "🛈 No standalone PR-sweep run artifacts (`run_kind == \"sweep\"`) in "
        f"the trailing {_TREND_DAYS} days — only coverage runs found, or "
        "sweep artifacts predate this page."
    )
    st.stop()

_first = trend_rows[0]["run_start"]
_last = trend_rows[-1]["run_start"]
_span_days = max((_last.date() - _first.date()).days + 1, 1)
st.caption(
    f"{len(trend_rows)} sweep run(s) across {_span_days} day(s) "
    f"({_first.strftime('%Y-%m-%d')} → {_last.strftime('%Y-%m-%d')})."
)
if _span_days < 3:
    st.warning(
        f"⚠️ Only {_span_days} day(s) of sweep history in the trailing "
        f"{_TREND_DAYS}-day window — the config#2709 close condition wants "
        "≥3 days of cycle history before this page is considered live."
    )

# ── (b) Bucket trends over trailing 14 days ─────────────────────────────────
st.subheader("Classify-bucket trends")
st.caption(
    "Final-cycle classify bucket sizes per sweep run (conflicts / CI-red / "
    "clean+ready) — falling conflicts/ci_red and a healthy clean_ready floor "
    "is the backlog draining faster than it refills."
)
bucket_df = pd.DataFrame(
    [
        {
            "run_start": r["run_start"],
            "conflicts": r["conflicts"],
            "ci_red": r["ci_red"],
            "clean_ready": r["clean_ready"],
        }
        for r in trend_rows
        if r["conflicts"] is not None or r["ci_red"] is not None or r["clean_ready"] is not None
    ]
).set_index("run_start") if trend_rows else pd.DataFrame()
if not bucket_df.empty:
    st.line_chart(bucket_df, color=["#cf222e", "#eda100", "#1a7f37"], height=260)
else:
    st.caption("No runs with a readable classify section in this window.")

# ── (c) Merge throughput by path ────────────────────────────────────────────
st.subheader("Merge throughput by path")
throughput = merge_throughput_by_path(trend_rows)
st.caption(
    "Total auto-merges over the window, by sweep path. `standing-exception` "
    "covers BOTH Dependabot-native and docs/pin-bump standing exceptions — "
    "the sweep scripts don't sub-bucket by reason inside that one counter "
    "(see `loaders/pr_pipeline.py::merge_throughput_by_path` docstring)."
)
t_cols = st.columns(3)
t_cols[0].metric("Scanner-remediation", throughput["scanner"])
t_cols[1].metric("Standing-exception", throughput["standing-exception"])
t_cols[2].metric("Groom-reviewed", throughput["groom-reviewed"])

throughput_df = pd.DataFrame(
    [
        {
            "run_start": r["run_start"],
            "scanner": r["scanner_merged"],
            "standing-exception": r["standing_merged"],
            "groom-reviewed": r["reviewed_merged"],
        }
        for r in trend_rows
    ]
).set_index("run_start")
if not throughput_df.empty:
    st.bar_chart(throughput_df, height=240)

# ── (d) Review-gate verdict history ─────────────────────────────────────────
st.subheader("Review-gate verdict history")
st.caption(
    "Per-run `GROOM_REVIEWED_MERGE_SWEEP_DONE` verdicts — the arming-decision "
    "evidence surface: how often the reviewed-merge gate armed (merged / "
    "approved-dry-run) vs held (blocked) a PR."
)
verdict_rows = review_gate_verdict_rows(trend_rows)
verdict_df = pd.DataFrame(
    [{"run_start": r["run_start"], "merged": r["merged"], "blocked": r["blocked"]}
     for r in verdict_rows]
).set_index("run_start")
if not verdict_df.empty:
    st.line_chart(verdict_df, color=["#1a7f37", "#cf222e"], height=240)

with st.expander("Per-run verdict table"):
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Run start (UTC)": r["run_start"].strftime("%Y-%m-%d %H:%M"),
                    "Merged": r["merged"],
                    "Approved (dry-run)": r["approved_dry_run"],
                    "Blocked": r["blocked"],
                }
                for r in reversed(verdict_rows)
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

# ── (e) Staleness / linkage-violation counters ──────────────────────────────
st.subheader("Staleness / linkage-violation counters")
st.caption(
    "`STALENESS_FLUSH_DONE` — Brian's 2026-07-15 directive that no PR (draft "
    "or ready) ever goes stale/orphaned. `linkage_violations` > 0 is worth "
    "checking directly: it means a PR's issue linkage broke."
)
staleness_df = pd.DataFrame(
    [
        {
            "run_start": r["run_start"],
            "flushed_gated": r["flushed_gated"],
            "flushed_ready": r["flushed_ready"],
            "linkage_violations": r["linkage_violations"],
        }
        for r in trend_rows
    ]
).set_index("run_start")
if not staleness_df.empty:
    st.line_chart(staleness_df, color=["#6e7781", "#2a78d6", "#cf222e"], height=220)

_total_linkage = sum(r["linkage_violations"] for r in trend_rows)
if _total_linkage:
    st.error(
        f"⚠️ {_total_linkage} linkage violation(s) flushed in the window — "
        "check `STALENESS_FLUSH_DONE` detail in the affected run's digest "
        "on the Backlog Groom page."
    )
else:
    st.caption("No linkage violations in the window.")
