"""
Signal data loading and flattening utilities for the Alpha Engine Dashboard.
Wraps s3_loader functions to provide structured DataFrames from signals.json.
"""

from datetime import date, datetime

import pandas as pd
import streamlit as st

from loaders.s3_loader import (
    load_config,
    list_s3_prefixes,
    download_s3_json,
)


def _research_bucket() -> str:
    return load_config()["s3"]["research_bucket"]


def _signals_key(date_str: str) -> str:
    return load_config()["paths"]["signals"].format(date=date_str)


def _cio_key(date_str: str) -> str:
    # Fall back to the canonical path if the deployed config.yaml (hydrated
    # from SSM, not the repo) predates this key — keeps the panel working
    # without a coordinated config/SSM update.
    template = (
        load_config().get("paths", {}).get("cio_decisions")
        or "archive/agent_runs/{date}/cio.json"
    )
    return template.format(date=date_str)


# A candidate "enters" the population on either a rubric ADVANCE or a
# floor-enforced ADVANCE_FORCED. The CIO emits BOTH literals (the
# min_new_entrants force-fill in alpha-engine-research tags promotions
# ADVANCE_FORCED); any consumer that matches only "ADVANCE" silently drops
# forced entrants. Match the set everywhere so this monitor stays correct
# the day the floor logic fires.
ADVANCE_DECISIONS = frozenset({"ADVANCE", "ADVANCE_FORCED"})


def _ttl(key: str) -> int:
    return load_config()["cache_ttl"].get(key, 900)


# ---------------------------------------------------------------------------
# Date discovery
# ---------------------------------------------------------------------------


@st.cache_data(ttl=900)
def get_available_signal_dates() -> list[str]:
    """
    List s3://alpha-engine-research/signals/ and return all available date
    strings (YYYY-MM-DD) sorted descending (most recent first).
    """
    prefix = "signals/"
    dates = list_s3_prefixes(_research_bucket(), prefix)
    return sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# Signal loading
# ---------------------------------------------------------------------------


@st.cache_data(ttl=900)
def load_signals(date_str: str | None = None) -> dict | None:
    """
    Load signals.json for *date_str* (YYYY-MM-DD). Defaults to today's date.
    Returns the parsed dict or None if not found.
    """
    if date_str is None:
        date_str = date.today().isoformat()
    key = _signals_key(date_str)
    return download_s3_json(_research_bucket(), key)


# ---------------------------------------------------------------------------
# Flattening helpers
# ---------------------------------------------------------------------------


def _extract_sub_scores(entry: dict) -> tuple[float | None, float | None, float | None]:
    """
    Extract (technical, news, research) sub-scores from a signal entry.
    Handles both nested sub_scores dict and flat top-level keys.
    """
    sub = entry.get("sub_scores", {})
    if isinstance(sub, dict) and sub:
        technical = sub.get("technical")
        news = sub.get("news")
        research = sub.get("research")
    else:
        technical = entry.get("technical")
        news = entry.get("news")
        research = entry.get("research")
    return technical, news, research


