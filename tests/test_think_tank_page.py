"""Tests for the Think Tank console page + its loaders (config#1579).

Covers the thinktank S3 loaders (ratings board / thesis / theme / manifests /
month costs) and the nav-registration contract (app.py must register
``44_Think_Tank.py`` under Research & Signals). The page reads the
``thinktank/`` artifacts written by crucible-research (ratings board:
``thinktank/ratings.py``; theses/themes/manifests: ``thinktank/run.py``).

Mirrors test_backlog_groom_page.py: streamlit is mocked (cache_data →
passthrough) and the page module itself is NOT imported (its module-level
Streamlit calls need a live runtime) — page wiring is asserted against
source text instead.
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


def _patch_json(payload):
    return patch.object(
        s3_loader, "_s3_get_object", return_value=json.dumps(payload).encode()
    )


class TestThinktankLoaders:
    def test_ratings_board_roundtrip(self):
        payload = {
            "schema_version": 1,
            "trading_day": "2026-07-02",
            "rows": {
                "MNST": {
                    "ticker": "MNST",
                    "rating": 72,
                    "attractiveness_score": 61.4,
                    "rating_minus_attractiveness": 10.6,
                }
            },
        }
        with patch.object(s3_loader, "_research_bucket", return_value="b"), _patch_json(payload):
            assert s3_loader.load_thinktank_ratings() == payload

    def test_thesis_latest_vs_versioned_keys(self):
        captured = []
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_fetch_s3_json",
                             side_effect=lambda b, k: captured.append(k) or {}):
            s3_loader.load_thinktank_thesis("MNST")
            s3_loader.load_thinktank_thesis("MNST", version=2)
        assert captured == [
            "thinktank/theses/MNST/latest.json",
            "thinktank/theses/MNST/v2.json",
        ]

    def test_theme_key_shape(self):
        captured = []
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_fetch_s3_json",
                             side_effect=lambda b, k: captured.append(k) or {}):
            s3_loader.load_thinktank_theme("macro", "macro")
            s3_loader.load_thinktank_theme("sector", "Tech")
        assert captured == [
            "thinktank/themes/macro/macro/latest.json",
            "thinktank/themes/sector/Tech/latest.json",
        ]

    def test_manifest_listing_sorts_newest_first_and_limits(self):
        keys = [
            {"Key": f"thinktank/runs/2026-07-0{d}/manifest_{d}.json"}
            for d in (1, 2, 3)
        ]
        client = MagicMock()
        client.get_paginator.return_value.paginate.return_value = [
            {"Contents": keys}
        ]
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "get_s3_client", return_value=client):
            out = s3_loader.list_thinktank_manifest_keys(limit=2)
        assert out == [
            "thinktank/runs/2026-07-03/manifest_3.json",
            "thinktank/runs/2026-07-02/manifest_2.json",
        ]

    def test_month_costs_key(self):
        captured = []
        with patch.object(s3_loader, "_research_bucket", return_value="b"), \
                patch.object(s3_loader, "_fetch_s3_json",
                             side_effect=lambda b, k: captured.append(k) or {}):
            s3_loader.load_thinktank_month_costs("2026-07")
        assert captured == ["thinktank/costs/2026-07.json"]


class TestPageWiring:
    def test_page_file_exists(self):
        assert (REPO_ROOT / "views" / "44_Think_Tank.py").is_file()

    def test_app_registers_page_under_research_signals(self):
        app = (REPO_ROOT / "app.py").read_text()
        assert '44_Think_Tank.py' in app, (
            "app.py must register the Think Tank page in st.navigation"
        )

    def test_page_reads_only_recorded_artifacts(self):
        """The console is read-only: the page must not import boto3 writers
        or the OpenAI client — it renders recorded thinktank/ S3 artifacts."""
        src = (REPO_ROOT / "views" / "44_Think_Tank.py").read_text()
        assert "put_object" not in src
        assert "openai" not in src.lower()

    def test_page_surfaces_the_divergence_column(self):
        """The independent-rating divergence is the page's headline —
        renaming the column upstream must break this pin, not silently
        drop the surface."""
        src = (REPO_ROOT / "views" / "44_Think_Tank.py").read_text()
        assert "rating_minus_attractiveness" in src
