"""
Backlog Groom — Alpha Engine (private console)

Operator audit surface for the complexity-tier backlog groom (config#1495,
#1512). Every scheduled groom run (2x/day Sonnet mid/low-tier + 1x/day Opus
high-tier) writes a per-run artifact to
``s3://alpha-engine-research/groom/{date}/{run_id_or_hhmmss}.json``
(``groom_driver.py::write_run_artifact``) — this page is its consumer.

The point of this page: answer "did the model actually think about each
issue?" from VERIFIABLE artifacts, never a self-report. Each queued issue's
disposition (closed / pr_opened / commented / untouched) is cross-referenced
against real GitHub state at write time — a PR link, a close reason, or the
actual latest comment — not a claim the agent made about itself. This is the
same ground-truth-over-self-report principle the run-attribution fix
(config#1512) applies to the PR/close counts in the GitHub digest issue.

Complementary to **Saturday SF Watch** (failure-event timeline for the trading
pipelines) — this page is the per-run activity log for the groom pipeline.
"""

from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.groom_efficiency import (  # noqa: E402
    compute_efficiency,
    format_efficiency_row,
    match_usage_for_run,
)
from loaders.s3_loader import (  # noqa: E402
    list_groom_run_keys,
    list_groom_usage_records,
    load_groom_run,
)

_DISPOSITION_LABEL: dict[str, str] = {
    "closed": "✅ closed",
    "pr_opened": "🔧 PR opened",
    "commented": "💬 commented",
    # config#1928: label-only work (gate labels, complexity escalations) —
    # the norm for blocked dispositions since the config#1890 comment-skip.
    "labeled": "🏷 labeled",
    "untouched": "⚠️ untouched",
}
_DISPOSITION_COLOR_HEX: dict[str, str] = {
    "closed": "#1a7f37",
    "pr_opened": "#0969da",
    "commented": "#9a6700",
    "labeled": "#8250df",
    "untouched": "#cf222e",
}


def _run_label(key: str) -> str:
    """``groom/{date}/{suffix}.json`` -> ``{date} {suffix}`` for the selector."""
    stem = key.removeprefix("groom/").removesuffix(".json")
    return stem.replace("/", " ")


st.title("🧹 Backlog Groom")
st.caption(
    "Per-run audit trail for the complexity-tier backlog groom — every "
    "issue's disposition cross-referenced against real GitHub state, not a "
    "self-report. (config#1495, #1512)"
)

keys = list_groom_run_keys()
if not keys:
    st.info(
        "🛈 No groom run artifacts found yet. Written by `groom_driver.py` "
        "starting with the config#1512 follow-up — older runs (and any run "
        "before that ships) have no artifact here; check the `groom-digest` "
        "GitHub issues on `alpha-engine-config` for their record instead."
    )
    st.stop()

# ── Run history — one summary row per recent run (2026-07-02 operator ask:
# see the digest + a per-run summary WITHOUT opening GitHub). Loaders are
# @st.cache_data'd per key, so this fans out to at most _HISTORY_N cached S3
# GETs and re-renders free within the TTL. ──────────────────────────────────
_HISTORY_N = 12
_DIGEST_ISSUE_URL = "https://github.com/nousergon/alpha-engine-config/issues/{n}"
usage_index = list_groom_usage_records()
assigned_usage: set[str] = set()
history_rows = []
run_efficiency: dict[str, dict] = {}
for k in keys[:_HISTORY_N]:
    run = load_groom_run(k)
    if not run:
        continue
    run_issues = run.get("issues") or []
    counts = {d: sum(1 for i in run_issues if i.get("disposition") == d)
              for d in ("closed", "pr_opened", "commented", "untouched")}
    soft = run.get("soft_limit_min") or 0
    digest_n = run.get("digest_issue") or 0
    usage = match_usage_for_run(k, run, usage_index, assigned=assigned_usage)
    if usage:
        assigned_usage.add(usage["key"])
    eff = compute_efficiency(run, run_issues, usage)
    run_efficiency[k] = eff
    eff_cols = format_efficiency_row(eff)
    history_rows.append({
        "Run": _run_label(k),
        "Tier": run.get("issue_filter", "—"),
        "Outcome": "🟠 floor breach" if run.get("floor_fail") else "✅ ok",
        "Stop reason": (run.get("stop_reason") or "—")[:60],
        "Coverage": f"{run.get('processed', len(run_issues))}/{run.get('total_issues', len(run_issues))}",
        "✅ closed": counts["closed"],
        "🔧 PRs": counts["pr_opened"],
        "💬 comm.": counts["commented"],
        "⚠️ unt.": counts["untouched"],
        "Budget (min)": f"{run.get('elapsed_min') or 0}/{soft}" if soft else "—",
        **eff_cols,
        "Digest": _DIGEST_ISSUE_URL.format(n=digest_n) if digest_n else None,
    })