def signals_to_df(signals_data: dict | None) -> pd.DataFrame:
    """
    Flatten the universe[] list from signals_data into a DataFrame.

    Columns: ticker, sector, signal, rating, score, conviction,
             technical, news, research, price_target_upside, thesis_summary, stale
    """
    if not signals_data:
        return pd.DataFrame()

    universe = signals_data.get("universe", [])
    if not universe:
        return pd.DataFrame()

    rows = []
    for entry in universe:
        technical, news, research = _extract_sub_scores(entry)
        rows.append(
            {
                "ticker": entry.get("ticker"),
                "sector": entry.get("sector"),
                "signal": entry.get("signal"),
                "rating": entry.get("rating"),
                "score": entry.get("score"),
                "conviction": entry.get("conviction"),
                "technical": technical,
                "news": news,
                "research": research,
                "price_target_upside": entry.get("price_target_upside"),
                "thesis_summary": entry.get("thesis_summary"),
                "stale": entry.get("stale", False),
            }
        )

    df = pd.DataFrame(rows)
    # Ensure numeric columns are numeric
    for col in ["score", "conviction", "technical", "news", "research", "price_target_upside"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_buy_candidates_df(signals_data: dict | None) -> pd.DataFrame:
    """
    Flatten the population stocks from signals_data into a DataFrame.
    Uses universe[] (buy_candidates was merged into universe).
    """
    if not signals_data:
        return pd.DataFrame()

    candidates = signals_data.get("universe", [])
    if not candidates:
        return pd.DataFrame()

    rows = []
    for entry in candidates:
        technical, news, research = _extract_sub_scores(entry)
        rows.append(
            {
                "ticker": entry.get("ticker"),
                "sector": entry.get("sector"),
                "signal": entry.get("signal"),
                "rating": entry.get("rating"),
                "score": entry.get("score"),
                "conviction": entry.get("conviction"),
                "technical": technical,
                "news": news,
                "research": research,
                "price_target_upside": entry.get("price_target_upside"),
                "thesis_summary": entry.get("thesis_summary"),
                "stale": entry.get("stale", False),
            }
        )

    df = pd.DataFrame(rows)
    for col in ["score", "conviction", "technical", "news", "research", "price_target_upside"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_sector_ratings_df(signals_data: dict | None) -> pd.DataFrame:
    """
    Flatten the sector_ratings dict from signals_data into a DataFrame.
    Returns columns: sector, rating (and any other keys present).
    """
    if not signals_data:
        return pd.DataFrame()

    sector_ratings = signals_data.get("sector_ratings", {})
    if not sector_ratings:
        return pd.DataFrame()

    if isinstance(sector_ratings, dict):
        rows = []
        for sector, value in sector_ratings.items():
            if isinstance(value, dict):
                row = {"sector": sector, **value}
            else:
                row = {"sector": sector, "rating": value}
            rows.append(row)
        return pd.DataFrame(rows)
    elif isinstance(sector_ratings, list):
        return pd.DataFrame(sector_ratings)

    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Signal count helpers
# ---------------------------------------------------------------------------


def get_signal_counts(signals_data: dict | None) -> dict:
    """
    Return a dict with counts for ENTER, EXIT, REDUCE, HOLD signals
    from the universe list.
    """
    df = signals_to_df(signals_data)
    counts = {"ENTER": 0, "EXIT": 0, "REDUCE": 0, "HOLD": 0}
    if df.empty or "signal" not in df.columns:
        return counts
    vc = df["signal"].value_counts()
    for k in counts:
        counts[k] = int(vc.get(k, 0))
    return counts


# ---------------------------------------------------------------------------
# Population flow / new-entrant tracking
# ---------------------------------------------------------------------------
#
# Surfaces *why* a given week added (or did not add) new names to the tracked
# population. Background: the 2026-06-05 weekly run produced 0 net-new
# entrants — correctly, because the CIO rejected all 7 fresh candidates
# (max conviction 40 vs the ~60 bar prior entrants cleared) as low-conviction
# names in macro-underweight sectors. That outcome was previously invisible
# in the console; these helpers make the new-entrant pipeline a tracked,
# first-class panel so a chronic zero-add streak (saturation) is caught early.
#
# A "new candidate" for week d = a CIO candidate whose ticker was NOT in the
# prior week's held population. "Net-new entrants" = new candidates the CIO
# advanced (ADVANCE or ADVANCE_FORCED). Computed from the CIO decision
# archive (archive/agent_runs/{date}/cio.json) cross-referenced against the
# prior week's signals.json population.


def _cio_output(raw: dict | None) -> dict | None:
    """Unwrap the persisted CIO agent-run envelope.

    The archive shape is {"run_date", "agent_id", "output": {ic_decisions,
    advanced_tickers, entry_theses}}. Older / direct payloads may already be
    the output dict. Returns the output dict (with ic_decisions) or None.
    """
    if not isinstance(raw, dict):
        return None
    out = raw.get("output", raw)
    if isinstance(out, dict) and "ic_decisions" in out:
        return out
    return None


def population_tickers(signals_data: dict | None) -> set[str]:
    """Set of held-population tickers from a signals.json dict.

    Population entries may be bare ticker strings or dicts with a ``ticker``
    key — handle both.
    """
    if not signals_data:
        return set()
    out: set[str] = set()
    for p in signals_data.get("population", []) or []:
        ticker = p.get("ticker") if isinstance(p, dict) else p
        if ticker:
            out.add(ticker)
    return out


def entrant_flow_row(
    date_str: str,
    cio_output: dict | None,
    prior_pop: set[str],
    cur_pop: set[str],
    *,
    have_prior: bool,
) -> dict | None:
    """Compute one week's new-entrant stats (pure).

    ``have_prior`` distinguishes "prior population is genuinely empty" from
    "prior week's signals.json was unavailable" — when the prior baseline is
    missing we cannot classify new-vs-held, so net-new / new-candidate counts
    are reported as None rather than a misleading number.
    """
    if not cio_output:
        return None
    decisions = cio_output.get("ic_decisions", []) or []
    if have_prior:
        new_decs = [d for d in decisions if d.get("ticker") not in prior_pop]
    else:
        new_decs = []
    advanced_new = [d for d in new_decs if d.get("decision") in ADVANCE_DECISIONS]
    rejected_new = [d for d in new_decs if d.get("decision") == "REJECT"]
    convs = [
        d.get("conviction")
        for d in new_decs
        if isinstance(d.get("conviction"), (int, float))
    ]
    return {
        "date": date_str,
        "net_new_entrants": len(advanced_new) if have_prior else None,
        "new_candidates": len(new_decs) if have_prior else None,
        "new_rejected": len(rejected_new) if have_prior else None,
        "candidates_total": len(decisions),
        "new_conv_max": max(convs) if convs else None,
        "new_conv_mean": round(sum(convs) / len(convs), 1) if convs else None,
        "population_size": len(cur_pop),
        "advanced_new_tickers": [d.get("ticker") for d in advanced_new],
    }


def entrant_detail_df(
    cio_output: dict | None,
    prior_pop: set[str],
    sector_map: dict[str, str],
    sector_ratings: dict,
    *,
    have_prior: bool,
) -> pd.DataFrame:
    """This week's NEW candidates (not in prior population) with the context
    needed to read a zero-add week: sector, sector rating, conviction,
    advanced/rejected, and the CIO's reason (pure).
    """
    if not cio_output:
        return pd.DataFrame()
    rows = []
    for d in cio_output.get("ic_decisions", []) or []:
        ticker = d.get("ticker")
        if have_prior and ticker in prior_pop:
            continue  # incumbent re-advance — not a fresh candidate
        # Prefer the sector persisted on the decision (research L4533) — it
        # covers REJECTED fresh names that never entered the universe; fall
        # back to the universe sector_map for older cio.json without the field.
        sector = d.get("sector") or sector_map.get(ticker)
        rating = sector_ratings.get(sector, {}) if sector else {}
        rating = rating.get("rating") if isinstance(rating, dict) else rating
        decision = d.get("decision")
        if decision in ADVANCE_DECISIONS:
            decision_label = "✅ Advanced"
        elif decision == "REJECT":
            decision_label = "❌ Rejected"
        else:
            decision_label = decision or "—"
        rows.append(
            {
                "ticker": ticker,
                "sector": sector,
                "sector_rating": rating,
                "conviction": d.get("conviction"),
                "decision": decision_label,
                "reason": (d.get("rationale") or "")[:200],
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["conviction"] = pd.to_numeric(df["conviction"], errors="coerce")
        df = df.sort_values("conviction", ascending=False, na_position="last")
        # Sector context is not persisted on CIO decisions today (it lives on
        # the upstream team recommendation), so for rejected fresh names it is
        # usually unknown — the sector rationale is still in `reason`. Drop the
        # sector columns when entirely empty to avoid an all-blank column; they
        # light up automatically if the producer starts emitting sector.
        for col in ("sector", "sector_rating"):
            if col in df.columns and df[col].isna().all():
                df = df.drop(columns=[col])
    return df


@st.cache_data(ttl=900)
def load_cio_decisions(date_str: str) -> dict | None:
    """Load + unwrap the CIO decision archive for *date_str*. Returns the
    output dict (ic_decisions / advanced_tickers / entry_theses) or None.
    """
    raw = download_s3_json(_research_bucket(), _cio_key(date_str))
    return _cio_output(raw)


def _sector_map(signals_data: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not signals_data:
        return out
    for e in signals_data.get("universe", []) or []:
        ticker = e.get("ticker")
        if ticker:
            out[ticker] = e.get("sector")
    return out


def compute_entrant_flow(dates_desc: list[str], weeks: int = 12) -> pd.DataFrame:
    """Per-week new-entrant stats over the most recent *weeks* signal dates.

    ``dates_desc`` is the descending date list from
    ``get_available_signal_dates()``. One extra older date is consumed as the
    prior-population baseline for the earliest displayed week. Weeks with no
    CIO archive are skipped. Returns a DataFrame in ascending date order.
    """
    if not dates_desc:
        return pd.DataFrame()
    # Take weeks+1 ascending so the oldest displayed week has a prior baseline.
    window = list(reversed(dates_desc[: weeks + 1]))
    rows = []
    for i, d in enumerate(window):
        cio = load_cio_decisions(d)
        if not cio:
            continue
        cur_pop = population_tickers(load_signals(d))
        if i > 0:
            prior_signals = load_signals(window[i - 1])
            prior_pop = population_tickers(prior_signals)
            have_prior = prior_signals is not None
        else:
            prior_pop, have_prior = set(), False
        row = entrant_flow_row(d, cio, prior_pop, cur_pop, have_prior=have_prior)
        if row:
            rows.append(row)
    # Drop the baseline-only oldest row from display if it has no prior.
    df = pd.DataFrame(rows)
    return df


def get_entrant_detail_df(date_str: str, prior_date_str: str | None) -> pd.DataFrame:
    """This week's fresh-candidate detail table (advanced + rejected new names)."""
    cio = load_cio_decisions(date_str)
    signals = load_signals(date_str)
    sector_ratings = (signals or {}).get("sector_ratings", {}) or {}
    prior_signals = load_signals(prior_date_str) if prior_date_str else None
    prior_pop = population_tickers(prior_signals)
    have_prior = prior_signals is not None
    return entrant_detail_df(
        cio, prior_pop, _sector_map(signals), sector_ratings, have_prior=have_prior
    )
