"""Consumer-side schema conformance for the ``report_card`` contract
(alpha-engine-config#3026, dashboard consumer half).

crucible-evaluator's producer half is covered by
``crucible-evaluator/tests/test_report_card_producer_contract.py`` (validates
the assembled artifact against ``nousergon_lib.contracts`` v1). This repo is
the boundary's ONLY consumer and, until now, had no test that the frozen
schema — imported from the INSTALLED ``nousergon_lib.contracts`` package via
``importlib.resources`` — still declares the fields the dashboard actually
reads off ``evaluator/{date}/report_card.json``. Consumer call sites traced:

  * ``components/report_card.py::render_report_card`` — ``overall.{grade,letter}``,
    ``research|predictor|executor.{grade,letter,components}``, per-component
    ``{letter,reason}``.
  * ``components/report_card_v2.py`` — ``tiles_overall_status``,
    ``tiles[*].{status,numeric_grade,letter,components}``, per-component
    ``{name,criticality,status,value,metric_type,ci_low,ci_high,n_samples,
    n_floor,target,red_line,trend_decoration,status_reason}``,
    ``_provenance.{run_date,artifacts.{n_read,n_missing}}``, and — the
    documented finding below — ``attestation`` / ``degraded_staleness`` /
    ``stale_tiles``.
  * ``results/view_model.py::build_experiment_meta`` — ``_provenance.{run_date,
    grader_source}``.
  * ``loaders/s3_loader.py::load_report_card`` — pass-through S3 fetch, no
    field-level parsing; not a schema-shaped call site.

KNOWN GAP (report, not silently patched): ``components/report_card_v2.py``'s
``render_attestation`` reads ``card["attestation"]`` and
``card["degraded_staleness"]`` / ``card["stale_tiles"]`` off the card root, and
the LIVE producer (``s3://alpha-engine-research/evaluator/2026-08-14/report_card.json``,
fetched 2026-08-18) already emits ``attestation`` and ``degraded_attestation``
at the root. The nousergon-lib schema PINNED here (v0.124.62, contract v1) does
NOT declare any of ``attestation`` / ``degraded_attestation`` /
``degraded_staleness`` / ``stale_tiles`` in ``properties`` — additive-only
evolution means a payload lacking them still validates, so this is not a hard
failure of ``contracts.validate``, but it is a real drift: the schema the
dashboard's own contract test can check against does not yet cover fields the
dashboard has depended on since the attestation banner shipped. nousergon-lib
PR324 (open, green, unmerged at test-writing time) adds ``attestation`` (and
presumably ``degraded_attestation``) to the schema's ``properties``/
``required``. This test pins the CURRENT contract version (v1) and expresses
the gap via ``TestSchemaDoesNotYetDeclareAttestation`` below rather than
widening the schema itself (out of scope — ``nousergon-lib`` is not edited by
this PR) or silently dropping the field from the fixture payload.
"""

from __future__ import annotations

import json
import os

import pytest

contracts = pytest.importorskip(
    "nousergon_lib.contracts",
    reason="needs nousergon-lib[contracts] (jsonschema) installed",
)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FIXTURE = os.path.join(_REPO, "tests", "fixtures", "report_card_live_shaped.json")


def _live_shaped_payload() -> dict:
    """A payload shaped like the live artifact (evaluator/2026-08-14/report_card.json,
    fetched via AWS_PROFILE=ne-admin 2026-08-18), trimmed of bulk arrays
    (per-check known-answer lists, 13-week trend series) that carry no
    contract-shape information — not a hand-built toy dict."""
    with open(_FIXTURE) as f:
        return json.load(f)


class TestLiveShapedPayloadValidates:
    """The v1 contract still accepts a payload shaped like what the evaluator
    is actually emitting today (minus the not-yet-schematized attestation
    fields — additive fields never break validation)."""

    def test_conforms_to_pinned_schema(self):
        contracts.validate("report_card", _live_shaped_payload())

    def test_schema_version_is_pinned_v1(self):
        assert contracts.SCHEMA_VERSIONS["report_card"] == 1


