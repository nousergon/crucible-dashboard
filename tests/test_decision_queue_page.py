"""Decision Queue page contracts (config#1926): pinned deep-link slug,
home-chip wiring, Ask-block parser, and write-payload purity.

1. **Slug contract.** ``app.py`` MUST register ``views/49_Decision_Queue.py``
   as a standalone ``st.Page`` with ``url_path="decision-queue"`` — the
   weekly Telegram digest (config#1922) deep-links to it. Mirrors
   ``tests/test_fleet_status_page.py``.

2. **Home-chip contract.** ``app.py`` home renders the decision-queue chip.

3. **Parser.** ``parse_ask_block`` extracts the config#1923 Ask contract;
   unframed comments degrade to (None, [], None), never raise.
   ``parse_context_block`` extracts the config#1923 Summary/SOTA/Delta
   extension — each field independent, absent fields degrade to None.

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
    defer_issue,
    kill_issue,
    parse_ask_block,
    parse_context_block,
    post_ruling,
    reexam_snoozed_until,
    ruling_comment,
    send_to_session,
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


class TestContextParser:
    """config#1923 extension: **Summary:** / **SOTA:** / **Delta:** — each
    independent of the others and of the Ask/Options block, so an older
    3-line comment (pre-extension) or a partially-backfilled one degrades
    field-by-field rather than all-or-nothing."""

    FULL = (
        "**Summary:** groom PAT lacks ssm:GetParameter on the new prefix.\n"
        "**Ask:** Should we widen the role or split the prefix?\n"
        "**Options:** A) Widen role (recommended) B) Split prefix\n"
        "**SOTA:** least-privilege — split the prefix and scope a new policy\n"
        "**Delta:** recommended option widens an existing grant instead — "
        "faster, small added blast radius\n"
        "**Consequence of no action:** groom stays blocked.\n"
    )

    def test_full_block_parses_all_three_fields(self):
        summary, sota, delta = parse_context_block(self.FULL)
        assert summary == "groom PAT lacks ssm:GetParameter on the new prefix."
        assert sota == "least-privilege — split the prefix and scope a new policy"
        assert delta.startswith("recommended option widens an existing grant")

    def test_legacy_three_line_block_degrades_to_none(self):
        legacy = (
            "**Ask:** Should low-stakes gated decisions auto-apply a default?\n"
            "**Options:** A) Yes, as scoped (recommended) B) No\n"
            "**Consequence of no action:** pool re-accumulates.\n"
        )
        assert parse_context_block(legacy) == (None, None, None)

    def test_partial_backfill_only_missing_field_is_none(self):
        summary, sota, delta = parse_context_block(
            "**Summary:** context here.\n**Ask:** x?\n**Options:** A) y (recommended)\n"
        )
        assert summary == "context here."
        assert sota is None
        assert delta is None

    def test_empty_and_none_safe(self):
        assert parse_context_block("") == (None, None, None)
        assert parse_context_block(None) == (None, None, None)


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


class TestSubmitLatencyFix:
    """Perf fix: a ruling used to clear_queue_cache() then the view's
    st.rerun() forced a full serial re-fetch of every OTHER pending issue —
    the reported ~10s "sits there" stall on every single submit. The view's
    ``dq_done`` session-state guard already hides the acted-on item on that
    same rerun with no network round trip, so write actions must not
    invalidate the cache themselves; only the explicit Refresh button (and
    the 300s TTL) should."""

    @staticmethod
    def _patch_request(monkeypatch):
        import loaders.decision_queue_loader as dq
        calls: list[tuple] = []

        def fake(method, url, payload=None):
            calls.append((method, url, payload))
            return {"body": "existing body"} if method == "GET" else {}

        monkeypatch.setattr(dq, "_request", fake)
        return calls

    def test_post_ruling_does_not_clear_cache(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        self._patch_request(monkeypatch)
        cleared = []
        monkeypatch.setattr(dq, "clear_queue_cache", lambda: cleared.append(True))
        post_ruling("nousergon/alpha-engine-config", 1, "Option A")
        assert cleared == []

    def test_kill_issue_does_not_clear_cache(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        self._patch_request(monkeypatch)
        cleared = []
        monkeypatch.setattr(dq, "clear_queue_cache", lambda: cleared.append(True))
        kill_issue("nousergon/alpha-engine-config", 1)
        assert cleared == []

    def test_send_to_session_does_not_clear_cache(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        self._patch_request(monkeypatch)
        cleared = []
        monkeypatch.setattr(dq, "clear_queue_cache", lambda: cleared.append(True))
        send_to_session("nousergon/alpha-engine-config", 1)
        assert cleared == []

    def test_defer_issue_skips_redundant_get_when_body_supplied(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        calls = self._patch_request(monkeypatch)
        cleared = []
        monkeypatch.setattr(dq, "clear_queue_cache", lambda: cleared.append(True))
        defer_issue("nousergon/alpha-engine-config", 1, "2026-08-01", body="already-loaded body")
        assert cleared == []
        assert not any(m == "GET" for m, _u, _p in calls)

    def test_defer_issue_falls_back_to_get_when_body_missing(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        calls = self._patch_request(monkeypatch)
        monkeypatch.setattr(dq, "clear_queue_cache", lambda: None)
        defer_issue("nousergon/alpha-engine-config", 1, "2026-08-01")
        assert any(m == "GET" for m, _u, _p in calls)


class TestLoadQueueFanout:
    """load_decision_queue() used to fetch the per-issue gate comment
    SERIALLY, one blocking GET per pending issue across 4 repos x 2 labels —
    the O(N) round trips that made the page (and post-ruling reload) slow.
    Comment lookups must be fanned out concurrently, and an issue carrying
    BOTH human-gate labels must be deduped BEFORE the fetch, not after."""

    def test_uses_thread_pool_for_comment_fanout(self):
        src = (REPO_ROOT / "loaders" / "decision_queue_loader.py").read_text()
        assert "ThreadPoolExecutor" in src

    def test_shared_issue_across_both_gates_fetches_comment_once(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        shared_issue = {
            "number": 42, "title": "shared",
            "created_at": "2026-07-01T00:00:00Z", "html_url": "http://x", "body": "b",
        }
        monkeypatch.setattr(dq, "BACKLOG_REPOS", ["nousergon/alpha-engine-config"])
        monkeypatch.setattr(dq, "_list_gated_issues", lambda repo, label: [shared_issue])
        comment_calls: list[tuple] = []

        def fake_newest(repo, number):
            comment_calls.append((repo, number))
            return "no ask here"

        monkeypatch.setattr(dq, "_newest_gate_comment", fake_newest)
        getattr(dq.load_decision_queue, "clear", lambda: None)()  # real st caches; conftest mock doesn't
        out = dq.load_decision_queue()
        assert len(comment_calls) == 1  # deduped before the network fan-out
        assert len(out["items"]) == 1


class TestDeferSnooze:
    """The Defer-2w contract (the 2026-07-08 repopulation bug): ``defer_issue``
    bumps the ``Re-exam:`` line and leaves the gate label standing, so the
    loader — not the session-local ``dq_done`` guard, which dies with the
    browser session — must exclude issues whose Re-exam date is in the
    future. A deferred issue re-entering the queue on every reload made the
    button a no-op across sessions."""

    from datetime import date as _date

    TODAY = _date(2026, 7, 8)

    def test_future_reexam_is_snoozed(self):
        assert reexam_snoozed_until("x\n\nRe-exam: 2026-07-22\n", self.TODAY) == "2026-07-22"

    def test_due_today_is_not_snoozed(self):
        assert reexam_snoozed_until("Re-exam: 2026-07-08\n", self.TODAY) is None

    def test_past_reexam_is_not_snoozed(self):
        assert reexam_snoozed_until("Re-exam: 2026-06-01\n", self.TODAY) is None

    def test_absent_line_is_not_snoozed(self):
        assert reexam_snoozed_until("no date here", self.TODAY) is None
        assert reexam_snoozed_until("", self.TODAY) is None

    def test_malformed_date_fails_open(self):
        # Shape-valid but impossible date: the issue must stay VISIBLE —
        # silently hiding an item behind a typo is the worse failure mode.
        assert reexam_snoozed_until("Re-exam: 2026-02-31\n", self.TODAY) is None

    def test_defer_roundtrip_survives_reload(self):
        # The exact reported bug path: what the Defer button writes must be
        # what the loader recognizes as snoozed on the next (re)load.
        body = bump_reexam_line("original body", "2026-07-22")
        assert reexam_snoozed_until(body, self.TODAY) == "2026-07-22"

    def test_loader_splits_due_vs_snoozed_before_comment_fanout(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        issues = [
            {"number": 1, "title": "deferred", "created_at": "2026-06-01T00:00:00Z",
             "html_url": "http://a", "body": "Re-exam: 2099-01-01\n"},
            {"number": 2, "title": "due", "created_at": "2026-06-01T00:00:00Z",
             "html_url": "http://b", "body": "Re-exam: 2020-01-01\n"},
        ]
        monkeypatch.setattr(dq, "BACKLOG_REPOS", ["nousergon/alpha-engine-config"])
        monkeypatch.setattr(dq, "HUMAN_GATE_LABELS", ("gate:decision",))
        monkeypatch.setattr(dq, "_list_gated_issues", lambda repo, label: issues)
        comment_calls: list[int] = []

        def fake_newest(repo, number):
            comment_calls.append(number)
            return "no ask"

        monkeypatch.setattr(dq, "_newest_gate_comment", fake_newest)
        getattr(dq.load_decision_queue, "clear", lambda: None)()  # real st caches; conftest mock doesn't
        out = dq.load_decision_queue()
        assert [i["number"] for i in out["items"]] == [2]
        assert [s["key"] for s in out["snoozed"]] == ["nousergon/alpha-engine-config#1"]
        assert out["snoozed"][0]["until"] == "2099-01-01"
        assert comment_calls == [2]  # no network spent on the snoozed item


class TestBuildDecisionItemContext:
    """``_build_decision_item`` must wire ``parse_context_block`` output into
    the item exactly like it wires ``parse_ask_block`` — the console can only
    render Summary/SOTA/Delta if the builder actually populates them."""

    def test_full_block_populates_all_three_fields(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        from datetime import datetime, timezone

        monkeypatch.setattr(
            dq, "_newest_gate_comment",
            lambda repo, number: TestContextParser.FULL,
        )
        it = {
            "number": 1, "title": "t", "created_at": "2026-07-01T00:00:00Z",
            "html_url": "http://x", "body": "",
        }
        item = dq._build_decision_item(
            "nousergon/alpha-engine-config", "gate:decision", it,
            datetime.now(timezone.utc),
        )
        assert item.summary == "groom PAT lacks ssm:GetParameter on the new prefix."
        assert item.sota.startswith("least-privilege")
        assert item.delta.startswith("recommended option widens")

    def test_unframed_comment_leaves_context_fields_none(self, monkeypatch):
        import loaders.decision_queue_loader as dq
        from datetime import datetime, timezone

        monkeypatch.setattr(dq, "_newest_gate_comment", lambda repo, number: "status update")
        it = {
            "number": 2, "title": "t", "created_at": "2026-07-01T00:00:00Z",
            "html_url": "http://x", "body": "",
        }
        item = dq._build_decision_item(
            "nousergon/alpha-engine-config", "gate:operator", it,
            datetime.now(timezone.utc),
        )
        assert (item.summary, item.sota, item.delta) == (None, None, None)


class TestCardRendersContext:
    """Source-text contract (mirrors ``TestScopeContract``'s style): the page
    must actually surface the three new fields, not just parse them — a
    populated ``item["sota"]``/``item["delta"]`` that never reaches
    ``st.markdown``/``st.caption`` would satisfy the parser tests while
    leaving the console showing nothing new to Brian."""

    def test_view_renders_summary_sota_and_delta(self):
        src = (REPO_ROOT / "views" / "49_Decision_Queue.py").read_text()
        assert 'item["summary"]' in src
        assert 'item["sota"]' in src
        assert 'item["delta"]' in src