st.subheader("Run history")
if history_rows:
    st.caption(
        "**WET** / **WET/eng** / **iss/min** join run artifacts to "
        "`claude_code_usage/groom/` by date + nearest end-time (spot runs use "
        "different IDs than the artifact key). **Efficiency** flags high "
        "untouched %, WET/issue, or slow throughput vs tier baselines."
    )
    st.dataframe(
        pd.DataFrame(history_rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Digest": st.column_config.LinkColumn("Digest", display_text=r".*/(\d+)$"),
        },
    )
else:
    st.caption("No readable run artifacts in the recent window.")

col_sel, col_meta = st.columns([1, 3])
with col_sel:
    selected_key = st.selectbox(
        "Run", keys, index=0, format_func=_run_label,
    )

data = load_groom_run(selected_key)
if data is None:
    st.warning(f"Run artifact `{selected_key}` could not be read.")
    st.stop()

issues = data.get("issues") or []
other_closed = data.get("other_closed") or []
other_prs = data.get("other_prs") or []

n_closed = sum(1 for i in issues if i.get("disposition") == "closed")
n_pr = sum(1 for i in issues if i.get("disposition") == "pr_opened")
n_commented = sum(1 for i in issues if i.get("disposition") == "commented")
n_untouched = sum(1 for i in issues if i.get("disposition") == "untouched")

# Efficiency for selected run (reuse history cache when present)
if selected_key in run_efficiency:
    sel_eff = run_efficiency[selected_key]
else:
    sel_usage = match_usage_for_run(selected_key, data, usage_index)
    sel_eff = compute_efficiency(data, issues, sel_usage)

with col_meta:
    st.caption(
        f"Model: **{data.get('model', '—')}** · Filter: **{data.get('issue_filter', '—')}** · "
        f"Stop reason: {data.get('stop_reason', '—')}"
    )

