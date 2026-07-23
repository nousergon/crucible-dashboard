"""Fleet Status page contracts: pinned deep-link slug + render smoke +
loader condensation.

1. **Slug contract.** ``app.py`` MUST register ``views/48_Fleet_Status.py``
   as a standalone ``st.Page`` with ``url_path="fleet-status"`` — the home
   strip page_links to it and future notification deep-links will use the
   slug. Mirrors ``tests/test_pipeline_status_page.py``.

2. **Home-strip contract.** ``app.py`` home renders the fleet strip.

3. **Render smoke.** The page exec-loads with streamlit mocked and
   ``gather_fleet_inputs`` stubbed (no AWS / network) and renders every
   resolver group without raising.

4. **Loader condensation.** ``_pipeline_snapshots`` maps page-25 LoadResults
   onto PipelineSnapshot faithfully (RUNNING state name, UNAVAILABLE error
   carry-through, NO_EXECUTIONS passthrough).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

EXPECTED_SLUG = "fleet-status"
PAGE = REPO_ROOT / "views" / "48_Fleet_Status.py"


class TestSlugContract:
    def test_app_pins_fleet_status_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert f'url_path="{EXPECTED_SLUG}"' in app_src

    def test_pinned_page_is_the_fleet_status_view(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        # rindex: the view path also appears in the home-strip helper's
        # docstring/page_link; the st.Page registration is the LAST mention.
        idx = app_src.rindex("views/48_Fleet_Status.py")
        window = app_src[idx : idx + 300]
        assert f'url_path="{EXPECTED_SLUG}"' in window

    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_home_renders_fleet_strip(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert "def _render_fleet_strip" in app_src
        assert app_src.count("_render_fleet_strip()") >= 1


class TestDeepLinkTargets:
    """Every deep-link URL on the page must resolve to something real.

    st.page_link is FORBIDDEN for host-tab views: post nav-collapse
    (dashboard#273) most former pages are view-host TABS, not st.Page
    registrations, and page_link raises StreamlitPageNotFoundError in
    production for them (bit live 2026-07-06). Deep links are markdown
    page URLs instead; this guard pins each target's existence:
    - registered slugs must appear as a pinned url_path in app.py;
    - host?tab= targets must name a (label, view-file) pair that exists
      verbatim in the host page's subviews list.
    """

    def _url_map(self):
        import re

        src = PAGE.read_text()
        block = src[src.index("_URL_BY_SLUG") :]
        block = block[: block.index("}") + 1]
        return dict(re.findall(r'"([a-z-]+)":\s*"([^"]+)"', block))

    def test_page_uses_no_page_link(self):
        assert "st.page_link(" not in PAGE.read_text()

    def test_every_deep_link_slug_has_a_url(self):
        from fleet_status import FleetInputs, resolve_fleet

        urls = self._url_map()
        now = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
        statuses = resolve_fleet(FleetInputs(now=now, is_trading_day=True))
        for s in statuses:
            if s.deep_link:
                assert s.deep_link in urls, (
                    f"{s.component_id} deep_link {s.deep_link!r} missing from "
                    f"_URL_BY_SLUG"
                )

    def test_registered_slug_targets_exist_in_app_nav(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        for slug, url in self._url_map().items():
            if "?" not in url:
                assert f'url_path="{url}"' in app_src, (
                    f"deep link /{url} expects a registered st.Page with that "
                    f"pinned url_path in app.py"
                )

    def test_host_tab_targets_exist_in_host_source(self):
        from urllib.parse import parse_qs, unquote_plus, urlsplit

        for slug, url in self._url_map().items():
            if "?" not in url:
                continue
            parts = urlsplit(url)
            host_file = REPO_ROOT / "views" / f"{parts.path}.py"
            assert host_file.exists(), f"deep link /{url}: no {host_file.name}"
            tab = parse_qs(parts.query)["tab"][0]
            tab = unquote_plus(tab)
            assert f'"{tab}"' in host_file.read_text(), (
                f"deep link /{url}: tab label {tab!r} not found in "
                f"{host_file.name}'s subviews"
            )

    def _representative_inputs(self):
        """A FleetInputs fixture set spanning each resolver's branches.

        config#3207: ``ComponentStatus.deep_link`` is Optional with no
        chokepoint, so a new/changed resolver branch could silently ship
        without one. Mirrors ``tests/test_fleet_status.py``'s reference
        clocks + adds the probe-state toggles needed to walk daemon/
        trading_instance/groomer/module_self_reports through more than
        their single default branch.
        """
        from fleet_status import FleetInputs, GroomSnapshot, ModuleHealthRow

        trading_mid = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
        saturday = datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)
        sunday = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)
        return [
            FleetInputs(now=trading_mid, is_trading_day=True),
            FleetInputs(now=trading_mid, is_trading_day=True, ec2_available=False),
            FleetInputs(now=trading_mid, is_trading_day=True, intraday_nav_age_s=5.0),
            FleetInputs(now=trading_mid, is_trading_day=True, intraday_nav_age_s=9999.0),
            FleetInputs(now=trading_mid, is_trading_day=True, live_service_ok=True),
            FleetInputs(now=trading_mid, is_trading_day=True, live_service_ok=False),
            FleetInputs(now=saturday, is_trading_day=False),
            FleetInputs(now=sunday, is_trading_day=False),
            FleetInputs(
                now=trading_mid, is_trading_day=True,
                groom=GroomSnapshot(marker_started_at=trading_mid, marker_tier="mid"),
            ),
            FleetInputs(
                now=trading_mid, is_trading_day=True,
                module_health=(
                    ModuleHealthRow(module="predictor", status="failed", age_hrs=1.0,
                                     stale_after_hrs=None, error="boom"),
                ),
            ),
        ]

    def test_every_row_has_a_deep_link_or_a_documented_exemption(self):
        """Chokepoint (config#3207): every resolved row either deep_links to
        a real target, or its component_id is a documented exemption in
        ``fleet_status.NO_DEEP_LINK_TARGETS``. Neither can drift silently —
        an undocumented None fails here; a stale exemption entry whose row
        now HAS a deep_link fails in ``test_no_deep_link_targets_registry_
        is_not_stale`` below.
        """
        from fleet_status import NO_DEEP_LINK_TARGETS, resolve_fleet

        urls = self._url_map()
        for inputs in self._representative_inputs():
            for s in resolve_fleet(inputs):
                if s.component_id in NO_DEEP_LINK_TARGETS:
                    continue
                assert s.deep_link is not None, (
                    f"{s.component_id} carries deep_link=None and is not in "
                    f"NO_DEEP_LINK_TARGETS — either wire a deep_link or add a "
                    f"one-line documented exemption"
                )
                assert s.deep_link in urls, (
                    f"{s.component_id} deep_link {s.deep_link!r} missing from "
                    f"_URL_BY_SLUG"
                )

    def test_no_deep_link_targets_registry_is_not_stale(self):
        """Every NO_DEEP_LINK_TARGETS entry must still be a real component_id
        that actually produces deep_link=None across the fixture set —
        catches a row gaining a real deep_link without its exemption being
        removed (config#3207)."""
        from fleet_status import NO_DEEP_LINK_TARGETS, resolve_fleet

        seen_ids = set()
        for inputs in self._representative_inputs():
            for s in resolve_fleet(inputs):
                seen_ids.add(s.component_id)
                if s.component_id in NO_DEEP_LINK_TARGETS:
                    assert s.deep_link is None, (
                        f"{s.component_id} is listed in NO_DEEP_LINK_TARGETS "
                        f"but now carries deep_link={s.deep_link!r} — remove "
                        f"its exemption entry"
                    )
        missing = set(NO_DEEP_LINK_TARGETS) - seen_ids
        assert not missing, (
            f"NO_DEEP_LINK_TARGETS names component_id(s) resolve_fleet() "
            f"never produces: {sorted(missing)}"
        )


# ── Render smoke (exec-load with mocked streamlit + stubbed loader) ─────────


def _healthy_inputs():
    from fleet_status import FleetInputs, GroomSnapshot, PipelineSnapshot

    now = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
    snap = PipelineSnapshot(
        status="SUCCEEDED", verdict="COMPLETE",
        started_at=datetime(2026, 7, 7, 12, 45, tzinfo=timezone.utc),
        stopped_at=datetime(2026, 7, 7, 13, 30, tzinfo=timezone.utc),
    )
    return FleetInputs(
        now=now, is_trading_day=True,
        trading_instance_state="running", trading_instance_ping="Online",
        live_service_ok=True, intraday_nav_age_s=45.0,
        pipelines={"weekly": snap, "preopen": snap, "postclose": snap},
        heartbeat={"last_run": now.isoformat(), "alerts_enabled": True},
        check_results={"run_at": now.isoformat(), "results": [
            {"artifact_id": "a", "state": "fresh", "severity": "critical",
             "owner_repo": "r", "reason": ""}]},
        groom=GroomSnapshot(marker_started_at=now),
    )


@pytest.fixture
def rendered_page():
    """Exec-load the page; return the mock streamlit for assertions."""
    mock_st = MagicMock()
    mock_st.cache_data = lambda **kw: (lambda f: f)
    mock_st.cache_resource = lambda **kw: (lambda f: f)
    # st.fragment must pass the function through AND the page calls it at
    # top level — keep it a real passthrough so the grid renders in-test.
    mock_st.fragment = lambda **kw: (lambda f: f)
    cols = [MagicMock() for _ in range(5)]
    mock_st.columns = MagicMock(side_effect=lambda spec: cols[: len(spec)] if isinstance(spec, (list, tuple)) else cols[: int(spec)])

    import loaders.fleet_status_loader as fsl

    saved_st = sys.modules.get("streamlit")
    sys.modules["streamlit"] = mock_st
    saved_gather = fsl.gather_fleet_inputs
    fsl.gather_fleet_inputs = _healthy_inputs
    try:
        spec = importlib.util.spec_from_file_location("fleet_status_page", PAGE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        fsl.gather_fleet_inputs = saved_gather
        if saved_st is not None:
            sys.modules["streamlit"] = saved_st
    return mock_st


class TestRenderSmoke:
    def test_page_renders_without_raising(self, rendered_page):
        rendered_page.title.assert_called_once()

    def test_all_groups_rendered(self, rendered_page):
        subheaders = [c.args[0] for c in rendered_page.subheader.call_args_list]
        from fleet_status import GROUP_ORDER

        for group in GROUP_ORDER:
            assert group in subheaders

    def test_no_degraded_banner_when_healthy(self, rendered_page):
        rendered_page.warning.assert_not_called()


# ── Loader condensation ─────────────────────────────────────────────────────


class TestPipelineSnapshots:
    def _load_result(self, **kw):
        from loaders.pipeline_status_loader import LoadOutcome, LoadResult

        defaults = dict(arn="arn:x", outcome=LoadOutcome.LIVE, run=None,
                        error_message=None)
        defaults.update(kw)
        return LoadResult(**defaults)

    def test_running_maps_current_state(self, monkeypatch):
        from nousergon_lib.pipeline_status import PipelineRun

        run = PipelineRun.model_validate({
            "state_machine_arn": "arn:x",
            "pretty_label": "Weekly Freshness SF",
            "execution_arn": "arn:e",
            "execution_name": "e1",
            "status": "RUNNING",
            "start_utc": "2026-07-07T12:45:00Z",
            "tasks": [
                {"state_name": "MorningEnrich", "status": "SUCCEEDED",
                 "archive": {"kind": "artifact_reason", "reason": "substrate"}},
                {"state_name": "RunMorningPlanner", "status": "RUNNING",
                 "archive": {"kind": "artifact_reason", "reason": "substrate"}},
            ],
        })
        import loaders.fleet_status_loader as fsl

        monkeypatch.setattr(
            fsl, "read_pipeline_state_with_fallback",
            lambda arn, role_filter=None: self._load_result(run=run),
        )
        snaps = fsl._pipeline_snapshots()
        assert snaps["preopen"].status == "RUNNING"
        assert snaps["preopen"].current_state == "RunMorningPlanner"

    def test_unavailable_carries_error(self, monkeypatch):
        from loaders.pipeline_status_loader import LoadOutcome

        import loaders.fleet_status_loader as fsl

        monkeypatch.setattr(
            fsl, "read_pipeline_state_with_fallback",
            lambda arn, role_filter=None: self._load_result(
                outcome=LoadOutcome.ERROR, error_message="SFN throttled — x"),
        )
        snaps = fsl._pipeline_snapshots()
        assert snaps["weekly"].status == "UNAVAILABLE"
        assert "throttled" in snaps["weekly"].error

    def test_no_executions_passthrough(self, monkeypatch):
        from loaders.pipeline_status_loader import LoadOutcome

        import loaders.fleet_status_loader as fsl

        monkeypatch.setattr(
            fsl, "read_pipeline_state_with_fallback",
            lambda arn, role_filter=None: self._load_result(
                outcome=LoadOutcome.NO_EXECUTIONS,
                error_message="no executions"),
        )
        snaps = fsl._pipeline_snapshots()
        assert snaps["postclose"].status == "NO_EXECUTIONS"

    def test_role_filter_unions_cadence_and_recovery_roles(self, monkeypatch):
        """config#3085: the role_filter passed to read_pipeline_state_with_
        fallback must include the cadence role AND every recovery role, so
        the loader's newest-first walk can resolve to a running/succeeded
        recovery overlay instead of pinning to a stale scheduled failure.
        Smoke/shell/backfill roles must NOT be in the filter (Option-D)."""
        from fleet_status import RECOVERY_PIPELINE_ROLES

        import loaders.fleet_status_loader as fsl

        seen_filters = {}

        def _fake(arn, role_filter=None):
            seen_filters[arn] = role_filter
            return self._load_result()

        monkeypatch.setattr(fsl, "read_pipeline_state_with_fallback", _fake)
        fsl._pipeline_snapshots()
        assert len(seen_filters) == 3
        for arn, role_filter in seen_filters.items():
            assert RECOVERY_PIPELINE_ROLES <= role_filter
            assert "smoke" not in role_filter
            assert "shell-run" not in role_filter

    def test_role_passthrough_for_recovery_completion(self, monkeypatch):
        from nousergon_lib.pipeline_status import PipelineRun

        run = PipelineRun.model_validate({
            "state_machine_arn": "arn:x",
            "pretty_label": "Weekly Freshness SF",
            "execution_arn": "arn:e",
            "execution_name": "e1",
            "status": "SUCCEEDED",
            "start_utc": "2026-07-20T12:45:00Z",
            "end_utc": "2026-07-20T13:45:00Z",
            "pipeline_role": "watch-rerun",
            "tasks": [],
        })
        import loaders.fleet_status_loader as fsl

        monkeypatch.setattr(
            fsl, "read_pipeline_state_with_fallback",
            lambda arn, role_filter=None: self._load_result(run=run),
        )
        snaps = fsl._pipeline_snapshots()
        assert snaps["weekly"].role == "watch-rerun"


class TestWatchDispatchAlerts:
    """_watch_dispatch_alert's title-matching (the only non-S3-passthrough
    logic in the SF/CI Watch snapshot loaders) — monkeypatches the cached
    _open_watch_dispatch_issues directly so no network/AWS/cache-runtime
    dependency is exercised."""

    def test_matches_sf_watch_title(self, monkeypatch):
        import loaders.fleet_status_loader as fsl

        monkeypatch.setattr(fsl, "_open_watch_dispatch_issues", lambda: [
            {"title": "SF-watch dispatch failed to launch for "
                      "ne-weekly-freshness-pipeline (2026-07-11)"},
        ])
        assert fsl._watch_dispatch_alert(fsl._SF_WATCH_ALERT_TITLE) is not None
        assert fsl._watch_dispatch_alert(fsl._CI_WATCH_ALERT_TITLE) is None

    def test_no_match_returns_none(self, monkeypatch):
        import loaders.fleet_status_loader as fsl

        monkeypatch.setattr(
            fsl, "_open_watch_dispatch_issues",
            lambda: [{"title": "some unrelated P1 issue"}],
        )
        assert fsl._watch_dispatch_alert(fsl._SF_WATCH_ALERT_TITLE) is None
        assert fsl._watch_dispatch_alert(fsl._CI_WATCH_ALERT_TITLE) is None

    def test_no_open_issues_returns_none(self, monkeypatch):
        import loaders.fleet_status_loader as fsl

        monkeypatch.setattr(fsl, "_open_watch_dispatch_issues", lambda: [])
        assert fsl._watch_dispatch_alert(fsl._SF_WATCH_ALERT_TITLE) is None
