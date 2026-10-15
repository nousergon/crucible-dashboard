"""Consumer contract test — the features parquet column contract (alpha-engine-config-I10793).

Pinned copy of nousergon-data's ``contracts/features_parquet.schema.json`` lives at
``tests/contracts/features_parquet.schema.json`` here. That file is GENERATED from
nousergon-data's ``features/registry.py::CATALOG`` and describes every column of
``features/{date}/{group}.parquet`` (D31 daily, D50 settled regrade): one ``$defs``
row schema per group, ``ticker``/``date`` required, ``additionalProperties: false``.
This repo never imports nousergon-data; the versioned JSON Schema is the coupling
(same shape as ``test_p07_feature_registry_contract.py``).

The reader is ``views/13_Feature_Store.py``: ``_load_parquet`` reads the five
group files ``_group_filenames`` names, the catalog table drops ``_meta_cols``
and labels named macro / alternative columns by source. This test fails here,
at review time, when a contract change would break any of that:

  1. the pinned schema is a valid JSON Schema;
  2. every group file the page reads is a group the contract declares;
  3. every column the page names (meta columns, the macro FRED/yfinance sets,
     the alternative FMP/yfinance sets) is declared in the group the page reads
     it from;
  4. a contract-conformant snapshot, serialised to parquet and fed through the
     REAL ``_load_parquet``, comes back with the declared columns, and its rows
     validate against the pinned schema;
  5. the contract rejects an undeclared column and a missing ticker.

The page's constants are read from its source with ``ast`` (they are defined
below the page's ``st.stop()`` on an empty store, so an exec-load never reaches
them) — literal values only, so no page code runs to obtain them.

A stale copy is caught on the producer side too: nousergon-data's ``data_gate``
compares this file's validation shape with the producer's on every read.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).parent.parent
PAGE = REPO_ROOT / "views" / "13_Feature_Store.py"
CONTRACTS_DIR = Path(__file__).parent / "contracts"
DATE = "2026-10-14"


def _schema() -> dict:
    return json.loads((CONTRACTS_DIR / "features_parquet.schema.json").read_text())


def _page_literal(name: str):
    """The literal value of a module-level ``name = <literal>`` in the page."""
    tree = ast.parse(PAGE.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"views/13_Feature_Store.py no longer assigns {name!r} at module level")


def _group_of(filename: str) -> str:
    return filename.removesuffix(".parquet")


def _declared(group: str) -> dict:
    return _schema()["$defs"][group]["properties"]


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

    # Empty store -> the page halts at its own `latest_date is None` stop,
    # after `_load_parquet` is defined (see test_p07_feature_registry_contract.py).
    from loaders import s3_loader
    s3_loader._s3_get_object = lambda *a, **k: None
    s3_loader._fetch_s3_json = lambda *a, **k: None
    s3_loader.load_daily_data_health = lambda *a, **k: None

    spec = importlib.util.spec_from_file_location("_feature_store_parquet_page_under_test", PAGE)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except _StopPage:
        pass
    return mod


def _conformant_rows(group: str, n: int = 3) -> list[dict]:
    """Rows carrying every column the pinned contract declares for ``group``."""
    rows = []
    for i in range(n):
        row = {}
        for column, prop in _declared(group).items():
            if column == "ticker":
                row[column] = f"T{i}"
            elif column == "date":
                row[column] = DATE
            else:
                assert "number" in prop["type"], f"{group}.{column}: unexpected type {prop['type']}"
                row[column] = float(i) + 0.5
        rows.append(row)
    return rows


def test_pinned_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(_schema())


class TestPageReadsOnlyDeclaredColumns:
    def test_every_group_file_the_page_reads_is_a_declared_group(self):
        filenames = _page_literal("_group_filenames")
        assert set(filenames) == {"Technical", "Interaction", "Macro", "Alternative", "Fundamental"}
        for filename in filenames.values():
            assert _group_of(filename) in _schema()["$defs"], filename

    def test_meta_columns_are_the_contract_identity_columns(self):
        meta = _page_literal("_meta_cols")
        assert meta == {"ticker", "date"}
        for filename in _page_literal("_group_filenames").values():
            group = _group_of(filename)
            row = _schema()["$defs"][group]
            assert "date" in row["required"]
            if group != "macro":
                assert "ticker" in row["required"]

    @pytest.mark.parametrize(
        "constant,group",
        [
            ("_macro_fred_features", "macro"),
            ("_macro_yfinance_features", "macro"),
            ("_alt_fmp_features", "alternative"),
            ("_alt_yfinance_features", "alternative"),
        ],
    )
    def test_named_feature_sets_are_declared_in_their_group(self, constant, group):
        names = _page_literal(constant)
        missing = sorted(set(names) - set(_declared(group)))
        assert not missing, f"{constant} names columns the {group} contract does not declare: {missing}"

    def test_xsect_dispersion_is_a_macro_column(self):
        # `_resolve_runtime_source` special-cases it by name.
        assert "xsect_dispersion" in _declared("macro")


class TestRealLoaderReadsAConformantSnapshot:
    @pytest.mark.parametrize("filename", ["technical.parquet", "interaction.parquet", "macro.parquet",
                                          "alternative.parquet", "fundamental.parquet"])
    def test_load_parquet_round_trips_declared_columns(self, page_mod, monkeypatch, filename):
        group = _group_of(filename)
        rows = _conformant_rows(group)
        jsonschema.validate(instance={group: rows}, schema=_schema())
        buf = io.BytesIO()
        pd.DataFrame(rows).to_parquet(buf, index=False)
        key = f"features/{DATE}/{filename}"
        monkeypatch.setattr(page_mod, "_s3_get_object", lambda bucket, k: buf.getvalue() if k == key else None)

        df = page_mod._load_parquet("bucket", key)

        assert df is not None
        assert list(df.columns) == list(_declared(group))
        read_back = json.loads(df.to_json(orient="records"))
        jsonschema.validate(instance={group: read_back}, schema=_schema())

    def test_missing_object_returns_none(self, page_mod, monkeypatch):
        monkeypatch.setattr(page_mod, "_s3_get_object", lambda bucket, key: None)
        assert page_mod._load_parquet("bucket", f"features/{DATE}/macro.parquet") is None


class TestContractRejectsWhatThePageCannotRead:
    def test_an_undeclared_column_is_rejected(self):
        rows = _conformant_rows("technical", 1)
        rows[0]["not_a_catalog_column_pct"] = 0.1
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance={"technical": rows}, schema=_schema())

    def test_a_row_without_ticker_is_rejected(self):
        rows = _conformant_rows("fundamental", 1)
        del rows[0]["ticker"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance={"fundamental": rows}, schema=_schema())
