"""Tests for the Distillation Corpus console page + its loader.

Covers the stats loader (``load_distillation_corpus_stats``) and the
nav-registration contract (app.py must register ``43_Distillation_Corpus.py``
under Backtester & Eval). The page reads the deduped stats artifact written by
crucible-research ``scripts/corpus_stats.py`` (config#1544) at
``decision_artifacts/distillation/corpus_stats/latest.json``.

Mirrors test_backlog_groom_page.py: streamlit is mocked (cache_data ->
passthrough) and the page module itself is NOT imported (its module-level
Streamlit calls need a live runtime) — page wiring is asserted against source
text instead.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

from loaders import s3_loader  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


class TestLoadDistillationCorpusStats:
    def _payload(self) -> dict:
        return {
            "schema_version": 1,
            "trigger": {"task": "sector_quant", "target_pairs": 1000,
                        "deduped_single_teacher": 46, "dominant_teacher": "haiku",
                        "pct": 4.6, "crossed": False, "clock_started": False},
            "totals": {"raw_records": 275, "unparseable": 0,
                       "duplicates_dropped": 15, "deduped_pairs": 260},
            "by_task": {"sector_quant": 46, "sector_qual": 39},
            "capture": {"last_captured_date": "2026-06-27",
                        "missing_saturdays": ["2026-06-20"]},
            "growth": [{"date": "2026-06-27", "added": 132, "cumulative": 260}],
        }

    def test_returns_dict_on_valid_json(self):
        payload = self._payload()
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object",
                             return_value=json.dumps(payload).encode()):
            got = s3_loader.load_distillation_corpus_stats()
        assert got["trigger"]["deduped_single_teacher"] == 46
        assert got["totals"]["deduped_pairs"] == 260

    def test_returns_none_on_missing(self):
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", return_value=None):
            assert s3_loader.load_distillation_corpus_stats() is None

    def test_reads_canonical_latest_key(self):
        seen = {}

        def _fake_get(bucket, key):
            seen["key"] = key
            return None

        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_s3_get_object", side_effect=_fake_get):
            s3_loader.load_distillation_corpus_stats()
        assert seen["key"] == "decision_artifacts/distillation/corpus_stats/latest.json"


class TestNavRegistration:
    def test_page_registered_in_app_nav(self):
        src = (REPO_ROOT / "app.py").read_text()
        assert "43_Distillation_Corpus.py" in src, \
            "Distillation Corpus page must be registered in app.py navigation"

    def test_page_file_exists(self):
        assert (REPO_ROOT / "views" / "43_Distillation_Corpus.py").exists()