class TestConsumerFieldsAreDeclaredByTheSchema:
    """Fail when the producer's contract stops carrying a field this
    dashboard's rendering code depends on. Each assertion below names the
    exact consumer call site the field backs."""

    @staticmethod
    @pytest.fixture(scope="class")
    def schema():
        return contracts.load_schema("report_card")

    def test_top_level_required_fields_cover_report_card_py_and_view_model(self, schema):
        # components/report_card.py::render_report_card reads overall/research/
        # predictor/executor directly off the card root; results/view_model.py
        # ::build_experiment_meta reads _provenance off the root.
        required = set(schema["required"])
        needed = {"overall", "research", "predictor", "executor", "_provenance", "tiles", "tiles_overall_status"}
        assert needed <= required, f"missing from report_card schema required: {needed - required}"

    def test_grade_block_declares_grade_and_letter(self, schema):
        # components/report_card.py::_render_tile reads module.get("letter"),
        # module.get("grade") for overall/research/predictor/executor.
        block = schema["$defs"]["grade_block"]
        assert set(block["required"]) >= {"grade", "letter"}

    def test_module_block_declares_components(self, schema):
        # components/report_card.py::_render_component_expander iterates
        # module["components"].items().
        block = schema["$defs"]["module_block"]
        assert set(block["required"]) >= {"grade", "letter", "components"}

    def test_tile_declares_status(self, schema):
        # report_card_v2.py::_chip / render_home_summary reads tile["status"].
        tile = schema["$defs"]["tile"]
        assert "status" in tile["required"]

    def test_tile_schema_documents_the_additive_metric_record_fields(self, schema):
        # report_card_v2.py::render_detail reads numeric_grade, letter,
        # components (list), and per-component value/ci_low/ci_high/n_samples/
        # n_floor/target/red_line/trend_decoration/status_reason/criticality/
        # name off each tile. None of these are in the tile $defs' `required`
        # (only `status` is — additive-only evolution, by design per
        # config#2343), but the schema's own description names them as the
        # intended additive MetricRecord surface; assert that documentation
        # hasn't silently narrowed, which would be the signal that this
        # consumer surface is no longer an intentional contract extension.
        desc = schema["$defs"]["tile"]["description"]
        for field in ("value", "ci_low", "ci_high", "n_samples", "target", "red_line", "criticality"):
            assert field in desc, f"tile schema description no longer documents {field!r} as additive"

    def test_provenance_declares_run_date_and_grader_source(self, schema):
        # results/view_model.py::build_experiment_meta reads
        # provenance.get("run_date") / provenance.get("grader_source").
        prov = schema["properties"]["_provenance"]
        assert set(prov["required"]) >= {"run_date", "grader_source", "artifacts"}

    def test_provenance_artifacts_declares_n_read_and_n_missing(self, schema):
        # report_card_v2.py::_provenance_caption reads
        # artifacts.get("n_read") / artifacts.get("n_missing").
        arts = schema["properties"]["_provenance"]["properties"]["artifacts"]["properties"]
        assert {"n_read", "n_missing"} <= set(arts)


class TestSchemaDoesNotYetDeclareAttestation:
    """KNOWN GAP (report, don't silently widen): report_card_v2.py's
    render_attestation is the FIRST element rendered on every Report Card
    surface (sf-pipeline-policy.md Sec 2.3a rule 3) and reads
    card["attestation"], card["degraded_staleness"], card["stale_tiles"] off
    the root. The v1 schema pinned here (nousergon-lib v0.124.62) declares
    none of them. The live producer already emits `attestation` and
    `degraded_attestation` (verified 2026-08-18 against
    s3://alpha-engine-research/evaluator/2026-08-14/report_card.json) — so this
    is schema/producer drift the schema hasn't caught up to, not a producer
    that has stopped emitting the field. nousergon-lib PR324 (open at
    test-writing time) closes this by adding `attestation` +
    `degraded_attestation` to `properties`/`required`.

    These are xfail (not skip): once PR324 merges and this repo's pin bumps
    past it, this class should start FAILING (proving the gap closed) so the
    xfail markers get deleted then, not silently rot as false green forever.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def schema():
        return contracts.load_schema("report_card")

    @pytest.mark.xfail(
        reason="report_card_v2.py::render_attestation reads card['attestation'] "
        "(nousergon-lib PR324, unmerged) — not yet in schema properties",
        strict=True,
    )
    def test_attestation_is_a_declared_property(self, schema):
        assert "attestation" in schema["properties"]

    @pytest.mark.xfail(
        reason="report_card_v2.py::render_attestation reads "
        "card['degraded_staleness'] — not yet in schema properties",
        strict=True,
    )
    def test_degraded_staleness_is_a_declared_property(self, schema):
        assert "degraded_staleness" in schema["properties"]

    @pytest.mark.xfail(
        reason="report_card_v2.py::render_attestation reads card['stale_tiles'] "
        "— not yet in schema properties",
        strict=True,
    )
    def test_stale_tiles_is_a_declared_property(self, schema):
        assert "stale_tiles" in schema["properties"]

    def test_additive_undeclared_fields_still_validate(self):
        # The flip side of the gap: because the schema leaves additionalProperties
        # open at the root, a payload carrying attestation/degraded_staleness/
        # stale_tiles (as the live producer does) is NOT rejected — it's simply
        # unchecked. Pin that behavior so a future `additionalProperties: false`
        # tightening on this schema is a deliberate, visible break here rather
        # than a silent new failure mode.
        payload = _live_shaped_payload()
        assert "attestation" in payload
        assert "degraded_staleness" in payload
        contracts.validate("report_card", payload)  # does not raise
