"""Rendering contracts for the engineering-phase helpers."""

from typing import cast
from unittest.mock import MagicMock

import pytest

from components import phase_indicator


def test_current_phase_indicator(monkeypatch: pytest.MonkeyPatch) -> None:
    st = MagicMock()
    monkeypatch.setattr(phase_indicator, "st", st)

    phase_indicator.render_phase_indicator("Reliability + Measurability")

    st.markdown.assert_called_once()
    html = cast(str, st.markdown.call_args.args[0])
    assert st.markdown.call_args.kwargs == {"unsafe_allow_html": True}
    for index, phase in enumerate(phase_indicator.PHASES, start=1):
        assert f"Phase {index}" in html
        assert f'KPI: {phase["kpi"]}' in html
    assert html.count("&#x2713;") == 1
    assert html.count("&#x25B6;") == 1
    assert html.count("&#x2022;") == 2
    assert "#5fa8f0" in html


@pytest.mark.parametrize(
    ("phase", "complete", "current", "upcoming"),
    [("Performance (live)", 3, 1, 0), ("Unknown phase", 0, 0, 4)],
)
def test_phase_indicator_statuses(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    complete: int,
    current: int,
    upcoming: int,
) -> None:
    st = MagicMock()
    monkeypatch.setattr(phase_indicator, "st", st)

    phase_indicator.render_phase_indicator(phase)

    st.markdown.assert_called_once()
    html = cast(str, st.markdown.call_args.args[0])
    assert st.markdown.call_args.kwargs == {"unsafe_allow_html": True}
    assert html.count("&#x2713;") == complete
    assert html.count("&#x25B6;") == current
    assert html.count("&#x2022;") == upcoming


def test_unknown_phase_caption(monkeypatch: pytest.MonkeyPatch) -> None:
    st = MagicMock()
    monkeypatch.setattr(phase_indicator, "st", st)

    phase_indicator.render_phase_caption("Unknown phase")

    st.markdown.assert_not_called()


def test_paper_phase_caption_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    st = MagicMock()
    monkeypatch.setattr(phase_indicator, "st", st)

    phase_indicator.render_phase_caption("Performance (paper)")

    st.markdown.assert_called_once()
    html = cast(str, st.markdown.call_args.args[0])
    assert st.markdown.call_args.kwargs == {"unsafe_allow_html": True}
    caption = "Tuning signals, risk, and execution for sustained alpha vs SPY on paper."
    empty_slot = '<div style="flex:1; min-width:140px;"></div>'
    assert html.count(caption) == 1
    before, after = html.split(caption)
    assert before.count(empty_slot) == 2
    assert after.count(empty_slot) == 1
    assert html.count('<div style="flex:1;') == 4


def test_phase_descriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    st = MagicMock()
    monkeypatch.setattr(phase_indicator, "st", st)

    phase_indicator.render_phase_descriptions("Reliability + Measurability")

    st.markdown.assert_called_once()
    html = cast(str, st.markdown.call_args.args[0])
    assert st.markdown.call_args.kwargs == {"unsafe_allow_html": True}
    for index, phase in enumerate(phase_indicator.PHASES, start=1):
        assert f'Phase {index} &middot; {phase["name"]}' in html
    assert html.count("<li") == sum(
        len(bullets) for bullets in phase_indicator._PHASE_DESCRIPTIONS.values()
    )
    # Phase 1 complete, phase 2 current (brand blue), phases 3-4 upcoming.
    assert html.count("border: 1px solid #2d5a2d;") == 1
    assert html.count("border: 1px solid #1a73e8;") == 1
    assert html.count("border: 1px solid #444;") == 2
    assert html.count("color: #7fd17f;") == 1
    assert html.count("color: #5fa8f0;") == 1
    assert html.count("color: #888;") == 2
