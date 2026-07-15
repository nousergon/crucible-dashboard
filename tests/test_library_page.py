"""Tests for the Library page (config#2588) — private-docs system-doc
corpus browsing (SYSTEM_STATE.md + system_state/*.md, ARCHITECTURE.md,
EXPERIMENTS.md, STATUS_GENERATED.md) + its loader.

Mirrors test_registry_page_targets.py / test_console_ia_phase2a.py: nav
wiring is asserted against source text (page modules are not imported —
their module-level Streamlit calls need a live runtime); the loader itself
IS imported and exercised directly against a tmp_path fixture standing in
for the 4-tier path-resolution env-var override tier (same pattern
test_observation_registry_loader.py would use, if present — mirrors
observation_registry_loader.py's own precedent 1:1).

Pipeline-diagrams tab (PIPELINE_DIAGRAMS_GENERATED.md, alpha-engine-config
I2587) is explicitly NOT covered here — I2587 hasn't shipped the generated
file yet; fast-follow once it does.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loaders import system_docs_loader  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
VIEWS = REPO_ROOT / "views"


# ---------------------------------------------------------------------------
# Nav wiring — source-text pins (app.py / host_reference.py need a live
# Streamlit runtime to import).
# ---------------------------------------------------------------------------


class TestNavRenamedToLibrary:
    def test_app_nav_section_renamed(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert '"📚 Library": [' in app_src
        # The old nav-section key itself is gone (a history comment
        # mentioning the retired label in prose is fine and expected).
        assert '"📚 Reference": [' not in app_src

    def test_host_reference_file_and_key_survive_for_deep_links(self):
        # Rename is nav-label-only — host_reference.py/host_reference key
        # are unchanged so any future deep-link stays stable.
        app_src = (REPO_ROOT / "app.py").read_text()
        assert 'page("host_reference.py", "Library", "📚")' in app_src
        host_src = (VIEWS / "host_reference.py").read_text()
        assert 'key="host_reference"' in host_src


class TestLibraryTabsRegistered:
    def test_host_reference_lists_all_new_tabs(self):
        host_src = (VIEWS / "host_reference.py").read_text()
        for label, filename in (
            ("System State", "50_System_State.py"),
            ("Architecture Doc", "51_Architecture_Doc.py"),
            ("Experiments Log", "52_Experiments_Log.py"),
            ("Generated Status", "53_Status_Generated.py"),
        ):
            assert f'("{label}", "{filename}")' in host_src, (
                f"host_reference.py must register the {label!r} tab"
            )

    def test_original_reference_tabs_preserved(self):
        # The rename/extension must not drop the pre-existing trio.
        host_src = (VIEWS / "host_reference.py").read_text()
        for label, filename in (
            ("Architecture", "10_Architecture.py"),
            ("Signal Lifecycle", "11_Signal_Lifecycle.py"),
            ("RAG Inventory", "14_RAG_Inventory.py"),
        ):
            assert f'("{label}", "{filename}")' in host_src

    def test_all_tab_view_files_exist(self):
        for filename in (
            "50_System_State.py",
            "51_Architecture_Doc.py",
            "52_Experiments_Log.py",
            "53_Status_Generated.py",
        ):
            assert (VIEWS / filename).exists(), f"views/{filename} is missing"

    def test_pipeline_diagrams_tab_not_yet_added(self):
        # I2587 (PIPELINE_DIAGRAMS_GENERATED.md) hasn't shipped — no tab
        # *file* wires it up yet (a fast-follow once I2587 ships). A history
        # comment noting the deferral is fine and expected.
        host_src = (VIEWS / "host_reference.py").read_text()
        assert "Pipeline Diagrams" not in host_src
        assert not (VIEWS / "54_Pipeline_Diagrams.py").exists()

    def test_no_extra_tabs_beyond_the_scoped_set(self):
        host_src = (VIEWS / "host_reference.py").read_text()
        tab_files = set(__import__("re").findall(r'\(\s*"[^"]+",\s*"([^"]+\.py)"\s*\)', host_src))
        assert tab_files == {
            "10_Architecture.py", "11_Signal_Lifecycle.py", "14_RAG_Inventory.py",
            "50_System_State.py", "51_Architecture_Doc.py",
            "52_Experiments_Log.py", "53_Status_Generated.py",
        }


class TestNoDuplicateRegistrySurface:
    """Registries already have dedicated pages under Observability — Library
    must cross-link, not duplicate, their rendering logic (issue's explicit
    non-inferable-gotcha)."""

    def test_library_tabs_do_not_reimplement_registry_loaders(self):
        for filename in (
            "50_System_State.py",
            "51_Architecture_Doc.py",
            "52_Experiments_Log.py",
            "53_Status_Generated.py",
        ):
            src = (VIEWS / filename).read_text()
            # No tab imports the registry loader or a YAML parser directly
            # — a prose/caption mention of the registry filenames (for the
            # cross-link) is fine, an actual reimplementation is not.
            assert "load_observation_registry" not in src
            assert "import yaml" not in src

    def test_system_state_tab_cross_links_existing_registry_pages(self):
        src = (VIEWS / "50_System_State.py").read_text()
        assert "host_observability?tab=Artifact+Freshness" in src
        assert "host_observability?tab=Active+Observations" in src


# ---------------------------------------------------------------------------
# Loader — 4-tier path resolution (mirrors observation_registry_loader.py).
# ---------------------------------------------------------------------------


class TestSystemDocsLoaderPathResolution:
    def test_env_override_tier_wins(self, tmp_path, monkeypatch):
        (tmp_path / "ARCHITECTURE.md").write_text("# hello architecture\n")
        monkeypatch.setenv("SYSTEM_DOCS_ROOT", str(tmp_path))
        doc = system_docs_loader.load_doc("ARCHITECTURE.md")
        assert doc is not None
        assert doc["content"] == "# hello architecture\n"
        assert doc["source_path"] == str(tmp_path / "ARCHITECTURE.md")

    def test_nested_system_state_file_resolves_under_env_root(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "system_state"
        state_dir.mkdir()
        (state_dir / "executor.md").write_text("# executor axis\n")
        monkeypatch.setenv("SYSTEM_DOCS_ROOT", str(tmp_path))
        doc = system_docs_loader.load_system_state_file("executor.md")
        assert doc is not None
        assert doc["content"] == "# executor axis\n"

    def test_returns_none_when_unresolvable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYSTEM_DOCS_ROOT", str(tmp_path))  # empty dir
        assert system_docs_loader.load_doc("DOES_NOT_EXIST.md") is None

    def test_returns_none_on_undecodable_bytes_instead_of_raising(self, tmp_path, monkeypatch):
        # A non-UTF-8 byte in a generated doc must degrade to the same
        # "not reachable" panel as a missing file, not an unhandled
        # traceback — UnicodeDecodeError is a ValueError subclass, not an
        # OSError, so this needs its own except clause. Pin _candidate_roots
        # to exactly tmp_path (not just the env-var tier) — this box may
        # have a real alpha-engine-config checkout at the tier-2 EC2 path
        # that would otherwise mask the bad tmp file with a real, valid one.
        bad = tmp_path / "STATUS_GENERATED.md"
        bad.write_bytes(b"\xff\xfe not valid utf-8")
        monkeypatch.setattr(system_docs_loader, "_candidate_roots", lambda: [tmp_path])
        assert system_docs_loader.load_doc("STATUS_GENERATED.md") is None

    def test_candidate_roots_order_matches_observation_registry_loader_pattern(self, monkeypatch):
        # Same 4-tier order: env override, EC2 boot-pull path, ~/Development
        # sibling, repo-sibling-relative fallback.
        monkeypatch.setenv("SYSTEM_DOCS_ROOT", "/tmp/env-override")
        roots = [str(p) for p in system_docs_loader._candidate_roots()]
        assert roots[0] == "/tmp/env-override"
        assert roots[1] == "/home/ec2-user/alpha-engine-config/private-docs"
        assert roots[2].endswith("Development/alpha-engine-config/private-docs")
        assert roots[3].endswith("alpha-engine-config/private-docs")

    def test_env_override_tier_is_absent_when_unset(self, monkeypatch):
        monkeypatch.delenv("SYSTEM_DOCS_ROOT", raising=False)
        roots = [str(p) for p in system_docs_loader._candidate_roots()]
        assert roots[0] == "/home/ec2-user/alpha-engine-config/private-docs"


class TestSystemDocsLoaderConvenienceFns:
    def test_load_system_state_index(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYSTEM_DOCS_ROOT", str(tmp_path))
        (tmp_path / "SYSTEM_STATE.md").write_text("index content")
        doc = system_docs_loader.load_system_state_index()
        assert doc["content"] == "index content"

    def test_system_state_files_map_covers_every_fleet_repo(self):
        # One entry per repo axis SYSTEM_STATE.md's own index table lists,
        # plus the two cross-repo files.
        expected_filenames = {
            "cross_repo_invariants.md", "cross_repo_inflight.md",
            "executor.md", "research.md", "predictor.md", "backtester.md",
            "dashboard.md", "data.md", "evaluator.md", "lib.md",
            "config.md", "docs.md",
        }
        assert set(system_docs_loader.SYSTEM_STATE_FILES.values()) == expected_filenames


class TestRenderDocTab:
    """``render_doc_tab`` is the shared render used by the three single-file
    tabs (Architecture Doc / Experiments Log / Generated Status) — this is
    what actually exercises ``st.markdown``/``st.warning``, now that those
    view scripts are thin 3-line specs calling into it."""

    def test_renders_markdown_when_doc_resolves(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYSTEM_DOCS_ROOT", str(tmp_path))
        (tmp_path / "ARCHITECTURE.md").write_text("# arch content")
        # Assert against the mock instance the module actually bound at
        # import time (``system_docs_loader.st``), not whatever
        # sys.modules["streamlit"] currently holds — another test file
        # earlier in the run may have swapped in a *different* MagicMock,
        # which would silently desync the two and make every assertion here
        # a false negative.
        mock_st = system_docs_loader.st
        mock_st.reset_mock()
        system_docs_loader.render_doc_tab(
            system_docs_loader.ARCHITECTURE_DOC, title="Architecture", caption="cap"
        )
        mock_st.markdown.assert_called_with("# arch content")
        mock_st.warning.assert_not_called()

    def test_warns_when_doc_unresolvable(self, tmp_path, monkeypatch):
        # Isolate from the EC2/local-dev/sibling fallback tiers entirely (an
        # empty SYSTEM_DOCS_ROOT alone isn't enough — this box may have a
        # real alpha-engine-config checkout at the tier-2 EC2 path, which
        # would silently resolve EXPERIMENTS.md and mask the "unresolvable"
        # case this test means to cover).
        monkeypatch.setattr(system_docs_loader, "_candidate_roots", lambda: [tmp_path])
        # Assert against the mock instance the module actually bound at
        # import time (``system_docs_loader.st``), not whatever
        # sys.modules["streamlit"] currently holds — another test file
        # earlier in the run may have swapped in a *different* MagicMock,
        # which would silently desync the two and make every assertion here
        # a false negative.
        mock_st = system_docs_loader.st
        mock_st.reset_mock()
        system_docs_loader.render_doc_tab(
            system_docs_loader.EXPERIMENTS_DOC, title="Experiments Log", caption="cap"
        )
        assert mock_st.warning.called
        (msg,), _ = mock_st.warning.call_args
        assert "EXPERIMENTS.md" in msg
