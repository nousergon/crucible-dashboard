"""Pure, side-effect-free cadence gating for the morning-brief Haiku call.

This module is the decision core of the Phase-2 morning brief (config#664 /
L4574). It answers ONE question — *should this dashboard rerun fire a Haiku
call to (re)generate the brief?* — given a synthetic clock, the current
broad-market snapshot, and the persisted state of the last brief generation.

It is deliberately free of Streamlit, the Anthropic SDK, boto3, yfinance, and
even ``datetime.now()``: every input is passed in, so the four gates can be
unit-tested without live data, network, or API keys. The Streamlit I/O,
Anthropic call, and S3 reads live in ``live/morning_brief.py``; the renderer
in ``live/components/morning_brief_card.py``.

The four gates (ALL must hold to fire a call):

  1. Callable window  — LLM may be called ONLY during/just-before market hours.
                        Default 09:00–16:00 ET with a tunable pre-open lead
                        (~30 min), NYSE-calendar-aware so holidays are excluded.
  2. Demand           — there is a viewer right now (a Streamlit rerun). This
                        module is only ever reached from a rerun, so "demand"
                        is satisfied by being called at all; it is represented
                        here as the absence of any cron/background path. No
                        separate predicate is needed — the caller IS the demand.
  3. Hourly throttle  — <= 1 Haiku call per rolling 60 min. A refresh within
                        60 min of the last brief reuses cache.
  4. Materiality      — beyond the throttle, regenerate only if the broad
                        market moved materially since the snapshot captured at
                        the LAST BRIEF GENERATION (not last view).

Plus two backstops:

  * First view inside the callable window each trading day always generates
    once (subject to throttle) — there is no prior intraday snapshot to
    compare against.
  * Hard daily cap of <= 8 Haiku calls per trading day.

A view BEFORE the window opens shows the prior brief and does NOT call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Callable, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ── Tunable defaults (config-overridable; see CadenceConfig) ───────────────
DEFAULT_WINDOW_OPEN = time(9, 0)            # 09:00 ET regular open band start
DEFAULT_WINDOW_CLOSE = time(16, 0)          # 16:00 ET close
DEFAULT_PRE_OPEN_LEAD_MIN = 30              # may call ~30 min before 09:00
DEFAULT_THROTTLE_MINUTES = 60               # <= 1 call / rolling 60 min
DEFAULT_MATERIAL_INDEX_PP = 0.75            # |Δ| pp in larger of {SPY, QQQ}
DEFAULT_MATERIAL_VIX_PTS = 2.0              # VIX jump in absolute points
DEFAULT_DAILY_CAP = 8                       # hard backstop, calls/trading day


@dataclass(frozen=True)
class CadenceConfig:
    """Tunable cadence knobs. All have sane defaults; override via config."""

    window_open: time = DEFAULT_WINDOW_OPEN
    window_close: time = DEFAULT_WINDOW_CLOSE
    pre_open_lead_min: int = DEFAULT_PRE_OPEN_LEAD_MIN
    throttle_minutes: int = DEFAULT_THROTTLE_MINUTES
    material_index_pp: float = DEFAULT_MATERIAL_INDEX_PP
    material_vix_pts: float = DEFAULT_MATERIAL_VIX_PTS
    daily_cap: int = DEFAULT_DAILY_CAP


@dataclass(frozen=True)
class MarketSnapshot:
    """The broad-market state a brief is anchored to.

    ``spy_day_return_pp`` / ``qqq_day_return_pp`` are intraday day-returns in
    PERCENTAGE POINTS (e.g. -1.25 for -1.25%). ``vix`` is the VIX level in
    points. Any leg may be ``None`` when its source is unavailable; the
    materiality test degrades gracefully (a missing leg cannot trip).
    """

    ts: datetime                            # tz-aware; when the snapshot was taken
    spy_day_return_pp: Optional[float] = None
    qqq_day_return_pp: Optional[float] = None
    vix: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(),
            "spy_day_return_pp": self.spy_day_return_pp,
            "qqq_day_return_pp": self.qqq_day_return_pp,
            "vix": self.vix,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MarketSnapshot":
        ts = d["ts"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return cls(
            ts=ts,
            spy_day_return_pp=d.get("spy_day_return_pp"),
            qqq_day_return_pp=d.get("qqq_day_return_pp"),
            vix=d.get("vix"),
        )


@dataclass(frozen=True)
class BriefState:
    """Everything persisted per generation, keyed by ``trading_day``.

    Stored alongside the brief text so the NEXT view can evaluate the throttle
    and materiality gates. ``call_count`` is the number of Haiku calls made for
    ``trading_day`` so far (drives the daily cap).
    """

    trading_day: date
    brief_text: str
    snapshot: MarketSnapshot                # market state the brief was based on
    generated_at: datetime                  # tz-aware; when the brief was made
    call_count: int = 0

    def to_dict(self) -> dict:
        return {
            "trading_day": self.trading_day.isoformat(),
            "brief_text": self.brief_text,
            "snapshot": self.snapshot.to_dict(),
            "generated_at": self.generated_at.isoformat(),
            "call_count": self.call_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BriefState":
        td = d["trading_day"]
        if isinstance(td, str):
            td = date.fromisoformat(td)
        ga = d["generated_at"]
        if isinstance(ga, str):
            ga = datetime.fromisoformat(ga)
        return cls(
            trading_day=td,
            brief_text=d["brief_text"],
            snapshot=MarketSnapshot.from_dict(d["snapshot"]),
            generated_at=ga,
            call_count=int(d.get("call_count", 0)),
        )


class Decision(Enum):
    """What the cadence wants the caller to do on this rerun."""

    GENERATE = "generate"        # fire a Haiku call now
    REUSE_CACHED = "reuse"       # show the persisted brief; no call
    CLOSED = "closed"            # outside the callable window; show last brief
                                 # stamped "as of HH:MM ET" + closed indicator


@dataclass(frozen=True)
class CadenceResult:
    decision: Decision
    reason: str = ""                         # human-readable gate that decided it
    # Convenience flags for the renderer:
    is_window_open: bool = field(default=False)


# ── Window gate (gate 1) ───────────────────────────────────────────────────

def is_callable_window(
    now: datetime,
    *,
    config: CadenceConfig = CadenceConfig(),
    is_trading_day: Callable[[date], bool],
) -> bool:
    """True iff the LLM may be called at ``now``.

    The window is ``[window_open - pre_open_lead, window_close]`` in ET, and
    only on NYSE trading days (``is_trading_day`` is injected — pass
    ``trading_calendar.is_trading_day``). ``now`` is converted to ET, so the
    caller may pass any tz-aware datetime.
    """
    now_et = now.astimezone(ET)
    if not is_trading_day(now_et.date()):
        return False
    open_dt = datetime.combine(now_et.date(), config.window_open, tzinfo=ET)
    effective_open = open_dt - timedelta(minutes=config.pre_open_lead_min)
    close_dt = datetime.combine(now_et.date(), config.window_close, tzinfo=ET)
    return effective_open <= now_et <= close_dt


# ── Throttle gate (gate 3) ─────────────────────────────────────────────────

def throttle_elapsed(
    now: datetime, last_generated_at: datetime, *, config: CadenceConfig = CadenceConfig()
) -> bool:
    """True iff at least ``throttle_minutes`` have elapsed since the last brief."""
    return now - last_generated_at >= timedelta(minutes=config.throttle_minutes)


# ── Materiality gate (gate 4) ──────────────────────────────────────────────

def is_material_move(
    current: MarketSnapshot,
    baseline: MarketSnapshot,
    *,
    config: CadenceConfig = CadenceConfig(),
) -> bool:
    """True iff the broad market moved materially since ``baseline``.

    Material =
        |Δ| >= ``material_index_pp`` in the LARGER-MAGNITUDE of {SPY, QQQ}
        intraday day-return since the baseline snapshot, OR
        a VIX JUMP (current - baseline) >= ``material_vix_pts``.

    Both legs are evaluated independently; a missing leg on either side simply
    cannot trip (None-safe). The index leg compares the CHANGE in each index's
    day-return between the two snapshots (not the absolute day-return), so a
    market that gapped down at the baseline and then went sideways is correctly
    treated as immaterial.
    """
    # Index leg — larger-magnitude move of SPY/QQQ since baseline.
    index_deltas = []
    for cur_v, base_v in (
        (current.spy_day_return_pp, baseline.spy_day_return_pp),
        (current.qqq_day_return_pp, baseline.qqq_day_return_pp),
    ):
        if cur_v is not None and base_v is not None:
            index_deltas.append(abs(cur_v - base_v))
    if index_deltas and max(index_deltas) >= config.material_index_pp:
        return True

    # VIX leg — jump in absolute points since baseline.
    if current.vix is not None and baseline.vix is not None:
        if (current.vix - baseline.vix) >= config.material_vix_pts:
            return True

    return False


# ── The orchestrating decision (all four gates) ────────────────────────────

def decide(
    *,
    now: datetime,
    current_snapshot: Optional[MarketSnapshot],
    last_state: Optional[BriefState],
    is_trading_day: Callable[[date], bool],
    config: CadenceConfig = CadenceConfig(),
) -> CadenceResult:
    """Decide whether this rerun should generate, reuse, or show-closed.

    Args:
        now: tz-aware "now" (synthetic clock in tests; ``datetime.now(ET)`` live).
        current_snapshot: the market state captured on THIS rerun, or None if
            the live-quote set is unavailable (then a fresh generation can't be
            anchored, so we fall back to reuse/closed).
        last_state: the persisted brief for the current trading day (None if no
            brief has been generated for today yet). A ``last_state`` whose
            ``trading_day`` differs from today's ET date is treated as stale
            (i.e. as if there is no brief for today) for the first-view and cap
            logic, while still being renderable as the prior brief.
        is_trading_day: NYSE-calendar predicate (inject ``is_trading_day``).
        config: tunable knobs.

    Demand (gate 2) is satisfied implicitly: this function is only invoked from
    a Streamlit rerun (a viewer present). There is no cron/background caller.
    """
    window_open = is_callable_window(now, config=config, is_trading_day=is_trading_day)

    # Gate 1 — outside the callable window: never call. Show last brief +
    # closed indicator (the renderer stamps "as of HH:MM ET").
    if not window_open:
        return CadenceResult(Decision.CLOSED, reason="outside_callable_window")

    today_et = now.astimezone(ET).date()

    # A persisted brief from a PRIOR trading day must not satisfy "first view
    # of today" or count against today's cap.
    todays_state = (
        last_state
        if last_state is not None and last_state.trading_day == today_et
        else None
    )

    # Daily cost backstop — hard cap regardless of materiality.
    if todays_state is not None and todays_state.call_count >= config.daily_cap:
        return CadenceResult(
            Decision.REUSE_CACHED, reason="daily_cap_reached", is_window_open=True
        )

    # First view inside the window today: always generate once (subject to
    # throttle, handled below via the absent todays_state path). With no prior
    # intraday snapshot there is nothing to compare for materiality.
    if todays_state is None:
        if current_snapshot is None:
            # Can't anchor a fresh brief without a market snapshot; degrade to
            # showing whatever prior brief exists (renderer handles None).
            return CadenceResult(
                Decision.REUSE_CACHED,
                reason="first_view_no_snapshot",
                is_window_open=True,
            )
        return CadenceResult(
            Decision.GENERATE, reason="first_view_of_trading_day", is_window_open=True
        )

    # Gate 3 — hourly throttle. A refresh within the throttle window reuses
    # cache no matter how material the move.
    if not throttle_elapsed(now, todays_state.generated_at, config=config):
        return CadenceResult(
            Decision.REUSE_CACHED, reason="within_throttle_window", is_window_open=True
        )

    # Gate 4 — materiality, measured against the LAST BRIEF's snapshot.
    if current_snapshot is None:
        return CadenceResult(
            Decision.REUSE_CACHED, reason="no_current_snapshot", is_window_open=True
        )
    if is_material_move(current_snapshot, todays_state.snapshot, config=config):
        return CadenceResult(
            Decision.GENERATE, reason="material_move", is_window_open=True
        )

    return CadenceResult(
        Decision.REUSE_CACHED, reason="immaterial_move", is_window_open=True
    )
