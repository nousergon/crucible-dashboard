"""Phase-2 morning-brief consumer — Streamlit I/O, LLM call, persistence.

This is the impure shell around the pure cadence core in
``live/morning_brief_cadence.py``. It:

  * reads the producer's daily news (``live/loaders/daily_news.py``),
  * captures the broad-market snapshot (``live/loaders/market_snapshot.py``),
  * runs the four-gate cadence to decide GENERATE / REUSE / CLOSED,
  * on GENERATE, builds the brief via OpenRouter (DeepSeek V4 Flash),
  * persists ``{brief text + snapshot + generated_at + call_count}`` keyed by
    ``trading_day`` in ``st.session_state`` so the next rerun can evaluate the
    throttle + materiality gates,
  * honors the ``ai_advisor.enabled`` regulatory kill switch (config) — when
    off, NO LLM call is ever made and the card shows a disabled notice.

The brief LEADS WITH THE MACRO READ ("why is the market down today" — from the
live SPY/QQQ/VIX snapshot + any macro headlines) THEN per-ticker holdings news.

**alpha-engine-config-I2997 (2026-07-19): migrated off direct Anthropic API.**
Was a raw ``anthropic.Anthropic()`` client resolving its key via
``st.secrets["anthropic"]["ANTHROPIC_API_KEY"]`` — live-verified during the
migration that NO ``secrets.toml`` exists anywhere on the dashboard EC2 box
(``i-09b539c844515d549``), so this call site was silently dead in production
(``_anthropic_api_key()`` always returned ``None``, and the fail-soft path
swallowed it into an "unavailable" notice with no operator-visible signal).
Now uses the fleet-SOTA ``krepis.llm.LLMClient`` OpenRouter transport (the
Think-Tank-ratified multi-provider adapter — see
``crucible-research/thinktank/client.py`` / ``krepis.llm``), with the API key
resolved via ``nousergon_lib.secrets.get_secret("OPENROUTER_API_KEY")`` —
SSM-first (``/alpha-engine/OPENROUTER_API_KEY``, readable by the dashboard
box's existing ``alpha-engine-ssm-read`` instance-role policy, no new IAM),
env fallback. This is also the module's own test file's documented preferred
pattern (see ``tests/test_no_secret_environ_reads.py``) over the previously
used, and here non-functional, ``st.secrets`` path.
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
# OpenRouter/DeepSeek V4 Flash (alpha-engine-config-I2997, 2026-07-19).
# ID verified two ways: (1) live against the OpenRouter models API
# (`GET https://openrouter.ai/api/v1/models` lists `deepseek/deepseek-v4-flash`
# — "DeepSeek: DeepSeek V4 Flash"); (2) cross-checked against two independent
# live fleet configs already running this exact ID: morning-signal's SSM
# `/morning-signal/config-yaml` `fallback_llm`, and crucible-research's
# `evals/judge_models.py::OPENROUTER_SHADOW` (live-verified 2026-07-18). A
# hand-typed/wrong OpenRouter model ID silently killed morning-signal on
# 2026-07-15 (`deepseek/deepseek-chat-v4` is not a real model) — never
# hand-write one.
OPENROUTER_MODEL = "deepseek/deepseek-v4-flash"
_SESSION_KEY = "morning_brief_state"        # st.session_state cache key
_MAX_TOKENS = 900                           # brief is a few short paragraphs


# ── Config: kill switch + cadence overrides ────────────────────────────────

def _ai_advisor_enabled() -> bool:
    """Regulatory kill switch. Default ON; set ``ai_advisor.enabled: false`` in
    config.yaml to hard-disable ALL LLM calls (the brief card then shows a
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


# ── OpenRouter key resolution (nousergon_lib.secrets: SSM-first, env
#    fallback — no os.environ.get/getenv direct read; see
#    tests/test_no_secret_environ_reads.py) ──────────────────────────────────

def _openrouter_api_key() -> Optional[str]:
    """Resolve the OpenRouter key via ``nousergon_lib.secrets.get_secret``.

    SSM-first (``/alpha-engine/OPENROUTER_API_KEY``, readable by the
    dashboard box's instance role — the same ``alpha-engine-ssm-read``
    inline policy every other SSM-backed fleet secret on this box uses, no
    new IAM needed), env fallback. Predates this call site: verified live
    (alpha-engine-config-I2997 migration, 2026-07-19) that NO
    ``.streamlit/secrets.toml`` exists anywhere on the box — the prior
    ``st.secrets``-based Anthropic key resolution this replaced was always
    returning ``None`` in production. Returns None when unavailable — the
    card then shows a friendly "AI brief unavailable" notice instead of
    erroring (fail-soft; this is a UI convenience, not a producer).
    """
    from nousergon_lib.secrets import get_secret

    return get_secret("OPENROUTER_API_KEY", required=False, default=None) or None


# ── The LLM call (OpenRouter / DeepSeek V4 Flash) ──────────────────────────

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
    client_factory=None,
) -> Optional[str]:
    """Build the brief via OpenRouter (DeepSeek V4 Flash, see ``OPENROUTER_MODEL``).
    Returns the brief text, or None on any failure (no key, SDK/transport error)
    so the caller degrades gracefully — this is a UI convenience, not a
    producer; the fail-soft contract predates the alpha-engine-config-I2997
    transport migration and is unchanged by it.

    ``client_factory`` is the krepis.llm.LLMClient test seam (mirrors the
    Think Tank pattern): a callable ``(spec, api_key) -> transport_client``.
    Production leaves it unset — ``LLMClient`` lazily builds the real
    ``openai.OpenAI`` client pointed at OpenRouter's ``base_url``.

    ``reasoning={"exclude": True}`` mirrors the two other live fleet DeepSeek
    V4 consumers (morning-signal's ``fallback_llm``, crucible-research's
    ``evals/judge_models.py::OPENROUTER_SHADOW``) — without it, a reasoning-
    capable OpenRouter model can burn its entire output budget on chain-of-
    thought and return empty content even at a generous ``max_tokens``
    (live-reproduced fleet-wide, config#1659 / config#2575).
    """
    from krepis.llm import LLMClient
    from krepis.llm_config import ModelSpec

    key = api_key or _openrouter_api_key()
    if not key:
        logger.warning("[morning_brief] no OpenRouter API key — skipping generation")
        return None
    try:
        spec = ModelSpec(
            provider="openrouter",
            model=OPENROUTER_MODEL,
            max_tokens=_MAX_TOKENS,
            reasoning={"exclude": True},
        )
        client = LLMClient(spec, api_key=key, client_factory=client_factory)
        result = client.complete(
            system=_SYSTEM_PROMPT,
            user_content=_build_prompt(snapshot, holdings_news),
            max_tokens=_MAX_TOKENS,
        )
        text = (result.text or "").strip()
        return text or None
    except Exception as e:  # noqa: BLE001 — fail-soft; card shows last brief / notice
        logger.warning(
            "[morning_brief] OpenRouter call failed (%s: %s)", type(e).__name__, e
        )
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
