"""FLAT-direction retirement from live display (predictor#343, 2026-07-06;
config#1984 item 3).

Direction is now sign(alpha) — UP/DOWN only. Two pages still rendered FLAT
as if live:

  - views/2_Signals_and_Research.py: a P(FLAT) probability bar and a
    "FLAT →" direction-arrow mapping.
  - views/7_Predictor.py: a FLAT scatter-legend entry, a FLAT arrow mapping,
    and a P(FLAT) table column.

Backward-compat constraint: pre-2026-07-06 archived prediction JSON may
still carry a ``p_flat`` field / ``predicted_direction: "FLAT"`` value —
parsing must tolerate it (no crash), it just must not be headlined as a
live state. Source-text guards + isolated dict/lambda checks (mirrors the
lightweight convention in tests/test_predictor_page.py — these pages are
large and S3-backed at module level, so a full exec-load isn't the cheapest
way to pin a purely-cosmetic display fix).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SIGNALS_SRC = (REPO_ROOT / "views" / "2_Signals_and_Research.py").read_text()
PREDICTOR_SRC = (REPO_ROOT / "views" / "7_Predictor.py").read_text()


class TestSignalsAndResearchFlatRemoved:
    def test_p_flat_bar_removed_from_probability_chart(self):
        assert 'name="P(FLAT)"' not in SIGNALS_SRC
        assert 'pred.get("p_flat"' not in SIGNALS_SRC

    def test_flat_arrow_mapping_removed(self):
        assert '"FLAT": "FLAT →"' not in SIGNALS_SRC

    def test_up_down_arrow_mapping_still_present(self):
        assert '"UP": "UP ↑", "DOWN": "DOWN ↓"' in SIGNALS_SRC

    def test_prediction_map_tolerates_old_flat_value(self):
        # Replicates the exact mapping expression: an old archived "FLAT"
        # value must fall through to "" (no KeyError/crash), not be
        # headlined as a display label.
        mapping = {"UP": "UP ↑", "DOWN": "DOWN ↓"}
        assert mapping.get("FLAT", "") == ""
        assert mapping.get("UP", "") == "UP ↑"


class TestPredictorPageFlatRemoved:
    def test_flat_scatter_legend_entry_removed(self):
        assert '("FLAT", "#94a3b8")' not in PREDICTOR_SRC

    def test_flat_arrow_mapping_removed(self):
        assert '"FLAT": "→"' not in PREDICTOR_SRC

    def test_p_flat_column_removed_from_table(self):
        assert '"P(FLAT)"' not in PREDICTOR_SRC
        assert 'pred.get("p_flat")' not in PREDICTOR_SRC

    def test_format_columns_no_longer_include_p_flat(self):
        assert '["Confidence", "P(UP)", "P(DOWN)"]' in PREDICTOR_SRC

    def test_score_modifier_still_uses_only_p_up_p_down(self):
        # The (p_up - p_down) score-modifier logic was already FLAT-agnostic
        # — confirm it's untouched (not accidentally broken by the sweep).
        assert "(p_up - p_down) * 10.0 * conf" in PREDICTOR_SRC

    def test_direction_arrow_map_tolerates_old_flat_value(self):
        mapping = {"UP": "↑", "DOWN": "↓"}
        assert mapping.get("FLAT", "") == ""


class TestVetoThresholdHonestDefaultLabel:
    """I1984 item 4a: config/predictor_params.json has never been written
    (config#1841) — views/2_Signals_and_Research.py's veto-threshold reads
    must not present the constants fallback as if it were a promoted/live
    override."""

    def test_default_source_label_computed(self):
        assert "_veto_is_default" in SIGNALS_SRC
        assert '"veto_confidence" not in predictor_params' in SIGNALS_SRC

    def test_default_label_text_present(self):
        assert "default (no promoted override)" in SIGNALS_SRC

    def test_warning_and_caption_include_source_label(self):
        assert "{veto_threshold:.0%}, {_veto_source_label}" in SIGNALS_SRC

    def test_label_logic_matches_never_written_state(self):
        # config/predictor_params.json is never written today, so this
        # must always resolve to the honest "default" label, not silently
        # claim a configured override.
        predictor_params = {}
        veto_is_default = "veto_confidence" not in predictor_params
        label = "default (no promoted override)" if veto_is_default else "configured"
        assert label == "default (no promoted override)"
