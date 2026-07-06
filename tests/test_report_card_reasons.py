"""config#859 Problem 1b — System Report Card N/A honesty.

An N/A sub-component is a producer-INPUT gap (the upstream analysis wasn't
produced/persisted this cycle), not a sample-size story. The card must (1) not
frame N/A as "grades need weeks to accumulate", and (2) surface the specific
per-component reason the grader now emits.
"""
from unittest.mock import MagicMock

from components import report_card


def _joined(mock_method) -> str:
    return " ".join(str(c.args[0]) for c in mock_method.call_args_list if c.args)


def test_header_caption_drops_sample_size_framing():
    report_card.st.reset_mock()
    report_card.st.columns.return_value = (MagicMock(), MagicMock(), MagicMock())
    report_card.render_report_card({"overall": {"letter": "B", "grade": 80}})
    caps = _joined(report_card.st.caption)
    # The misleading maturity framing is gone …
    assert "4–8 weeks" not in caps
    assert "data accumulates" not in caps
    # … replaced by the honest producer-input framing.
    assert "wasn't produced this cycle" in caps


def test_component_reason_is_surfaced_verbatim():
    report_card.st.reset_mock()
    module = {
        "letter": "N/A",
        "grade": None,
        "components": {
            "risk_guard": {
                "letter": "N/A",
                "reason": "no shadow-book sweep this cycle",
            },
        },
    }
    report_card._render_component_expander(module)
    md = _joined(report_card.st.markdown)
    assert "no shadow-book sweep this cycle" in md
    assert "insufficient data" not in md


def test_missing_reason_falls_back_to_honest_phrase():
    report_card.st.reset_mock()
    module = {"components": {"veto_gate": {"letter": "N/A"}}}
    report_card._render_component_expander(module)
    md = _joined(report_card.st.markdown)
    assert "upstream analysis not produced this cycle" in md
    assert "insufficient data" not in md
