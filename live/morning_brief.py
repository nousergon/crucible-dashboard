"""Phase-2 morning-brief consumer — Streamlit I/O, Haiku call, persistence.

This is the impure shell around the pure cadence core in
``live/morning_brief_cadence.py``. It:

  * reads the producer's daily news (``live/loaders/daily_news.py``),
  * captures the broad-market snapshot (``live/loaders/market_snapshot.py``),
  * runs the four-gate cadence to decide GENERATE / REUSE / CLOSED,
  * on GENERATE, builds the brief with Haiku (``claude-haiku-4-5``),
  * persists ``{brief text + snapshot + generated_at + call_count}`` keyed by
    ``trading_day`` in ``st.session_state`` so the next rerun can evaluate the
    throttle + materiality gates,
  * honors the ``ai_advisor.enabled`` regulatory kill switch (config) — when
    off, NO Haiku call is ever made and the card shows a disabled notice.

The brief LEADS WITH THE MACRO READ ("why is the market down today" — from the
live SPY/QQQ/VIX snapshot + any macro headlines) THEN per-ticker holdings news.

Anthropic SDK usage follows the project claude-api guidance: the official
``anthropic`` Python SDK, model id literally ``claude-haiku-4-5``. Haiku 4.5
does NOT support the ``thinking`` / ``output_config.effort`` parameters (they
400 on Haiku), so we pass neither.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import streamlit as st
from zoneinfo import ZoneInfo

from loaders.daily_news import load_daily_news_rows, top_holdings_news
from loaders.market_snapshot import capture_market_snapshot
from loaders.s3_loader import load_config
from morning_brief_cadence import (
    BriefState,
    CadenceConfig,
    Decision,
    MarketSnapshot,
    decide,
)

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
HAIKU_MODEL = "claude-haiku-4-5"
_SESSION_KEY = "morning_brief_state"        # st.session_state cache key
_MAX_TOKENS = 900                           # brief is a few short paragraphs


# ── Config: kill switch + cadence overrides ────────────────────────────────

def _ai_advisor_enabled() -> bool:
    """Regulatory kill switch. Default ON; set ``ai_advisor.enabled: false`` in
    config.yaml to hard-disable ALL Haiku calls (the brief card then shows a
    disabled notice and never reaches the SDK)."""
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001 — missing config → treat as enabled default
        return True
    section = (cfg or {}).get("ai_advisor", {}) or {}
    return bool(section.get("enabled", True))


def _cadence_config() -> CadenceConfig:
    """Build a CadenceConfig from config.yaml ``morning_brief`` overrides
    (all optional; defaults from the dataclass)."""
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return CadenceConfig()
    mb = (cfg or {}).get("morning_brief", {}) or {}
    base = CadenceConfig()
    from dataclasses import replace

    overrides = {}
    if "pre_open_lead_min" in mb:
        overrides["pre_open_lead_min"] = int(mb["pre_open_lead_min"])
    if "throttle_minutes" in mb:
        overrides["throttle_minutes"] = int(mb["throttle_minutes"])
    if "material_index_pp" in mb:
        overrides["material_index_pp"] = float(mb["material_index_pp"])
    if "material_vix_pts" in mb:
        overrides["material_vix_pts"] = float(mb["material_vix_pts"])
    if "daily_cap" in mb:
        overrides["daily_cap"] = int(mb["daily_cap"])
    return replace(base, **overrides) if overrides else base


# ── Persistence (session_state, keyed by trading_day) ──────────────────────

def _load_state() -> Optional[BriefState]:
    raw = st.session_state.get(_SESSION_KEY)
    if not raw:
        return None
    try:
        return BriefState.from_dict(raw)
    except Exception:  # noqa: BLE001 — corrupt cache → regenerate
        return None


def _save_state(state: BriefState) -> None:
    st.session_state[_SESSION_KEY] = state.to_dict()


# ── Anthropic key resolution (st.secrets; NO os.environ — see
#    tests/test_no_secret_environ_reads.py) ──────────────────────────────────

def _anthropic_api_key() -> Optional[str]:
    """Resolve the Anthropic key from ``st.secrets`` only.

    The repo bans ``os.environ.get`` secret reads (tests/test_no_secret_environ_
    reads.py); on EC2 the key is seeded into Streamlit secrets (or
    ``nousergon_lib.secrets`` at deploy). Returns None when unavailable — the
    card then shows a friendly "AI brief unavailable" notice instead of erroring.
    """
    try:
        return st.secrets["anthropic"]["ANTHROPIC_API_KEY"]
    except (KeyError, FileNotFoundError, AttributeError):
        return None


# ── The Haiku call ─────────────────────────────────────────────────────────

def _build_prompt(snapshot: MarketSnapshot, holdings_news: list[dict]) -> str:
    """Assemble the user prompt: macro snapshot first, then holdings news."""
    def _fmt_pp(v):
        return f"{v:+.2f}%" if v is not None else "n/a"

    macro = (
        "Broad-market snapshot (intraday, today):\n"
        f"  S&P 500 (SPY) day return: {_fmt_pp(snapshot.spy_day_return_pp)}\n"
        f"  Nasdaq-100 (QQQ) day return: {_fmt_pp(snapshot.qqq_day_return_pp)}\n"
        f"  VIX level: {snapshot.vix if snapshot.vix is not None else 'n/a'}\n"
    )
    if holdings_news:
        lines = []
        for r in holdings_news:
            tkr = r.get("ticker", "?")
            sent = r.get("lm_sentiment_trusted_mean")
            if sent is None:
                sent = r.get("lm_sentiment_mean")
            cats = r.get("event_categories") or ""
            desc = r.get("top_event_descriptions") or ""
            n = r.get("n_articles") or 0
            lines.append(
                f"  {tkr}: {n} articles, sentiment {sent}, "
                f"events [{cats}] {desc}".rstrip()
            )
        holdings = "Per-holding news today:\n" + "\n".join(lines)
    else:
        holdings = "Per-holding news today: (no holdings news available)"
    return macro + "\n" + holdings


_SYSTEM_PROMPT = (
    "You write a concise pre-market/intraday brief for a retail-facing "
    "algorithmic-trading dashboard. LEAD WITH THE MACRO READ: in 2-3 sentences, "
    "explain what the broad market is doing today and why (use the SPY/QQQ day "
    "returns and the VIX level provided — e.g. risk-off vs risk-on, volatility "
    "regime). THEN, in a short bulleted list, summarize the most notable "
    "per-holding news, one bullet per ticker, plainly. Be factual and neutral. "
    "Do not give investment advice, price targets, or buy/sell recommendations. "
    "If a data point is 'n/a', do not speculate about it. Keep the whole brief "
    "under 200 words."
)


def generate_morning_brief(
    snapshot: MarketSnapshot,
    holdings_news: list[dict],
    *,
    api_key: Optional[str] = None,
) -> Optional[str]:
    """Build the brief with Haiku (``claude-haiku-4-5``). Returns the brief text,
    or None on any failure (no key, SDK error) so the caller degrades gracefully.

    Haiku 4.5 does not support ``thinking`` / ``effort`` — neither is passed.
    """
    key = api_key or _anthropic_api_key()
    if not key:
        logger.warning("[morning_brief] no Anthropic API key — skipping generation")
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": _build_prompt(snapshot, holdings_news)}
            ],
        )
        # Parse content blocks by .type (per claude-api guidance).
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        text = "\n".join(p for p in parts if p).strip()
        return text or None
    except Exception as e:  # noqa: BLE001 — fail-soft; card shows last brief / notice
        logger.warning("[morning_brief] Haiku call failed (%s: %s)", type(e).__name__, e)
        return None


# ── The rerun-driven entry point ───────────────────────────────────────────

def get_or_generate_brief(
    *,
    held_tickers: set[str] | None = None,
    now: Optional[datetime] = None,
) -> dict:
    """Resolve the brief for THIS rerun, running the four-gate cadence.

    Returns a render-ready dict:
        {
          "enabled": bool,            # kill switch
          "decision": Decision,       # GENERATE/REUSE_CACHED/CLOSED
          "reason": str,
          "is_window_open": bool,
          "brief_text": str | None,
          "as_of_et": str | None,     # "9:42 AM ET" of the brief's generated_at
          "stale_day": bool,          # brief is from a prior trading day
        }

    Demand (gate 2) is satisfied by being called from a Streamlit rerun. No
    cron / background warmer ever calls this.
    """
    from trading_calendar import is_trading_day

    if not _ai_advisor_enabled():
        return {
            "enabled": False,
            "decision": Decision.CLOSED,
            "reason": "ai_advisor_kill_switch_off",
            "is_window_open": False,
            "brief_text": None,
            "as_of_et": None,
            "stale_day": False,
        }

    now = now or datetime.now(ET)
    today_et = now.astimezone(ET).date()
    config = _cadence_config()
    last_state = _load_state()

    # Capture a snapshot for this rerun only when the window is plausibly open;
    # outside the window we never call, so a snapshot isn't needed (and we avoid
    # the yfinance round-trip). The cadence re-checks the window authoritatively.
    current_snapshot = MarketSnapshot.from_dict(capture_market_snapshot())

    result = decide(
        now=now,
        current_snapshot=current_snapshot,
        last_state=last_state,
        is_trading_day=is_trading_day,
        config=config,
    )

    if result.decision is Decision.GENERATE:
        rows = load_daily_news_rows()
        holdings_news = top_holdings_news(rows, held_tickers)
        text = generate_morning_brief(current_snapshot, holdings_news)
        if text:
            prior_count = last_state.call_count if (
                last_state is not None and last_state.trading_day == today_et
            ) else 0
            new_state = BriefState(
                trading_day=today_et,
                brief_text=text,
                snapshot=current_snapshot,
                generated_at=now,
                call_count=prior_count + 1,
            )
            _save_state(new_state)
            return _render_dict(new_state, result, enabled=True, stale_day=False)
        # Generation failed — fall through to whatever prior brief exists.

    # REUSE_CACHED / CLOSED / failed-GENERATE → render the persisted brief.
    state = _load_state()
    stale_day = state is not None and state.trading_day != today_et
    return _render_dict(state, result, enabled=True, stale_day=stale_day)


def _render_dict(
    state: Optional[BriefState], result, *, enabled: bool, stale_day: bool
) -> dict:
    as_of = None
    brief_text = None
    if state is not None:
        brief_text = state.brief_text
        as_of = state.generated_at.astimezone(ET).strftime("%-I:%M %p ET")
    return {
        "enabled": enabled,
        "decision": result.decision,
        "reason": result.reason,
        "is_window_open": result.is_window_open,
        "brief_text": brief_text,
        "as_of_et": as_of,
        "stale_day": stale_day,
    }
