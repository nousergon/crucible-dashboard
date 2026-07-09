"""Stale operational copy (config#1984 item 5) + vendored mermaid.js
(config#1984 item 6).

Item 5a: Saturday SF Watch's "OBSERVE mode (M1)" framing predates full
autonomy (shipped 2026-07-07, all 4 dispatch flags true).
Item 5b: Backlog Groom's "2x/day Sonnet + 1x/day Opus" cadence predates the
2026-07-07 move to demand-driven dispatch.
Item 5c: RAG Inventory's "awaiting first manifest 2026-05-09" empty-state
copy is dead weight well past that date (today 2026-07-09).
Item 6: views/10_Architecture.py loaded mermaid.js from the jsdelivr CDN —
vendored into assets/ instead (breaks offline/strict-CSP otherwise). Note:
as of this branch, views/12_Feedback_Loop.py (renamed
views/Crucible_Feedback.py by prior IA work) does not reference mermaid at
all — views/10_Architecture.py is the only callsite.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


class TestSaturdaySfWatchFullAutonomyCopy:
    SRC = (REPO_ROOT / "views" / "37_Saturday_SF_Watch.py").read_text()

    def test_observe_mode_m1_docstring_framing_removed(self):
        assert "**OBSERVE mode** (M1)" not in self.SRC

    def test_docstring_mentions_full_autonomy(self):
        assert "Full autonomy shipped 2026-07-07" in self.SRC

    def test_mode_fallback_label_no_longer_claims_observe_only_m1(self):
        assert '"OBSERVE only (M1)"' not in self.SRC

    def test_m2_not_landed_caption_removed(self):
        assert "Until the agent half lands (M2)" not in self.SRC


class TestBacklogGroomDemandDrivenCopy:
    SRC = (REPO_ROOT / "views" / "42_Backlog_Groom.py").read_text()

    def test_stale_fixed_cadence_claim_removed(self):
        assert "2x/day Sonnet mid/low-tier + 1x/day Opus" not in self.SRC

    def test_demand_driven_dispatch_described(self):
        assert "demand-driven dispatch" in self.SRC


class TestRagInventoryStaleEmptyState:
    SRC = (REPO_ROOT / "views" / "14_RAG_Inventory.py").read_text()

    def test_stale_first_manifest_date_removed(self):
        assert "2026-05-09" not in self.SRC

    def test_generic_no_manifest_copy_present(self):
        assert "No RAG manifest found" in self.SRC


class TestMermaidVendored:
    ARCH_SRC = (REPO_ROOT / "views" / "10_Architecture.py").read_text()
    ASSET_PATH = REPO_ROOT / "assets" / "mermaid-10.9.6.min.js"

    def test_cdn_url_no_longer_referenced(self):
        assert "cdn.jsdelivr.net" not in self.ARCH_SRC

    def test_vendored_asset_exists(self):
        assert self.ASSET_PATH.exists()

    def test_vendored_asset_is_the_umd_build(self):
        head = self.ASSET_PATH.read_text()[:200]
        assert "typeof exports" in head or "mermaid" in head.lower()

    def test_page_reads_the_vendored_asset(self):
        assert "mermaid-10.9.6.min.js" in self.ARCH_SRC
        assert "_mermaid_js()" in self.ARCH_SRC

    def test_mermaid_initialize_still_called(self):
        assert "mermaid.initialize(" in self.ARCH_SRC
