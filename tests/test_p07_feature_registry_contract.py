"""Consumer contract test — data-collector plan P-07 (alpha-engine-config-I10870), D12.

Pinned copy of nousergon-data's ``contracts/feature_registry.schema.json`` lives at
``tests/contracts/feature_registry.schema.json`` here (mirrors the metron
``tests/test_p07_crypto_holdings_contract.py`` precedent from the same P-07 batch,
nousergon-data-PR1747) — this repo never imports nousergon-data; the versioned
JSON Schema is the coupling.

``views/13_Feature_Store.py`` is a full Streamlit script (module-level side
effects throughout), so this exec-loads it the same way
``tests/test_book_status_banner.py`` exec-loads ``16_Order_Book_Rationale.py``:
streamlit + the S3 primitives are stubbed before ``exec_module`` runs, ``st.stop``
is made to raise a sentinel so the script halts exactly where the real page would
(``latest_date is None`` — no feature snapshot found, S3 stubbed empty), and the
module object still carries every function defined before that point, including
``_load_registry`` — the REAL consumer reader (`` views/13_Feature_Store.py:85``).

Covers:
  1. the pinned schema is itself a valid JSON Schema;
  2. a schema-conformant fixture validates and feeds through the REAL
     ``_load_registry`` reader, both the top-level ``{"features": [...]}``
     dict shape and a bare-list shape (both branches ``_load_registry`` handles);
  3. a payload missing the required ``features`` key fails schema validation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest

REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "13_Feature_Store.py"
CONTRACTS_DIR = Path(__file__).parent / "contracts"


def _schema() -> dict:
    return json.loads((CONTRACTS_DIR / "feature_registry.schema.json").read_text())


class _StopPage(Exception):
    """Sentinel standing in for streamlit's real st.stop() halting the script."""


@pytest.fixture
def page_mod():
    sys.path.insert(0, str(REPO_ROOT))

    mock_st = MagicMock()
    mock_st.cache_data = lambda **kw: (lambda f: f)
    mock_st.cache_resource = lambda **kw: (lambda f: f)
    mock_st.stop.side_effect = _StopPage
    sys.modules["streamlit"] = mock_st

    # No S3 objects exist -> _find_latest_feature_date returns None ->
    # the page's own `if latest_date is None: st.stop()` fires, which is
    # exactly where we want exec to halt: after _load_registry is defined
    # (line ~85), before the rest of the page's parquet-heavy body (line 377+).
    from loaders import s3_loader
    s3_loader._s3_get_object = lambda *a, **k: None
    s3_loader._fetch_s3_json = lambda *a, **k: None
    s3_loader.load_daily_data_health = lambda *a, **k: None

    spec = importlib.util.spec_from_file_location("_feature_store_page_under_test", PAGE)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except _StopPage:
        pass
    return mod


def test_pinned_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(_schema())


def _dict_fixture() -> dict:
    return {
        "features": [
            {
                "name": "rsi_14", "group": "technical", "description": "RSI(14)",
                "dtype": "float32", "source": "yfinance", "refresh": "daily",
                "per_ticker": True, "compute": "", "units": "0-100 score",
                "formula": "Wilder's RSI(14)", "consumers": "predictor + scanner",
                "display_order": 0,
            },
        ],
    }


class TestFixtureValidatesAgainstPinnedSchema:
    def test_dict_shape_validates(self):
        jsonschema.validate(instance=_dict_fixture(), schema=_schema())

    def test_missing_features_key_is_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance={}, schema=_schema())

    def test_entry_missing_required_field_is_rejected(self):
        bad = _dict_fixture()
        del bad["features"][0]["group"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=_schema())


class TestRealLoaderReaderExtractsPinnedFields:
    """``_load_registry`` is the ACTUAL reader (views/13_Feature_Store.py:85)
    — exercising it directly against a schema-conformant fixture, via both
    shapes it explicitly branches on, is the consumer half of this contract."""

    def test_dict_shape_returns_the_features_list(self, page_mod, monkeypatch):
        fixture = _dict_fixture()
        jsonschema.validate(instance=fixture, schema=_schema())
        monkeypatch.setattr(page_mod, "_fetch_s3_json", lambda bucket, key: fixture)
        out = page_mod._load_registry("bucket")
        assert out == fixture["features"]
        assert out[0]["name"] == "rsi_14"

    def test_bare_list_shape_passes_through(self, page_mod, monkeypatch):
        fixture = _dict_fixture()["features"]
        monkeypatch.setattr(page_mod, "_fetch_s3_json", lambda bucket, key: fixture)
        out = page_mod._load_registry("bucket")
        assert out == fixture

    def test_missing_artifact_returns_none(self, page_mod, monkeypatch):
        monkeypatch.setattr(page_mod, "_fetch_s3_json", lambda bucket, key: None)
        assert page_mod._load_registry("bucket") is None
