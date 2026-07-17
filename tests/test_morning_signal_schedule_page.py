"""Tests for the Morning Signal Content Schedule page + its loader.

Covers ``loaders/morning_signal_schedule.py`` (fetch/save with S3
conditional writes + the compare-then-put fallback, upsert/delete
semantics, applied markers) and the cross-repo contract (the validator +
fixtures duplicated identically in morning-signal — see
``test_schedule_contract`` class). Page wiring is asserted against source
text (mirrors test_backlog_groom_page.py: the page module's module-level
Streamlit calls need a live runtime, so it is never imported here).
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

mock_st = MagicMock()
mock_st.cache_data = lambda **kwargs: (lambda f: f)
mock_st.cache_resource = lambda **kwargs: (lambda f: f)
sys.modules["streamlit"] = mock_st

from botocore.exceptions import ClientError, ParamValidationError  # noqa: E402

from loaders import morning_signal_schedule as mss  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "schedule"


def _valid_manifest() -> dict:
    return {
        "schema_version": 1,
        "entries": {
            "2026-07-04": {
                "mode": "override",
                "topic": "Financial ML SOTA",
                "editions": ["am"],
            }
        },
    }


def _get_object_response(manifest: dict, etag: str = '"abc123"') -> dict:
    body = MagicMock()
    body.read.return_value = json.dumps(manifest).encode()
    return {"Body": body, "ETag": etag}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class TestFetchSchedule:
    def test_returns_manifest_and_etag(self):
        client = MagicMock()
        client.get_object.return_value = _get_object_response(_valid_manifest())
        with patch.object(mss, "_ms_client", return_value=client):
            manifest, etag, error = mss._fetch_schedule()
        assert error is None
        assert etag == '"abc123"'
        assert "2026-07-04" in manifest["entries"]

    def test_missing_object_returns_empty_manifest_no_error(self):
        client = MagicMock()
        client.get_object.side_effect = _client_error("NoSuchKey")
        with patch.object(mss, "_ms_client", return_value=client):
            manifest, etag, error = mss._fetch_schedule()
        assert error is None
        assert etag is None
        assert manifest == {"schema_version": 1, "entries": {}}

    def test_error_returns_error_string_never_raises(self):
        client = MagicMock()
        client.get_object.side_effect = _client_error("AccessDenied")
        with patch.object(mss, "_ms_client", return_value=client):
            manifest, etag, error = mss._fetch_schedule()
        assert error is not None
        assert manifest["entries"] == {}

    def test_rejects_invalid_manifest_with_error(self):
        client = MagicMock()
        client.get_object.return_value = _get_object_response(
            {"schema_version": 2, "entries": {}}
        )
        with patch.object(mss, "_ms_client", return_value=client):
            manifest, etag, error = mss._fetch_schedule()
        assert error is not None and "schema_version" in error
        assert manifest["entries"] == {}


class TestSaveSchedule:
    def test_conditional_put_sends_ifmatch(self):
        client = MagicMock()
        with patch.object(mss, "_ms_client", return_value=client):
            ok, msg = mss.save_schedule(_valid_manifest(), if_match='"abc"')
        assert ok, msg
        kwargs = client.put_object.call_args.kwargs
        assert kwargs["IfMatch"] == '"abc"'
        assert kwargs["ContentType"] == "application/json"
        assert kwargs["Key"] == mss.SCHEDULE_KEY

    def test_create_sends_ifnonematch_star(self):
        client = MagicMock()
        with patch.object(mss, "_ms_client", return_value=client):
            ok, _ = mss.save_schedule(_valid_manifest(), if_match=None)
        assert ok
        assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"

    def test_412_returns_conflict(self):
        client = MagicMock()
        client.put_object.side_effect = _client_error("PreconditionFailed")
        with patch.object(mss, "_ms_client", return_value=client):
            ok, msg = mss.save_schedule(_valid_manifest(), if_match='"abc"')
        assert not ok
        assert msg == "conflict"

    def test_paramvalidation_falls_back_to_compare_and_put(self):
        """Old botocore (no conditional writes): re-fetch, compare etags,
        unconditional put when they match."""
        client = MagicMock()
        client.put_object.side_effect = [
            ParamValidationError(report="IfMatch unknown"),
            {},
        ]
        with patch.object(mss, "_ms_client", return_value=client), patch.object(
            mss, "_fetch_schedule",
            return_value=(_valid_manifest(), '"abc"', None),
        ):
            ok, msg = mss.save_schedule(_valid_manifest(), if_match='"abc"')
        assert ok, msg
        second_kwargs = client.put_object.call_args_list[1].kwargs
        assert "IfMatch" not in second_kwargs and "IfNoneMatch" not in second_kwargs

    def test_paramvalidation_fallback_detects_conflict(self):
        client = MagicMock()
        client.put_object.side_effect = ParamValidationError(report="IfMatch")
        with patch.object(mss, "_ms_client", return_value=client), patch.object(
            mss, "_fetch_schedule",
            return_value=(_valid_manifest(), '"OTHER"', None),
        ):
            ok, msg = mss.save_schedule(_valid_manifest(), if_match='"abc"')
        assert not ok
        assert msg == "conflict"

    def test_put_failure_returns_false_never_raises(self):
        client = MagicMock()
        client.put_object.side_effect = _client_error("AccessDenied")
        with patch.object(mss, "_ms_client", return_value=client):
            ok, msg = mss.save_schedule(_valid_manifest(), if_match='"abc"')
        assert not ok
        assert "AccessDenied" in msg

    def test_refuses_to_write_invalid_manifest(self):
        client = MagicMock()
        bad = {"schema_version": 1, "entries": {"2026-07-04": {"mode": "nope"}}}
        with patch.object(mss, "_ms_client", return_value=client):
            ok, msg = mss.save_schedule(bad, if_match=None)
        assert not ok
        assert "invalid" in msg
        client.put_object.assert_not_called()

    def test_save_stamps_updated_at(self):
        client = MagicMock()
        with patch.object(mss, "_ms_client", return_value=client):
            ok, _ = mss.save_schedule(_valid_manifest(), if_match=None)
        assert ok
        written = json.loads(client.put_object.call_args.kwargs["Body"])
        assert written["updated_at_utc"].endswith("Z")


class TestUpsertDelete:
    def test_upsert_adds_entry_and_stamps_timestamps(self):
        saved = {}

        def _capture(manifest, *, if_match):
            saved.update(manifest)
            return True, "saved"

        with patch.object(
            mss, "_fetch_schedule",
            return_value=(_valid_manifest(), '"abc"', None),
        ), patch.object(mss, "save_schedule", side_effect=_capture):
            ok, _ = mss.upsert_entry(
                "2026-07-05", {"mode": "skip", "editions": ["am", "pm"]}
            )
        assert ok
        entry = saved["entries"]["2026-07-05"]
        assert entry["created_at_utc"].endswith("Z")
        assert entry["updated_at_utc"].endswith("Z")

    def test_upsert_preserves_created_at_on_edit(self):
        manifest = _valid_manifest()
        manifest["entries"]["2026-07-04"]["created_at_utc"] = "2026-07-01T00:00:00Z"
        saved = {}

        def _capture(m, *, if_match):
            saved.update(m)
            return True, "saved"

        with patch.object(
            mss, "_fetch_schedule", return_value=(manifest, '"abc"', None)
        ), patch.object(mss, "save_schedule", side_effect=_capture):
            ok, _ = mss.upsert_entry(
                "2026-07-04", {"mode": "override", "topic": "Edited"}
            )
        assert ok
        assert (
            saved["entries"]["2026-07-04"]["created_at_utc"]
            == "2026-07-01T00:00:00Z"
        )

    def test_upsert_rejects_bad_date_key(self):
        ok, msg = mss.upsert_entry("2026-7-5", {"mode": "skip"})
        assert not ok and "invalid date" in msg

    def test_upsert_refuses_when_schedule_unreadable(self):
        with patch.object(
            mss, "_fetch_schedule",
            return_value=({"schema_version": 1, "entries": {}}, None, "boom"),
        ):
            ok, msg = mss.upsert_entry("2026-07-05", {"mode": "skip"})
        assert not ok and "unreadable" in msg

    def test_delete_removes_entry(self):
        saved = {}

        def _capture(m, *, if_match):
            saved.update(m)
            return True, "saved"

        with patch.object(
            mss, "_fetch_schedule",
            return_value=(_valid_manifest(), '"abc"', None),
        ), patch.object(mss, "save_schedule", side_effect=_capture):
            ok, _ = mss.delete_entry("2026-07-04")
        assert ok
        assert saved["entries"] == {}

    def test_delete_missing_entry_reports(self):
        with patch.object(
            mss, "_fetch_schedule",
            return_value=(_valid_manifest(), '"abc"', None),
        ):
            ok, msg = mss.delete_entry("2026-07-06")
        assert not ok and "no entry" in msg


class TestAppliedMarkers:
    def _client(self, keys_bodies: dict[str, dict]):
        page = {"Contents": [{"Key": k} for k in keys_bodies]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator

        def _get(Bucket, Key):
            body = MagicMock()
            body.read.return_value = json.dumps(keys_bodies[Key]).encode()
            return {"Body": body}

        client.get_object.side_effect = _get
        return client

    def test_lists_and_parses_markers(self):
        marker = {"date": "2026-07-04", "edition": "am", "mode": "override"}
        client = self._client(
            {f"{mss.APPLIED_PREFIX}2026-07-04-am.json": marker}
        )
        with patch.object(mss, "_ms_client", return_value=client):
            markers = mss.load_applied_markers()
        assert markers == {"2026-07-04-am": marker}

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(mss, "_ms_client", return_value=client):
            assert mss.load_applied_markers() == {}


class TestLlmDecisions:
    """load_llm_decisions() mirrors load_applied_markers()'s list+fetch+
    cache shape exactly (config#1659, morning-signal#106/#107)."""

    def _client(self, keys_bodies: dict[str, dict]):
        page = {"Contents": [{"Key": k} for k in keys_bodies]}
        paginator = MagicMock()
        paginator.paginate.return_value = [page]
        client = MagicMock()
        client.get_paginator.return_value = paginator

        def _get(Bucket, Key):
            body = MagicMock()
            body.read.return_value = json.dumps(keys_bodies[Key]).encode()
            return {"Body": body}

        client.get_object.side_effect = _get
        return client

    def test_lists_and_parses_decisions(self):
        record = {
            "date": "2026-07-06", "edition": "am",
            "primary_provider": "openrouter", "primary_model": "moonshotai/kimi-k2.6",
            "used_provider": "openrouter", "used_model": "moonshotai/kimi-k2.6",
            "fell_back": False,
        }
        client = self._client(
            {f"{mss.LLM_DECISIONS_PREFIX}2026-07-06-am.llm_decision.json": record}
        )
        with patch.object(mss, "_ms_client", return_value=client):
            decisions = mss.load_llm_decisions()
        assert decisions == {"2026-07-06-am": record}

    def test_empty_on_error(self):
        client = MagicMock()
        client.get_paginator.side_effect = RuntimeError("boom")
        with patch.object(mss, "_ms_client", return_value=client):
            assert mss.load_llm_decisions() == {}

    def test_unreadable_record_skipped_not_raised(self):
        client = self._client({})
        paginator = client.get_paginator.return_value
        paginator.paginate.return_value = [
            {"Contents": [{"Key": f"{mss.LLM_DECISIONS_PREFIX}2026-07-06-am.llm_decision.json"}]}
        ]
        client.get_object.side_effect = RuntimeError("boom")
        with patch.object(mss, "_ms_client", return_value=client):
            assert mss.load_llm_decisions() == {}


class TestScheduleContract:
    """Cross-repo contract: identical validator + identical fixtures as
    morning-signal (src/morning_signal/schedule_override.py +
    tests/fixtures/schedule/). Drift fails CI on whichever side moved."""

    def test_schema_version_pinned(self):
        assert mss.SCHEMA_VERSION == 1

    def test_valid_fixture_passes(self):
        doc = json.loads((FIXTURES / "schedule_valid.json").read_text())
        assert mss.validate_schedule_manifest(doc) == []

    def test_valid_fixture_covers_all_modes(self):
        doc = json.loads((FIXTURES / "schedule_valid.json").read_text())
        assert {e["mode"] for e in doc["entries"].values()} == {
            "override", "extend", "skip",
        }

    @pytest.mark.parametrize(
        "fixture,needle",
        [
            ("schedule_invalid_mode.json", "mode"),
            ("schedule_missing_topic.json", "topic"),
            ("schedule_bad_version.json", "schema_version"),
        ],
    )
    def test_invalid_fixtures_rejected(self, fixture, needle):
        doc = json.loads((FIXTURES / fixture).read_text())
        errors = mss.validate_schedule_manifest(doc)
        assert errors
        assert any(needle in e for e in errors)


class TestNavRegistration:
    def test_page_file_exists(self):
        assert (REPO_ROOT / "views" / "45_Morning_Signal_Schedule.py").exists()

    def test_app_registers_page(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert 'page("45_Morning_Signal_Schedule.py"' in app_src
        assert "Morning Signal" in app_src

    def test_requirements_pin_streamlit_calendar_and_boto3_floor(self):
        # config#2357 (2026-07-14, operator ruling 2026-07-13): the floor
        # syntax this test cares about lives in requirements.in now —
        # requirements.txt is the uv-compiled, fully `==`-pinned lock
        # generated FROM requirements.in, so the floor string no longer
        # appears there verbatim.
        reqs = (REPO_ROOT / "requirements.in").read_text()
        assert "streamlit-calendar>=" in reqs
        # Floor must stay >= 1.36 (S3 conditional-write support); parse the
        # declared floor instead of string-matching it so routine floor bumps
        # (e.g. Dependabot) don't break the guard.
        m = re.search(r"^boto3>=(\d+)\.(\d+)", reqs, flags=re.MULTILINE)
        assert m, "boto3 floor missing from requirements.in"
        assert (int(m.group(1)), int(m.group(2))) >= (1, 36)

    def test_page_uses_calendar_component_and_loaders(self):
        src = (
            REPO_ROOT / "views" / "45_Morning_Signal_Schedule.py"
        ).read_text()
        assert "from streamlit_calendar import calendar" in src
        assert "dateClick" in src
        assert "upsert_entry" in src and "delete_entry" in src
        assert "load_applied_markers" in src
        # Component re-render + replayed-callback quirks stay encoded:
        # remount via nonce key after every processed click, month pinned
        # via initialDate so the remount doesn't jump back to today.
        assert "ms_cal_nonce" in src
        assert "initialDate" in src
        # Conflict handling (conditional-write 412) surfaces, not clobbers.
        assert '"conflict"' in src or "'conflict'" in src

    def test_page_click_opens_modal_editor_with_regular_default(self):
        """Click-to-edit contract: a day click opens the st.dialog editor,
        whose mode choice includes 'regular' as the no-entry default (saving
        regular over an existing entry deletes it)."""
        src = (
            REPO_ROOT / "views" / "45_Morning_Signal_Schedule.py"
        ).read_text()
        assert "@st.dialog" in src
        assert "_edit_day_dialog" in src
        assert '"regular"' in src
        assert '"skip"' in src and '"override"' in src and '"extend"' in src
        # Regular-over-existing removes the entry via delete_entry.
        assert "delete_entry(date_str)" in src

    def test_page_guards_missing_component_import(self):
        src = (
            REPO_ROOT / "views" / "45_Morning_Signal_Schedule.py"
        ).read_text()
        assert "except ImportError" in src
        assert "st.stop()" in src

    def test_page_shows_llm_decision_badge(self):
        """config#1659: which model aired each day, additive to the
        schedule calendar (most days have no schedule entry at all)."""
        src = (
            REPO_ROOT / "views" / "45_Morning_Signal_Schedule.py"
        ).read_text()
        assert "load_llm_decisions" in src
        assert "_llm_events" in src
        assert "fell_back" in src
        assert "Recent model usage" in src
