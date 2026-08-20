"""Phase-2 morning-brief consumer — Streamlit I/O, LLM call, persistence.

This is the impure shell around the pure cadence core in
``live/morning_brief_cadence.py``. It:

  * reads the producer's daily news (``live/loaders/daily_news.py``),
  * captures the broad-market snapshot (``live/loaders/market_snapshot.py``),
  * runs the four-gate cadence to decide GENERATE / REUSE / CLOSED,
  * on GENERATE, builds the brief through the krepis model router (``low``
    group),
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

**alpha-engine-config-I6367 / alpha-engine-config-I7879 (2026-08-20): migrated
off direct provider linkage onto the krepis model router.** The intermediate
state (2026-07-19 → 2026-08-20) built a raw provider-pinned ``ModelSpec`` here
and resolved that provider's API key directly via
``nousergon_lib.secrets.get_secret`` — the exact direct-linkage shape Brian's
2026-08-03 ruling (I6367) forbids, and that sibling call sites
(``crucible-evaluator/director/agent.py``,
``crucible-research/producers/single_agent.py``) had already migrated off of.
This call site was the one holdout, tracked by I7879 and pre-cleared in
``.openrouter-allowlist.yaml`` pending this fix. Now calls
``krepis.router.resolve_group_spec("low", exec_context="ec2", wire="openai")``
— model, endpoint, credential and the ``reasoning`` param are all registry
decisions (model-router-policy §2 layer 5); this module states only its
capability tier (``low`` — cheap, high-volume, short output) and where it
runs (the dashboard box, ``ec2``). No model id, base URL, provider name or API
key is held here.
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

# Router group addressing (alpha-engine-config-I6367 / I7879). `low` is the
# correct tier: this call is cheap and high-volume (one short brief per
# throttle window) and its output is "a few short paragraphs" (_MAX_TOKENS
# below) — the shape `low`'s members (deepseek-v4-flash-low primary,
# gpt-oss-120b fallback) exist for. Both declare `reachable_from: [laptop,
# ec2]` in LLM_MODEL_REGISTRY.yaml, and the dashboard box IS `ec2`
# (model-router-policy R28/R29) — this module declares only where it runs,
# never which routes it can reach.
ROUTER_GROUP = "low"
ROUTER_EXEC_CONTEXT = "ec2"
_SESSION_KEY = "morning_brief_state"        # st.session_state cache key
# Output budget, NOT brief length. The brief is bounded by the prompt ("under
# 200 words"), which is what actually keeps it short. This number has to cover
# reasoning tokens too: the call site used to hand-set `reasoning={"exclude":
# True}`, and that is a registry decision now — `low`'s primary
# (`deepseek-v4-flash-low`) declares `reasoning: {effort: low}`, so unlike the
# pre-migration call this one DOES spend tokens thinking. 900 was sized for a
# non-reasoning model and is the budget shape config#1659 / config#2575
# describe: a reasoning-capable model spends the whole allowance on
# chain-of-thought and returns empty content. Raised to leave that headroom
# while the prompt keeps the visible brief the same length
# (alpha-engine-config-I7879).
_MAX_TOKENS = 2400

# Cost-attribution join key for this call site (krepis >= 0.23 requires it).
# Every LLM call from here emits a cost row stamped with this literal; the
# matching row in alpha-engine-config/private-docs/LLM_CALLSITE_REGISTRY.yaml
# should carry the same value as its `id` — the two are a lockstep pair
# (registry row pending, config#I5206 coverage is rolling out repo by repo).
CALLSITE_ID = "dashboard-morning-brief"


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


# ── The LLM call (krepis model router, `low` group) ─────────────────────────

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
    """Build the brief through the krepis model router (``low`` group).
    Returns the brief text, or None on any failure (router resolution,
    SDK/transport error) so the caller degrades gracefully.

    FAIL-SOFT, DELIBERATELY (model-router-policy §3.4 R20 governs the
    resolver itself, which already fails closed — no direct-provider
    fallback, no ambient key, no default endpoint on a resolution failure;
    this function's own None-return is a layer above that, and is the
    documented deviation from the fleet's fail-loud default):
      (a) failure mode swallowed — the router is unreachable, the `low`
          group has no reachable entry, or the resolved endpoint's
          completion call raises;
      (b) why the primary deliverable survives — this is a UI convenience
          card on a read-only dashboard, not a producer; `get_or_generate_brief`
          falls through to the last persisted brief (or a disabled-style
          notice) on any None here, so no pipeline artifact and no trade
          decision depends on this call succeeding;
      (c) recording surface — every failure path below logs at WARNING
          naming the router (or the transport) as the failed dependency, so
          the degradation is operator-visible in the box's own logs even
          though nothing downstream breaks.

    ``client_factory`` is the krepis.llm.LLMClient test seam (mirrors the
    Think Tank / single_agent.py pattern): a callable
    ``(spec, api_key) -> transport_client``. ``api_key`` is likewise a test
    seam only — in production the registry decides the credential
    (``spec.api_key_env``), resolved by krepis at call time; this module
    never reads or holds one.
    """
    from krepis.llm import LLMClient
    from krepis.router import resolve_group_spec, route_is_degraded

    try:
        spec, route = resolve_group_spec(
            ROUTER_GROUP,
            exec_context=ROUTER_EXEC_CONTEXT,
            wire="openai",
            max_tokens=_MAX_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 — fail-soft (a)/(b)/(c) above
        logger.warning(
            "[morning_brief] router resolution failed for group=%s "
            "exec_context=%s (%s: %s) — skipping generation",
            ROUTER_GROUP, ROUTER_EXEC_CONTEXT, type(e).__name__, e,
        )
        return None

    degraded = route_is_degraded(route)
    logger.info(
        "[morning_brief] route: group=%s model=%s provider=%s route=%s "
        "degraded=%s", ROUTER_GROUP, route.get("deployment_id"), spec.provider,
        route.get("route"), degraded,
    )
    if degraded:
        logger.warning(
            "[morning_brief] route DEGRADED: group=%s primary=%s served=%s "
            "route=%s", ROUTER_GROUP,
            route.get("primary_registry_id") or route.get("primary_model"),
            route.get("registry_id") or route.get("deployment_id"),
            route.get("route"),
        )

    try:
        client = LLMClient(
            spec,
            api_key=api_key,
            client_factory=client_factory,
            callsite_id=CALLSITE_ID,
        )
        result = client.complete(
            system=_SYSTEM_PROMPT,
            user_content=_build_prompt(snapshot, holdings_news),
            max_tokens=_MAX_TOKENS,
        )
        text = (result.text or "").strip()
        return text or None
    except Exception as e:  # noqa: BLE001 — fail-soft (a)/(b)/(c) above
        logger.warning(
            "[morning_brief] router-resolved call failed (%s: %s)",
            type(e).__name__, e,
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
