"""Pure transforms for the Universe Churn page (no Streamlit) — so the
cross-repo consumer contract with crucible-research
``scoring/universe_membership.py`` (``universe_membership/{date}/membership.json``,
``schema_version=1``) is unit-testable independently of the Streamlit chrome in
``views/55_Universe_Churn.py``.

The question these transforms answer: **who stays in the cut, and what does the
churn look like?** Every other universe surface is a single-snapshot view — the
Board ranks one ``as_of``, the Funnel shows one week's ~900→60 cut, Trends plots
one ticker at a time. None of them can show that a name has been in the top 60
for nine straight weeks, or that two thirds of the cut turns over every Saturday.

Three bases, deliberately kept separate rather than blended — their DIVERGENCE
is the measurement:

  ``scanner_champion_60``    the scanner slot's champion-arm cut (candidate
                              generation, scored on the scanner leaderboard —
                              alpha-engine-config-I7818 renamed this from
                              ``scanner_gate_baseline_60``, which itself
                              replaced ``scanner_candidates``; both retired
                              names are handled below)
  ``attractiveness_top_25``  top 25 by cross-sectional attractiveness rank
  ``attractiveness_top_60``  top 60 by the same rank, count-matched to the
                              champion cut

Measured over 2026-05-29 → 2026-07-24: the attractiveness top-60 retains ~78% of
its members week over week while the scanner cut retains 22-47%. The ranking is
stable; the gate is what churns. A view that averaged the two would hide exactly
that.

Membership lists in the artifact are SORTED, not rank-ordered, so set arithmetic
here compares membership rather than position — a name sliding from rank 3 to
rank 40 is not churn, and must not read as churn.
"""
from __future__ import annotations

import pandas as pd

# Cut name → display label. Keys are the artifact's own cut names (the producer
# owns this vocabulary); an unknown cut still renders, under its raw name.
#
# ORDER IS LOAD-BEARING: `cut_names` returns known cuts in this order, and the
# view defaults its comparison to the first three and its detail cut to the
# first. The champion therefore has to be first. It was not, until
# alpha-engine-config-I6786 — `attractiveness_top_20` had no entry at all, so it
# sorted into the unknown-cut tail and the page opened comparing three cuts,
# none of which was the one the predictor actually resolves its daily universe
# from. A churn page whose default view omits the traded cut answers a question
# nobody asked.
CUT_LABELS = {
    "attractiveness_top_20": "Attractiveness top 20 (CHAMPION — predictor cut)",
    "scanner_top_20": "Scanner top 20 (incumbent challenger)",
    "scanner_champion_60": "Scanner champion cut",
    "attractiveness_top_25": "Attractiveness top 25",
    "attractiveness_top_60": "Attractiveness top 60",
    # Deprecated alias (alpha-engine-config-I7818) — still emitted for one
    # window so a cycle mid-transition still labels legibly instead of
    # falling into the raw-name unknown-cut tail. Not the champion position:
    # ordering above governs the page's default 3-way comparison and must not
    # move for a name that is going away.
    "scanner_gate_baseline_60": "Scanner champion cut (deprecated alias — see scanner_champion_60)",
}

CHURN_COLS = ["as_of", "size", "retained", "new", "dropped", "retention_pct"]


def cut_names(memberships: list[dict]) -> list[str]:
    """Every cut name present across the loaded artifacts, stable-ordered:
    the known cuts first (in CUT_LABELS order), then any others alphabetically.

    Driven off the artifacts rather than a hardcoded list so a cut added by the
    producer appears here without a dashboard change."""
    seen: set[str] = set()
    for m in memberships:
        seen.update((m.get("cuts") or {}).keys())
    known = [c for c in CUT_LABELS if c in seen]
    return known + sorted(seen - set(known))


def cut_label(cut: str) -> str:
    return CUT_LABELS.get(cut, cut)


def membership_series(memberships: list[dict], cut: str) -> list[tuple[str, set[str]]]:
    """``[(as_of, {tickers}), ...]`` for ``cut``, ordered by date.

    Artifacts lacking the cut are SKIPPED rather than contributing an empty set:
    an absent cut means "this cycle's producer didn't emit it", not "the cut was
    empty that week". Rendering the two the same way would invent 100% churn out
    of a schema change.
    """
    series: list[tuple[str, set[str]]] = []
    for m in sorted(memberships, key=lambda x: x.get("run_date") or ""):
        run_date = m.get("run_date")
        block = (m.get("cuts") or {}).get(cut)
        if not run_date or not block:
            continue
        tickers = {str(t).upper() for t in (block.get("tickers") or [])}
        if tickers:
            series.append((run_date, tickers))
    return series


