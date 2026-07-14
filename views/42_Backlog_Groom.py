"""
Backlog Groom — Alpha Engine (private console)

Operator audit surface for the complexity-tier backlog groom (config#1495,
#1512). As of the 2026-07-07 move to demand-driven dispatch (config#1933),
groom runs fire based on backlog demand rather than a fixed daily cadence.
Every run writes a per-run artifact to
``s3://alpha-engine-research/groom/{date}/{run_id_or_hhmmss}.json``
(``groom_driver.py::write_run_artifact``) — this page is its consumer.
Standalone PR-sweep runs (``groom_run.sh --mode sweep``) write here too as of
config#1986, via ``scripts/write_sweep_artifact.py`` — same schema,
``run_kind="sweep"``, no issue queue.

The point of this page: answer "did the model actually think about each
issue?" from VERIFIABLE artifacts, never a self-report. Each queued issue's
disposition (closed / pr_opened / commented / untouched) is cross-referenced
against real GitHub state at write time — a PR link, a close reason, or the
actual latest comment — not a claim the agent made about itself. This is the
same ground-truth-over-self-report principle the run-attribution fix
(config#1512) applies to the PR/close counts in the GitHub digest issue.

Complementary to **Watch Status** (failure-event timeline + aggregate efficacy
for the SF Watch and CI Watch resilience agents) — this page is the per-run
activity log for the groom pipeline.

Complexity tiers: low/mid run on Sonnet (dedicated queue), high runs on Sonnet
(separate high-only queue) — both leverage Sonnet's reasoning depth with
dedicated attention budgets per tier.

Page layout (2026-07-14 readability redesign):

1. **Slot decisions** — one TABLE row per dispatcher enumerate-then-decide
   record (``groom/decisions/{date}/trigger-*.json``, written BEFORE any
   spot spend), showing the per-tier actionable backlog counts the
   dispatcher saw and what launched/deferred and WHY. Scheduled slots that
   are due but have no record render a loud ⚠️ row (broken-scheduler
   signal, config#1935) — time-aware, so a slot later today is never
   falsely flagged, and ad-hoc manual triggers never spawn phantom
   missing-record rows for other days.
2. **Groom health** — trailing-7-day KPI roll-up across coverage runs, plus
   the latest weekly disposition-quality audit (config#2153) — the
   CORRECTNESS check on terminal dispositions, previously invisible here.
3. **Trends** — actionable backlog by tier over time (is the backlog
   draining?), WET per engaged issue by tier, and per-run coverage.
4. **Run history** + per-run detail (dispositions, digest, chunk log).
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.groom_efficiency import (  # noqa: E402
    compute_efficiency,
    match_usage_for_run,
)
from loaders.groom_trends import (  # noqa: E402
    TIER_COLOR,
    TIER_ORDER,
    audit_summary,
    decision_table_rows,
    demand_trend_rows,
    runs_trend_rows,
    window_kpis,
)
from loaders.s3_loader import (  # noqa: E402
    KNOWN_GROOM_SLOTS,
    list_groom_audit_keys,
    list_groom_decision_keys,
    list_groom_run_keys,
    list_groom_usage_records,
    load_groom_audit,
    load_groom_decision,
    load_groom_run,
    normalize_groom_decision_record,
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


_DECISIONS_HISTORY_DAYS = 7


def _load_decision_records() -> tuple[list[tuple[str, dict, list[dict]]], list[str]]:
    """(readable records as ``(key, raw, normalized_boxes)``, unreadable keys)
    for the trailing decisions window. Loaders are cached, so the trends
    section re-reading the same keys costs nothing extra.
    """
    records: list[tuple[str, dict, list[dict]]] = []
    unreadable: list[str] = []
    for k in list_groom_decision_keys(days=_DECISIONS_HISTORY_DAYS):
        raw = load_groom_decision(k)
        if raw is None:
            unreadable.append(k)
            continue
        records.append((k, raw, normalize_groom_decision_record(raw)))
    return records, unreadable


def _render_slot_decisions_strip(
    records: list[tuple[str, dict, list[dict]]],
    unreadable: list[str],
) -> None:
    """Slot decisions table (config#1933/#1935) — one row per dispatcher
    enumerate-then-decide record in the trailing window, newest first.

    A scheduled slot that is DUE (UTC slot time + grace passed) with no
    record renders a loud ⚠️ NO RECORD row — never silently blanked, even
    with zero records anywhere (the pre-bootstrap cold-start state).
    """
    st.subheader("Slot decisions")
    st.caption(
        "One row per dispatcher decision — the per-tier actionable backlog "
        "it counted (low/mid/high), what it launched, and why the rest was "
        "deferred. Written BEFORE any spot spend, so a deferral costs "
        "nothing. Floor = 8 actionable issues per tier; escape valves: P0 "
        "present or 72h wait. (config#1933, config#1935)"
    )

    now = _dt.datetime.now(_dt.timezone.utc)
    rows = decision_table_rows(
        records, known_slots=KNOWN_GROOM_SLOTS, now=now,
        days=_DECISIONS_HISTORY_DAYS,
    )

    if not rows:
        # Nothing observed AND nothing due-but-missing — the pre-bootstrap
        # cold-start state (the dispatcher hasn't written its first record
        # yet). Render an explicit notice, not a blank section.
        st.warning(
            "⚠️ No slot decision records found in the last "
            f"{_DECISIONS_HISTORY_DAYS} days and no scheduled slot is due "
            "without one — either the dispatcher hasn't written its first "
            "record yet (cold start) or listing failed. Expected slots: "
            + ", ".join(KNOWN_GROOM_SLOTS)
        )
        return

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "low": st.column_config.NumberColumn(
                "low", help="Actionable low-complexity issues at decision time"),
            "mid": st.column_config.NumberColumn(
                "mid", help="Actionable mid-complexity issues at decision time"),
            "high": st.column_config.NumberColumn(
                "high", help="Actionable high-complexity issues at decision time"),
            "Deferred": st.column_config.TextColumn(
                "Deferred", help="Dispatcher's own reason, verbatim", width="large"),
        },
    )
    if any(r["Status"] == "⚠️ NO RECORD" for r in rows):
        st.error(
            "⚠️ NO RECORD row(s) above: no decision record for a due "
            "scheduled slot. Three known causes, all currently record-less: "
            "(a) scheduler outage — the Lambda never invoked; (b) pre-boot "
            "PACE-GATE SKIP — fires BEFORE enumeration and pings Telegram "
            "silently (config-I2461 gotcha class); (c) enumeration failure "
            "(`demand_all_failed`). Disambiguate in CloudWatch: "
            "`/aws/lambda/alpha-engine-scheduled-groom-dispatcher`, filter "
            '"pace gate" / "demand trigger". Making skips (b)/(c) write '
            "records too is config#2540 — once shipped, this alert means "
            "OUTAGE unambiguously."
        )
    if unreadable:
        st.warning(
            f"{len(unreadable)} decision record(s) unreadable: "
            + ", ".join(f"`{k}`" for k in unreadable)
        )


st.title("🧹 Backlog Groom")
st.caption(
    "Per-run audit trail for the complexity-tier backlog groom — every "
    "issue's disposition cross-referenced against real GitHub state, not a "
    "self-report. (config#1495, #1512)"
)

_decision_records, _decision_unreadable = _load_decision_records()
_render_slot_decisions_strip(_decision_records, _decision_unreadable)

keys = list_groom_run_keys()
if not keys:
    st.info(
        "🛈 No groom run artifacts found yet. Written by `groom_driver.py` "
        "starting with the config#1512 follow-up — older runs (and any run "
        "before that ships) have no artifact here; check the `groom-digest` "
        "GitHub issues on `alpha-engine-config` for their record instead."
    )
    st.stop()

# ── Load recent runs once; every downstream section (KPIs, trends, history)
# reuses this list. Loaders are @st.cache_data'd per key, so this fans out to
# at most _TREND_N cached S3 GETs and re-renders free within the TTL. ────────
_TREND_N = 30
_HISTORY_N = 12
_DIGEST_ISSUE_URL = "https://github.com/nousergon/alpha-engine-config/issues/{n}"
usage_index = list_groom_usage_records()
assigned_usage: set[str] = set()
loaded_runs: list[tuple[str, dict, dict]] = []  # (key, run, efficiency)
run_efficiency: dict[str, dict] = {}
for k in keys[:_TREND_N]:
    run = load_groom_run(k)
    if not run:
        continue
    usage = match_usage_for_run(k, run, usage_index, assigned=assigned_usage)
    if usage:
        assigned_usage.add(usage["key"])
    eff = compute_efficiency(run, run.get("issues") or [], usage)
    run_efficiency[k] = eff
    loaded_runs.append((k, run, eff))

_now_utc = _dt.datetime.now(_dt.timezone.utc)
trend_rows = runs_trend_rows(loaded_runs)

# ── Groom health — trailing-window KPIs + disposition-quality audit ─────────
st.subheader("Groom health — trailing 7 days")
kpis = window_kpis(trend_rows, now=_now_utc, days=7)
k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Coverage runs", kpis["runs"])
k2.metric("Engaged / queued", f"{kpis['engaged']} / {kpis['queued']}",
          help="Issues dispositioned vs issues queued, summed across the "
               "window's coverage runs.")
k3.metric("✅ Closed · 🔧 PRs", f"{kpis['closed']} · {kpis['prs']}",
          help="Hard outcomes attributed to groom runs' own queues.")
k4.metric("Undispositioned", kpis["undispositioned"],
          delta=None if not kpis["undispositioned"] else "should be 0",
          delta_color="inverse",
          help="Queued issues that hit run wind-down with no terminal "
               "disposition — the coverage gap to watch.")
k5.metric("Floor breaches", kpis["floor_breaches"],
          delta=None if not kpis["floor_breaches"] else "self-taper",
          delta_color="inverse")
mwpe = kpis["median_wet_per_engaged_k"]
k6.metric("Median WET/engaged", f"{mwpe:.0f}K" if mwpe is not None else "—",
          help="Median token cost per dispositioned issue across the "
               "window's runs.")
if kpis["max_turns_chunks"]:
    st.warning(
        f"⚠️ {kpis['max_turns_chunks']} chunk-agent invocation(s) exhausted "
        "max_turns in the window — lost mid-chunk work that re-pays context "
        "on re-queue (config#2148). Persistent counts here mean the "
        "chunk-size/turn-budget calibration is off for a tier (auto-filed "
        "as the TURN-BUDGET EXCEEDED issues, e.g. config#2192)."
    )

# Disposition-quality audit (config#2153) — correctness, not coverage: were
# the closes/gates/downgrades RIGHT? Sampled weekly, judged from live gh
# state by a Haiku auditor; bad closes get reopened + groom:defect flagged.
_audit_keys = list_groom_audit_keys()
_audit_key = _audit_keys[0] if _audit_keys else None
_audit = audit_summary(
    _audit_key, load_groom_audit(_audit_key) if _audit_key else None,
    now=_now_utc,
)
_AUDIT_BADGE = {
    "ok": "🟢", "warn": "🟡", "fail": "🔴", "stale": "⚠️", "missing": "⚠️",
}
if _audit["status"] == "missing":
    st.caption(
        f"{_AUDIT_BADGE['missing']} Disposition-quality audit: {_audit['detail']}."
    )
else:
    _audit_line = (
        f"{_AUDIT_BADGE[_audit['status']]} **Disposition-quality audit** "
        f"(weekly sampled, config#2153) — last ran **{_audit['date']}** "
        f"({_audit['age_days']}d ago): {_audit['pass_count']} pass / "
        f"{_audit['fail_count']} fail / {_audit['error_count']} error "
        f"across {_audit['sampled']} sampled terminal dispositions."
    )
    if _audit["status"] == "stale":
        st.warning(_audit_line + " **Audit is STALE (>8d)** — the daily "
                                 "low-only box that carries it hasn't "
                                 "completed a pass.")
    elif _audit["status"] == "fail":
        st.error(_audit_line + " ≥2 FAILs is the page threshold — see the "
                               "`groom/audit/` artifact for findings.")
    elif _audit["status"] == "warn":
        st.warning(_audit_line)
    else:
        st.markdown(_audit_line)

# ── Trends — cross-run series the per-run tables can't show ────────────────
st.subheader("Trends")
_tier_palette = [TIER_COLOR[t] for t in TIER_ORDER]

t_left, t_right = st.columns(2)
with t_left:
    st.markdown("**Actionable backlog by tier** — at each dispatcher decision")
    demand = demand_trend_rows([(k, raw) for k, raw, _ in _decision_records])
    if demand:
        demand_df = (pd.DataFrame(demand)
                     .set_index("decided_at")[list(TIER_ORDER)])
        st.line_chart(demand_df, color=_tier_palette, height=240)
        st.caption("Falling lines = the groom is draining the backlog "
                   "faster than filing refills it.")
    else:
        st.caption("No decision records in the window yet.")

with t_right:
    st.markdown("**WET per engaged issue (K tokens)** — by run, per tier")
    wpe_rows = [r for r in trend_rows
                if r.get("wet_per_engaged_k") is not None
                and r.get("run_start") is not None]
    if wpe_rows:
        wpe_df = pd.DataFrame([
            {"run_start": r["run_start"],
             **{t: (r["wet_per_engaged_k"] if r["tier"] == t else None)
                for t in TIER_ORDER}}
            for r in wpe_rows
        ]).set_index("run_start")
        st.scatter_chart(wpe_df, color=_tier_palette, height=240)
        st.caption("Token cost per dispositioned issue — the primary "
                   "efficiency ratio. Alert ceilings per tier live in "
                   "`groom_efficiency.py` (low ≲80K, mid ≲500K, high ≲700K).")
    else:
        st.caption("No runs with WET attribution in the window yet.")

_cov_rows = [r for r in trend_rows if r.get("run_start") is not None]
if _cov_rows:
    st.markdown("**Per-run coverage** — engaged vs queued issues")
    cov_df = pd.DataFrame([
        {"run_start": r["run_start"], "engaged": r["engaged"],
         "queued": r["queued"]}
        for r in _cov_rows
    ]).set_index("run_start")
    st.line_chart(cov_df, color=["#2a78d6", "#6e7781"], height=200)
    st.caption("A persistent gap between the lines = issues repeatedly "
               "queued but not dispositioned (undispositioned / "
               "dropped-at-cap) — see the Undisp column below.")

# ── Run history — one summary row per recent run (2026-07-02 operator ask:
# see the digest + a per-run summary WITHOUT opening GitHub). ────────────────
st.subheader("Run history")
history_rows = []
for k, run, eff in loaded_runs[:_HISTORY_N]:
    run_kind = run.get("run_kind", "coverage")
    run_issues = run.get("issues") or []
    counts = {d: sum(1 for i in run_issues if i.get("disposition") == d)
              for d in ("closed", "pr_opened", "commented", "untouched")}
    soft = run.get("soft_limit_min") or 0
    digest_n = run.get("digest_issue") or 0
    # compute_efficiency already folds floor_fail into alerts — no re-derive.
    flags = "; ".join(eff.get("alerts") or [])
    wet = eff.get("wet")
    wpe = eff.get("wet_per_engaged")
    undisp = run.get("undispositioned")
    history_rows.append({
        "Run": _run_label(k),
        "Kind": "🔧 sweep" if run_kind == "sweep" else "🧹 coverage",
        "Tier": run.get("issue_filter", "—"),
        "Coverage": ("—" if run_kind == "sweep" else
                     f"{run.get('processed', len(run_issues))}/{run.get('total_issues', len(run_issues))}"),
        # None (not "—") so the column stays nullable-numeric for Arrow.
        "Undisp": (None if run_kind == "sweep" or undisp is None else int(undisp)),
        "✅ closed": counts["closed"],
        "🔧 PRs": counts["pr_opened"],
        "💬 comm.": counts["commented"],
        "WET": f"{wet/1e6:.1f}M" if wet is not None else "—",
        "WET/eng": f"{wpe/1e3:.0f}K" if wpe is not None else "—",
        "Budget (min)": f"{run.get('elapsed_min') or 0}/{soft}" if soft else "—",
        "Flags": flags.strip("; ") or "✅",
        "Digest": _DIGEST_ISSUE_URL.format(n=digest_n) if digest_n else None,
    })

if history_rows:
    st.caption(
        "**Undisp** = queued issues left with no terminal disposition at "
        "wind-down. **WET/eng** = tokens per dispositioned issue (run "
        "artifacts join to `claude_code_usage/groom/` by date + nearest "
        "end-time when the artifact carries no measured WET). **Flags** "
        "roll up floor breaches + efficiency alerts vs tier baselines. "
        "Stop reasons, chunk logs, and per-issue dispositions live in the "
        "run detail below."
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
sel_run_kind = data.get("run_kind", "coverage")

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
              help="(closes + PRs) / engaged — comment-only runs skew low on verify-heavy tiers.")
    r1, r2, r3 = st.columns(3)
    dr = sel_eff.get("disposition_rate")
    r1.metric("Disposition rate", f"{dr*100:.0f}%" if dr is not None else "—",
              help="Engaged / queued — coverage quality.")
    cr2 = sel_eff.get("comment_rate")
    r2.metric("Comment-only rate", f"{cr2*100:.0f}%" if cr2 is not None else "—",
              help="Commented / engaged — high on verify-heavy (high-tier) runs.")
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

if sel_run_kind == "sweep":
    # config#1986: a standalone PR-sweep run has no issue queue, so the
    # coverage-loop's Engaged/floor + Queue coverage framing doesn't apply —
    # just show elapsed vs soft budget. PRs-swept detail lives in the report
    # below (Run digest), which IS this run's disposition record.
    st.caption(
        f"🔧 Standalone PR-sweep run — elapsed **{data.get('elapsed_min', 0)}** / "
        f"**{data.get('soft_limit_min', 0)}** min soft budget. No issue queue "
        "(sweep runs bring existing PRs to merge-ready, they don't triage "
        "issues) — see the report below for what was swept."
    )
else:
    # ── Budget vs consumed (config#1569; schema_version >= 2 only — older ──
    # runs never captured these fields, so soft_limit_min is 0/absent). ─────
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

    # ── Queue-shape detail (schema v9 fields — absent on older artifacts,
    # in which case the whole row is skipped rather than rendered as "—"). ──
    if any(data.get(f) is not None for f in
           ("undispositioned", "dropped_at_cap", "gated_excluded",
            "max_turns_chunks", "fresh_skipped")):
        q1, q2, q3, q4, q5 = st.columns(5)
        q1.metric("Undispositioned", data.get("undispositioned", "—"),
                  help="Queued issues left with no terminal disposition at "
                       "wind-down — never folded into 'engaged' (config#1804).")
        q2.metric("Dropped at cap", data.get("dropped_at_cap", "—"),
                  help="Issues dropped after exhausting the adaptive "
                       "re-queue attempt cap.")
        q3.metric("Gated at enumeration", data.get("gated_excluded", "—"),
                  help="Issues excluded before queueing — gate:* labels "
                       "without gate-due (config#1805).")
        q4.metric("max_turns chunks", data.get("max_turns_chunks", "—"),
                  help="Chunk-agent invocations that exhausted their turn "
                       "budget mid-chunk — lost-work signal (config#2148).")
        q5.metric("Fresh-skipped", data.get("fresh_skipped", "—"),
                  help="Issues engaged by a recent groom with no new "
                       "activity since — dropped from this queue "
                       "(config#1893).")

    if data.get("floor_fail"):
        st.error(
            "⚠️ FAIL-LOUD FLOOR BREACHED — this run stopped with budget+time "
            "remaining but delivered fewer than the minimum work-items. Treated "
            "as a self-taper failure; see the run digest below."
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
elif sel_run_kind == "sweep":
    st.caption("This is a standalone PR-sweep run — it has no issue queue by design (see the report above for PRs swept).")
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
