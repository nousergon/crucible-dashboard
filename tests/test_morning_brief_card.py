"""Hermetic presentation tests; the live brief machinery is never imported."""

import sys
from enum import Enum, auto
from types import ModuleType
from typing import TypedDict
from unittest.mock import MagicMock, call

import pytest

from components import morning_brief_card


class Decision(Enum):
    GENERATE = auto()
    REUSE_CACHED = auto()
    CLOSED = auto()


class BriefInfo(TypedDict):
    enabled: bool
    brief_text: str | None
    as_of_et: str | None
    decision: Decision
    stale_day: bool


class FakeBrief(ModuleType):
    def __init__(self, info: BriefInfo) -> None:
        super().__init__("morning_brief")
        self.info: BriefInfo = info
        self.calls: list[set[str] | None] = []

    def get_or_generate_brief(self, *, held_tickers: set[str] | None) -> BriefInfo:
        self.calls.append(held_tickers)
        return self.info


class FakeCadence(ModuleType):
    Decision: type[Decision] = Decision


def install_brief(
    monkeypatch: pytest.MonkeyPatch, info: BriefInfo
) -> FakeBrief:
    brief = FakeBrief(info)
    monkeypatch.setitem(sys.modules, "morning_brief", brief)
    monkeypatch.setitem(sys.modules, "morning_brief_cadence", FakeCadence("morning_brief_cadence"))
    return brief


def test_disabled_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    st = MagicMock()
    monkeypatch.setattr(morning_brief_card, "st", st)
    brief = install_brief(monkeypatch, {
        "enabled": False, "brief_text": "Hidden brief", "as_of_et": "09:35",
        "decision": Decision.GENERATE, "stale_day": False,
    })

    morning_brief_card.render_morning_brief_card()

    assert st.mock_calls == []
    assert brief.calls == [None]


@pytest.mark.parametrize(
    ("decision", "text", "as_of", "stale", "caption"),
    [
        (Decision.GENERATE, "Today's brief", "09:35", False, "As of 09:35."),
        (Decision.REUSE_CACHED, "Today's brief", None, False, ""),
        (Decision.REUSE_CACHED, "Yesterday's brief", "09:35", True,
         "Last brief as of 09:35 (prior session)."),
        (Decision.CLOSED, "Last brief", "09:35", False,
         "🔴 Market closed — last brief as of 09:35."),
        (Decision.CLOSED, "Last brief", None, False, "🔴 Market closed."),
        (Decision.CLOSED, None, None, False,
         "🔴 Market closed — today's brief will appear once the market opens."),
        (Decision.GENERATE, None, None, False, "Preparing today's brief…"),
    ],
)
def test_enabled_brief_rendering(
    monkeypatch: pytest.MonkeyPatch,
    decision: Decision,
    text: str | None,
    as_of: str | None,
    stale: bool,
    caption: str,
) -> None:
    st = MagicMock()
    monkeypatch.setattr(morning_brief_card, "st", st)
    brief = install_brief(monkeypatch, {
        "enabled": True, "brief_text": text, "as_of_et": as_of,
        "decision": decision, "stale_day": stale,
    })
    held_tickers = {"AAPL"}

    morning_brief_card.render_morning_brief_card(held_tickers=held_tickers)

    expected_calls = [call.markdown("### 📋 Morning Brief"), call.caption(caption)]
    if text:
        expected_calls.append(call.markdown(text))
    expected_calls.extend([
        call.caption("Auto-generated market context for orientation only — not investment advice."),
        call.divider(),
    ])
    assert st.mock_calls == expected_calls
    st.divider.assert_called_once_with()
    assert brief.calls == [held_tickers]
    assert brief.calls[0] is held_tickers