def churn_table(memberships: list[dict], cut: str) -> pd.DataFrame:
    """Per-cycle retained / new / dropped counts vs the PRIOR cycle in the
    series. The first cycle has no predecessor — its retained/new/dropped are
    None (not 0, which would read as "nothing was retained")."""
    series = membership_series(memberships, cut)
    rows = []
    prev: set[str] | None = None
    for as_of, current in series:
        if prev is None:
            rows.append({
                "as_of": as_of, "size": len(current), "retained": None,
                "new": None, "dropped": None, "retention_pct": None,
            })
        else:
            retained = len(current & prev)
            rows.append({
                "as_of": as_of,
                "size": len(current),
                "retained": retained,
                "new": len(current - prev),
                "dropped": len(prev - current),
                "retention_pct": 100.0 * retained / len(prev) if prev else None,
            })
        prev = current
    return pd.DataFrame(rows, columns=CHURN_COLS)


def tenure_table(memberships: list[dict], cut: str) -> pd.DataFrame:
    """Per-ticker tenure: how many cycles it was in the cut, its streak of
    consecutive cycles ending at the most recent one, and whether it is
    currently in. Sorted by weeks-in desc — the persistent core first, the
    one-week names last.

    ``current_streak`` is counted from the END of the series so it answers "how
    long has this name been in the cut *right now*", not "what was its longest
    run ever" — those diverge for a name that was in early, dropped out, and
    came back, and the first question is the one that matters for a cut you are
    trading off."""
    series = membership_series(memberships, cut)
    if not series:
        return pd.DataFrame(columns=["ticker", "weeks_in", "current_streak", "in_latest", "first_seen", "last_seen"])

    dates = [d for d, _ in series]
    all_tickers = sorted({t for _, s in series for t in s})
    latest = series[-1][1]

    rows = []
    for ticker in all_tickers:
        present = [ticker in s for _, s in series]
        streak = 0
        for flag in reversed(present):
            if not flag:
                break
            streak += 1
        seen_dates = [d for d, flag in zip(dates, present) if flag]
        rows.append({
            "ticker": ticker,
            "weeks_in": sum(present),
            "current_streak": streak,
            "in_latest": ticker in latest,
            "first_seen": seen_dates[0],
            "last_seen": seen_dates[-1],
        })
    df = pd.DataFrame(rows)
    return df.sort_values(
        ["weeks_in", "current_streak", "ticker"], ascending=[False, False, True]
    ).reset_index(drop=True)


def membership_matrix(memberships: list[dict], cut: str, top_n: int | None = None) -> pd.DataFrame:
    """Ticker × cycle membership grid (1 = in the cut) for the heatmap, rows
    ordered by tenure. ``top_n`` limits to the longest-tenured names — the tail
    of one-week names is long and adds no signal to the visual.

    Callers that limit MUST say so in the UI; a silently truncated heatmap reads
    as "this is everyone"."""
    series = membership_series(memberships, cut)
    if not series:
        return pd.DataFrame()
    order = tenure_table(memberships, cut)["ticker"].tolist()
    if top_n:
        order = order[:top_n]
    data = {
        as_of: [1 if t in members else 0 for t in order]
        for as_of, members in series
    }
    return pd.DataFrame(data, index=order)


def survivors(memberships: list[dict], cut: str) -> list[str]:
    """Tickers present in EVERY cycle of the series — the persistent core.
    Empty list is a real and interesting answer (the scanner cut has none over
    the recorded history), not a failure."""
    series = membership_series(memberships, cut)
    if not series:
        return []
    return sorted(set.intersection(*(s for _, s in series)))


def tenure_distribution(memberships: list[dict], cut: str) -> pd.DataFrame:
    """``weeks_in → how many distinct tickers`` — the shape of the tail. A cut
    dominated by weeks_in=1 is churning through names; one with mass at the high
    end has a stable core."""
    tenure = tenure_table(memberships, cut)
    if tenure.empty:
        return pd.DataFrame(columns=["weeks_in", "tickers"])
    counts = tenure.groupby("weeks_in").size().reset_index(name="tickers")
    return counts.sort_values("weeks_in").reset_index(drop=True)


def cut_comparison(memberships: list[dict], cuts: list[str]) -> pd.DataFrame:
    """One row per cut: cycles recorded, mean size, mean week-over-week
    retention, and the size of the persistent core. The summary that makes the
    gate-vs-ranking divergence legible at a glance."""
    rows = []
    for cut in cuts:
        churn = churn_table(memberships, cut)
        if churn.empty:
            continue
        retention = churn["retention_pct"].dropna()
        rows.append({
            "cut": cut_label(cut),
            "cycles": len(churn),
            "mean_size": churn["size"].mean(),
            "mean_retention_pct": retention.mean() if not retention.empty else None,
            "survivors": len(survivors(memberships, cut)),
        })
    return pd.DataFrame(
        rows, columns=["cut", "cycles", "mean_size", "mean_retention_pct", "survivors"]
    )


def backfilled_dates(memberships: list[dict]) -> list[str]:
    """Cycles whose artifact was RECONSTRUCTED from historical inputs rather
    than emitted live. Surfaced in the UI because a reconstruction can only be
    as good as the artifacts that survived, and the viewer should know which
    points those are."""
    return sorted(
        m.get("run_date") for m in memberships
        if m.get("backfilled_from") and m.get("run_date")
    )
