"""Decision Queue page contracts (config#1926): pinned deep-link slug,
home-chip wiring, Ask-block parser, and write-payload purity.

1. **Slug contract.** ``app.py`` MUST register ``views/49_Decision_Queue.py``
   as a standalone ``st.Page`` with ``url_path="decision-queue"`` — the
   weekly Telegram digest (config#1922) deep-links to it. Mirrors
   ``tests/test_fleet_status_page.py``.

2. **Home-chip contract.** ``app.py`` home renders the decision-queue chip.

3. **Parser.** ``parse_ask_block`` extracts the config#1923 Ask contract;
   unframed comments degrade to (None, [], None), never raise.

4. **Write helpers.** ``bump_reexam_line`` / ``ruling_comment`` are pure and
   deterministic — the ruling comment must be self-contained enough for the
   next tier groom's executor (self-contained-issue rule).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from loaders.decision_queue_loader import (  # noqa: E402
    BACKLOG_REPOS,
    HUMAN_GATE_LABELS,
    bump_reexam_line,
    parse_ask_block,
    ruling_comment,
)

EXPECTED_SLUG = "decision-queue"
PAGE = REPO_ROOT / "views" / "49_Decision_Queue.py"


class TestSlugContract:
    def test_page_file_exists(self):
        assert PAGE.exists()

    def test_app_pins_decision_queue_url_path(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert f'url_path="{EXPECTED_SLUG}"' in app_src
        assert "views/49_Decision_Queue.py" in app_src

    def test_home_renders_chip(self):
        app_src = (REPO_ROOT / "app.py").read_text()
        assert "_render_decision_queue_chip()" in app_src


class TestScopeContract:
    def test_all_four_backlog_repos_enumerated(self):
        assert BACKLOG_REPOS == [
            "nousergon/alpha-engine-config",
            "nousergon/metron-ops",
            "nousergon/vires-ops",
            "nousergon/telos-ops",
        ]

    def test_only_human_gates(self):
        assert set(HUMAN_GATE_LABELS) == {"gate:operator", "gate:decision"}

    def test_loader_never_shells_to_gh_for_api_calls(self):
        # Proxy-TLS constraint: GitHub via urllib only. `gh auth token` (local
        # dev fallback) is the sole permitted subprocess use.
        src = (REPO_ROOT / "loaders" / "decision_queue_loader.py").read_text()
        assert "api.github.com" in src
        assert src.count("subprocess.run") == 1
        assert '["gh", "auth", "token"]' in src


class TestAskParser:
    FRAMED = (
        "**Ask:** Should low-stakes gated decisions auto-apply a default?\n"
        "**Options:** A) Yes, as scoped (recommended) B) Notification-only soak "
        "C) No — triage ritual only\n"
        "**Consequence of no action:** pool re-accumulates.\n"
    )

    def test_framed_comment_parses(self):
        ask, options, rec = parse_ask_block(self.FRAMED)
        assert ask == "Should low-stakes gated decisions auto-apply a default?"
        assert [l for l, _ in options] == ["A", "B", "C"]
        assert rec == "A"

    def test_recommended_detection_is_case_insensitive(self):
        ask, options, rec = parse_ask_block(
            "**Ask:** x?\n**Options:** A) no B) yes (Recommended)\n"
        )
        assert rec == "B"

    def test_unframed_comment_degrades(self):
        assert parse_ask_block("just a status update, no structure") == (None, [], None)

    def test_empty_and_none_safe(self):
        assert parse_ask_block("") == (None, [], None)


class TestWriteHelpers:
    def test_bump_replaces_existing_reexam_line(self):
        body = "Some text\n\nRe-exam: 2026-07-01\n\ntail"
        out = bump_reexam_line(body, "2026-08-01")
        assert "Re-exam: 2026-08-01" in out
        assert "Re-exam: 2026-07-01" not in out
        assert out.count("Re-exam:") == 1

    def test_bump_appends_when_absent(self):
        out = bump_reexam_line("no date here", "2026-08-01")
        assert out.rstrip().endswith("Re-exam: 2026-08-01")

    def test_ruling_comment_is_self_contained(self):
        c = ruling_comment("Option B", "use the forum supergroup", "2026-07-07")
        assert "Operator decision 2026-07-07: Option B" in c
        assert "use the forum supergroup" in c
        assert "config#1926" in c  # provenance for the executing groom


class TestNewestCommentOrdering:
    def test_scans_ascending_pages_newest_first(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        # Ascending API order (as GitHub actually returns): old -> new.
        comments = [
            {"body": "old status note", "created_at": "2026-06-01T00:00:00Z"},
            {"body": "**Ask:** stale old ask\n**Options:** A) x (recommended)",
             "created_at": "2026-06-10T00:00:00Z"},
            {"body": "**Ask:** the CURRENT ask\n**Options:** A) y (recommended)",
             "created_at": "2026-07-07T00:00:00Z"},
        ]
        monkeypatch.setattr(dq, "_request", lambda m, u: comments)
        out = dq._newest_gate_comment("nousergon/alpha-engine-config", 1)
        assert "CURRENT ask" in out  # newest Ask wins, not the June one

    def test_no_ask_falls_back_to_newest_comment(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        comments = [{"body": "first", "created_at": "2026-06-01T00:00:00Z"},
                    {"body": "latest status", "created_at": "2026-07-07T00:00:00Z"}]
        monkeypatch.setattr(dq, "_request", lambda m, u: comments)
        assert dq._newest_gate_comment("r/r", 1) == "latest status"
