"""
Expenses — Alpha Engine (private console)

Every external service Nous Ergon pays for (or draws quota from), one row
each: month-to-date spend, straight-line/forecast month-end projection, and
whether the month is trending **over or under** its budget/quota. Covers AWS
(Cost Explorer), Anthropic API, OpenRouter, DeepSeek, Neon, GitHub Actions
(org + personal, separate meters), plus flat subscriptions (Claude Max) —
future providers appear automatically once added to the collector or, for
flat subscriptions, to the budgets SSoT alone.

Source: ``expenses/latest.json``, written twice daily (00:15 / 12:15 UTC) by
the alpha-engine-data ``expense-collector`` Lambda. Budgets/quotas SSoT:
``s3://alpha-engine-research/config/expense_budgets.json`` (operator-edited;
null budget ⇒ the row shows spend without an over/under call). The weekly
Claude-Max WET pacing gauge stays on the LLM Usage tab — this page is the
$-denominated monthly view.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from loaders.s3_loader import load_expense_reconciliation, load_expense_report
from shared.expense_view import (
    as_of_age_hours,
    error_rows,
    provider_table_rows,
    reconciliation_table_rows,
    usd,
)

# How many closed months back the Reconciliation section looks — enough to
# catch a month whose reconciliation run landed late without querying every
# period that ever existed.
RECONCILIATION_LOOKBACK_MONTHS = 3

st.divider()
st.title("Expenses")
st.caption(
    "Month-to-date spend across every external provider, with month-end "
    "projection and over/under pacing vs budget. Produced twice daily by the "
    "`expense-collector` Lambda (alpha-engine-data). Budgets/quotas live in "
    "`s3://alpha-engine-research/config/expense_budgets.json` — edit that "
    "object to set a budget, change an included-minutes quota, or add a flat "
    "subscription line."
)

doc = load_expense_report()

if not doc:
    st.info(
        "No expense rollup yet — the `expense-collector` Lambda hasn't run. "
        "Deploy/bootstrap it from alpha-engine-data: "
        "`bash infrastructure/lambdas/expense-collector/deploy.sh --bootstrap`, "
        "then `--smoke` to write the first rollup."
    )
    st.stop()

# --- freshness + collector-side warnings (loud, never silent) -----------------
age_h = as_of_age_hours(doc)
if age_h is None:
    st.warning("Rollup `as_of` timestamp is unparseable — treat all figures as stale.")
elif age_h > 26:
    st.warning(
        f"Rollup is **{age_h:,.0f}h old** (expected ≤ ~12h) — the collector "
        "may be failing; check the `alpha-engine-expense-collector` Lambda logs."
    )

for w in doc.get("warnings", []):
    st.warning(w)

errs = error_rows(doc)
if errs:
    st.error(
        "**Provider collection errors** (rows excluded from totals — figures "
        "below are incomplete):\n"
        + "\n".join(f"- **{p.get('label', p.get('key'))}**: {p.get('error')}" for p in errs)
    )

# --- headline -----------------------------------------------------------------
totals = doc.get("totals", {})
elapsed = float(doc.get("month_elapsed_frac") or 0.0)
h1, h2, h3, h4 = st.columns(4)
h1.metric(
    f"MTD total ({doc.get('period', '?')})", usd(totals.get("mtd_usd")),
    help="Sum of all healthy provider rows (status ok/fixed). Error rows are "
         "excluded and flagged above.",
)
h2.metric(
    "Projected month-end", usd(totals.get("projected_usd")),
    help="Per-provider projections summed: AWS uses the Cost Explorer "
         "forecast; diff-based providers extrapolate straight-line, forward "
         "only from their measurement window.",
)
h3.metric(
    "Budgeted (set rows)", usd(totals.get("budget_usd")),
    help="Sum of monthly_budget_usd + fixed_monthly_usd over rows that have "
         "one set. Rows with a null budget contribute spend but no budget.",
)
h4.metric(
    "Month elapsed", f"{elapsed * 100:,.0f}%",
    help=f"Calendar-month (UTC) fraction elapsed as of {doc.get('as_of', '?')}.",
)
st.progress(min(elapsed, 1.0), text=f"{elapsed * 100:,.0f}% of {doc.get('period', '?')} elapsed")

over = [p for p in doc.get("providers", []) if p.get("pace") == "over"]
if over:
    names = ", ".join(f"**{p.get('label', p.get('key'))}**" for p in over)
    st.error(f"Trending OVER budget/quota this month: {names} — detail in the table below.")

# --- GHA public vs private breakdown -------------------------------------------
# Guardrail against a real 2026-07-17 incident: a repo's public/private
# GitHub Actions minutes were misclassified (inferred from appearing in a
# billing pull rather than checking actual visibility), leading to an
# unnecessary self-hosted-runner build for 6 actually-public repos. Public
# repos get GHA free/unlimited — only PRIVATE-repo minutes draw the included
# quota. This table surfaces every repo's live-verified visibility + minutes
# so a misclassification is visible at a glance, not inferred.
gha_rows = [p for p in doc.get("providers", []) if p.get("key", "").startswith("github_")
            and p.get("detail", {}).get("gha_by_repo")]
if gha_rows:
    st.subheader("GitHub Actions — public vs private", divider="gray")
    st.caption(
        "Public repos get GHA hosted runners free and unlimited — only "
        "PRIVATE-repo minutes draw the included-minutes quota. Visibility is "
        "checked live against the GitHub API each collector run, not inferred."
    )
    g1, g2 = st.columns(2)
    total_private = sum(p["detail"].get("gha_private_minutes", 0) for p in gha_rows)
    total_public = sum(p["detail"].get("gha_public_minutes", 0) for p in gha_rows)
    g1.metric("Private-repo minutes (quota-relevant)", f"{total_private:,.0f}")
    g2.metric("Public-repo minutes (free)", f"{total_public:,.0f}")
    repo_records = [
        {"account": p.get("label", p.get("key")), **r}
        for p in gha_rows
        for r in p["detail"]["gha_by_repo"]
    ]
    repo_table = pd.DataFrame(repo_records).sort_values("minutes", ascending=False)
    st.dataframe(repo_table, use_container_width=True, hide_index=True)

# --- per-provider table -------------------------------------------------------
st.subheader("Providers", divider="gray")
table = pd.DataFrame(provider_table_rows(doc))
st.dataframe(table, use_container_width=True, hide_index=True)
st.caption(
    "Pace: 🟢/🔴 = projected month-end vs budget (or quota, e.g. GitHub "
    "Actions included minutes; Neon data-transfer GB). 💳 = flat "
    "subscription. — = no budget set for the row (set one in "
    "`config/expense_budgets.json`)."
)

# --- reconciliation (prior months) --------------------------------------------
# Month-close true-up (alpha-engine-config#2849): each closed month's
# provider-authoritative FINAL actual vs what was last projected/accrued for
# it — the backward-looking check that catches drift (like the AWS forecast
# double-count, config-era fix nousergon-data-PR910) systematically instead of
# by eyeballing. Producer: alpha-engine-data expense-collector's reconcile
# mode (~03:00 UTC, 2nd of each month), writing
# `expenses/reconciliation/{YYYY-MM}.json`.
st.subheader("Reconciliation (prior months)", divider="gray")
st.caption(
    "Closed-month true-up: each provider's FINAL actual (re-queried after "
    "providers finalize the prior month) vs what was last projected/accrued "
    "for it. Rows past the drift threshold are flagged ⚠️."
)


def _prior_periods(current_period: str, n: int) -> list[str]:
    y, m = (int(x) for x in current_period.split("-"))
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        out.append(f"{y:04d}-{m:02d}")
    return out


current_period = doc.get("period") or date.today().strftime("%Y-%m")
found_any = False
for period in _prior_periods(current_period, RECONCILIATION_LOOKBACK_MONTHS):
    recon_doc = load_expense_reconciliation(period)
    if not recon_doc:
        continue
    found_any = True
    flagged = recon_doc.get("flagged") or []
    with st.expander(f"{period}" + (f" — {len(flagged)} flagged" if flagged else ""),
                     expanded=bool(flagged)):
        if flagged:
            names = ", ".join(f"**{k}**" for k in flagged)
            st.error(f"Drift beyond threshold this closed month: {names}")
        recon_table = pd.DataFrame(reconciliation_table_rows(recon_doc))
        st.dataframe(recon_table, use_container_width=True, hide_index=True)

if not found_any:
    st.info(
        "No reconciliation data yet for the last "
        f"{RECONCILIATION_LOOKBACK_MONTHS} closed months — the collector's "
        "`reconcile` mode runs ~03:00 UTC on the 2nd of each month, after the "
        "prior month's providers finalize."
    )

# --- drill-down ---------------------------------------------------------------
st.subheader("Detail", divider="gray")
for p in doc.get("providers", []):
    label = p.get("label", p.get("key", "?"))
    with st.expander(f"{label} — {usd(p.get('mtd_cost_usd'))} MTD"):
        meta_cols = st.columns(3)
        meta_cols[0].write(f"**Status:** {p.get('status')}")
        meta_cols[1].write(f"**Source:** {p.get('source') or '—'}")
        meta_cols[2].write(f"**Pace:** {p.get('pace') or '—'}")
        if p.get("quota"):
            q = p["quota"]
            proj = q.get("projected")
            st.write(
                f"**Quota:** {q.get('used'):,} / "
                f"{q.get('limit') if q.get('limit') is not None else '?'} "
                f"{q.get('unit', '')}"
                + (f" — projected {proj:,.0f} by month-end" if proj is not None else "")
            )
        if p.get("note"):
            st.info(p["note"])
        if p.get("error"):
            st.error(p["error"])
        if p.get("detail"):
            st.json(p["detail"], expanded=False)