st.subheader("Token efficiency")
if sel_eff.get("usage_matched"):
    e1, e2, e3, e4, e5, e6 = st.columns(6)
    wet = sel_eff.get("wet")
    e1.metric("Run WET", f"{wet/1e6:.1f}M" if wet is not None else "—",
              help="Weighted effective tokens for this groom run (from usage capture).")
    wpe = sel_eff.get("wet_per_engaged")
    e2.metric("WET / engaged", f"{wpe/1e3:.0f}K" if wpe is not None else "—",
              help="Token cost per dispositioned issue — primary efficiency ratio.")
    wph = sel_eff.get("wet_per_hard")
    e3.metric("WET / hard outcome", f"{wph/1e3:.0f}K" if wph is not None else "—",
              help="WET per close or PR opened (undefined when none).")
    thr = sel_eff.get("throughput")
    e4.metric("Throughput", f"{thr:.2f}/min" if thr is not None else "—",
              help="Engaged issues per elapsed minute.")
    cr = sel_eff.get("cache_read_pct")
    e5.metric("Cache-read %", f"{cr:.0f}%" if cr is not None else "—",
              help="Share of raw tokens that were cache reads (high = good).")
    hr = sel_eff.get("hard_rate")
    e6.metric("Hard-outcome rate", f"{hr*100:.0f}%" if hr is not None else "—",
              help="(closes + PRs) / engaged — comment-only runs skew low on Opus.")
    r1, r2, r3 = st.columns(3)
    dr = sel_eff.get("disposition_rate")
    r1.metric("Disposition rate", f"{dr*100:.0f}%" if dr is not None else "—",
              help="Engaged / queued — coverage quality.")
    cr2 = sel_eff.get("comment_rate")
    r2.metric("Comment-only rate", f"{cr2*100:.0f}%" if cr2 is not None else "—",
              help="Commented / engaged — high on verify-heavy Opus runs.")
    uf = sel_eff.get("untouched_frac")
    r3.metric("Untouched rate", f"{uf*100:.0f}%" if uf is not None else "—",
              help="Untouched / queued — should stay near 0.")
    if sel_eff.get("alerts"):
        st.warning("Efficiency flags: " + "; ".join(sel_eff["alerts"]))
    else:
        st.caption(f"Usage matched: `{sel_eff.get('usage_key', '—')}`")
else:
    st.caption(
        "🛈 No groom usage file matched this run (pre-2026-07-02 capture gap, or "
        "usage capture failed). Token efficiency metrics need "
        "`claude_code_usage/groom/{date}/*.json` from the spot bootstrap step."
    )
    if sel_eff.get("alerts"):
        st.warning("Outcome flags (no usage join): " + "; ".join(sel_eff["alerts"]))

# ── Budget vs consumed (config#1569; schema_version >= 2 only — older runs ──
# never captured these fields, so soft_limit_min is 0/absent for them) ───────
if data.get("schema_version", 1) >= 2 and data.get("soft_limit_min"):
    soft_limit = data["soft_limit_min"]
    elapsed = data.get("elapsed_min", 0)
    engaged = data.get("engaged", 0)
    floor = data.get("floor", 0)
    pct_used = (elapsed / soft_limit * 100) if soft_limit else 0.0
    bcol1, bcol2, bcol3 = st.columns(3)
    with bcol1:
        st.metric("Soft budget used", f"{elapsed}/{soft_limit} min", f"{pct_used:.0f}%")
    with bcol2:
        st.metric(
            "Engaged / floor", f"{engaged} / {floor}",
            help="Issues dispositioned (closed/PR'd/commented) this run vs the fail-loud "
                 "floor below which a budget+time-remaining stop is flagged as a "
                 "self-taper (config#1374, engagement metric per config#1382/#1564).",
        )
    with bcol3:
        st.metric("Queue coverage", f"{data.get('processed', len(issues))}/{data.get('total_issues', len(issues))}")
    if not data.get("floor_fail") and elapsed < soft_limit:
        st.caption(
            f"Finished {soft_limit - elapsed} min under budget with stop reason starting "
            f"\"{(data.get('stop_reason') or '')[:40]}...\" — **this is expected, not a bug**, "
            "when the queue drains before the soft deadline (a small/clean backlog is cheap "
            "to fully disposition). Only a 🟠 floor-breach below is a self-taper signal."
        )
else:
    st.caption(
        "🛈 This run predates budget-tracking (schema_version "
        f"{data.get('schema_version', 1)}, pre-2026-07-02) — no soft-budget-vs-consumed "
        "data was captured for it."
    )

tiles = st.columns(5)
with tiles[0]:
    st.metric("Queued issues", len(issues))
with tiles[1]:
    st.metric("✅ Closed", n_closed)
with tiles[2]:
    st.metric("🔧 PR opened", n_pr)
with tiles[3]:
    st.metric("💬 Commented", n_commented)
with tiles[4]:
    st.metric("⚠️ Untouched", n_untouched, delta=None if n_untouched == 0 else "check below",
             delta_color="inverse")

if data.get("floor_fail"):
    st.error(
        "⚠️ FAIL-LOUD FLOOR BREACHED — this run stopped with budget+time "
        "remaining but delivered fewer than the minimum work-items. Treated "
        "as a self-taper failure; see the `groom-digest` GitHub issue."
    )
if n_untouched:
    st.warning(
        f"{n_untouched} queued issue(s) got NO action this run (not closed, "
        "no PR, no comment) — coverage is supposed to be mandatory; an "
        "untouched issue here means it hit the re-queue attempt cap without "
        "ever being dispositioned. Worth checking why."
    )

# ── Run digest (schema_version >= 3 embeds the finalized digest verbatim, ──
# written by groom_driver.py at the same moment it finalizes the GitHub
# groom-digest issue — same driver-computed content, zero GitHub API
# dependency for the console) ────────────────────────────────────────────────
st.subheader("Run digest")
digest_md = data.get("digest_markdown") or ""
digest_issue = data.get("digest_issue") or 0
if digest_issue:
    st.caption(
        f"Primary record: [alpha-engine-config#{digest_issue}]"
        f"({_DIGEST_ISSUE_URL.format(n=digest_issue)})"
    )
if digest_md:
    with st.expander(data.get("digest_title") or "Digest", expanded=True):
        st.markdown(digest_md)
else:
    st.caption(
        "🛈 This run predates digest embedding in the artifact (schema_version "
        f"{data.get('schema_version', 1)}, pre-2026-07-02) — see the "
        "`groom-digest` GitHub issues on `alpha-engine-config` for its "
        "narrative record."
    )

# ── Per-issue disposition table ─────────────────────────────────────────────
st.subheader("Per-issue disposition")

df = pd.DataFrame(issues)
display = pd.DataFrame({
    "Issue": df.apply(lambda r: f"{r['repo'].rsplit('/', 1)[-1]}#{r['number']}", axis=1) if len(df) else [],
    "Priority": df.get("priority"),
    "Title": df.get("title"),
    "Disposition": df.get("disposition", pd.Series(dtype=str)).map(
        lambda d: _DISPOSITION_LABEL.get(d, d or "—")
    ),
    "Detail": df.get("detail"),
})


def _disposition_style(col: pd.Series) -> list[str]:
    return [
        f"background-color: {_DISPOSITION_COLOR_HEX.get(v, '#6e7781')}; color: white"
        for v in df.get("disposition", pd.Series(dtype=str))
    ]


if len(display):
    st.dataframe(
        display.style.apply(_disposition_style, subset=["Disposition"]),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.caption("No issues in this run's queue (e.g. a clean empty-queue shutdown).")

# ── Other activity (config#1512 — transparent, not hidden) ─────────────────
if other_closed or other_prs:
    with st.expander(
        f"Other activity in this run's window — NOT attributed to this run's queue "
        f"({len(other_closed)} closes, {len(other_prs)} PRs)"
    ):
        st.caption(
            "Concurrent/unrelated work (a different schedule, an interactive "
            "session) that happened to land during this run's wall-clock "
            "window. Excluded from the metrics above and from the run's own "
            "floor-breach check (config#1512) — listed here for transparency."
        )
        for c in other_closed:
            st.markdown(f"- CLOSED `{c['repo'].rsplit('/', 1)[-1]}#{c['number']}` — {c.get('title', '')}")
        for p in other_prs:
            draft = " [draft]" if p.get("draft") else ""
            st.markdown(f"- PR `{p['repo'].rsplit('/', 1)[-1]}#{p['number']}`{draft} — {p.get('title', '')}")

# ── Chunk log ────────────────────────────────────────────────────────────────
if data.get("chunk_log"):
    with st.expander("Per-chunk log"):
        for line in data["chunk_log"]:
            st.markdown(line)

with st.expander("Raw run artifact JSON"):
    st.json(data)
