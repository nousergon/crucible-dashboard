"""Guards for the Architecture page slim-down (config#1989 item 4) + mermaid
vendoring (config#1984 item 6).

Brian ruled (Decision Queue, 2026-07-08) Option A: keep the two mermaid
diagrams + module cards, delete the hand-kept prose narrative that had
drifted (it claimed "six modules" when the system has seven), and add a
pointer to the maintained narrative doc instead. Additionally, vendor
mermaid.js locally (config#1984 item 6) instead of loading from CDN.
Source-text assertions in the repo's usual style (streamlit is not
imported/run; see test_console_ia_phase3.py / test_saturday_sf_watch_page.py
for the same pattern).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ARCH_SRC = (REPO_ROOT / "views" / "10_Architecture.py").read_text()

# Rendered code only — excludes the module docstring and inline comments,
# which legitimately reference the retired strings below to explain why
# they were removed (config#1989 item 4 history note).
_DOCSTRING_END = ARCH_SRC.index('"""', ARCH_SRC.index('"""') + 3) + 3
ARCH_CODE = ARCH_SRC[_DOCSTRING_END:]
ARCH_CODE = "\n".join(
    line for line in ARCH_CODE.splitlines() if not line.strip().startswith("#")
)


class TestDriftedProseRemoved:
    def test_six_modules_claim_is_gone(self):
        # The old hero prose asserted an exact module count that drifted
        # out of sync with reality (six claimed, seven actual per
        # Report_Card.py's "all 7 modules" / six-component-module grade).
        assert "Six modules" not in ARCH_CODE
        assert "six modules" not in ARCH_CODE

    def test_stale_s3_contract_table_removed(self):
        # The hand-kept S3 data-contracts table listed config/scoring_weights.json
        # and config/predictor_params.json as live "Weekly (Sat)" contracts —
        # neither has ever been written (config#1841). Table deleted in favor
        # of a pointer to the maintained doc.
        assert "config/scoring_weights.json" not in ARCH_CODE
        assert "config/predictor_params.json" not in ARCH_CODE
        assert "## S3 data contracts" not in ARCH_CODE

    def test_phase_trajectory_table_removed(self):
        assert "Phase 4 | Live capital" not in ARCH_CODE


class TestKeptContent:
    def test_two_mermaid_diagrams_and_pipeline_sequences_remain(self):
        assert "flowchart LR" in ARCH_SRC
        assert "sequenceDiagram" in ARCH_SRC
        assert ARCH_SRC.count("render_mermaid(") >= 4  # system block + 3 pipeline sequences

    def test_module_cards_remain(self):
        assert "_module_card(" in ARCH_SRC
        for module in ("Research", "Predictor", "Executor", "Backtester", "Data Platform", "Dashboard"):
            assert f'"{module}"' in ARCH_SRC

    def test_mermaid_is_now_vendored(self):
        # Vendoring mermaid.js off the CDN (I1984 item 6) was initially out of
        # scope for the slim-down, but is now included in this PR. Verify the
        # CDN reference has been replaced with the vendored asset load.
        assert "cdn.jsdelivr.net/npm/mermaid" not in ARCH_SRC
        assert "mermaid-10.9.6.min.js" in ARCH_SRC
        assert "_mermaid_js()" in ARCH_SRC


class TestOverviewPointerAdded:
    def test_points_to_narrative_doc(self):
        assert "nousergon-docs" in ARCH_SRC
        assert "OVERVIEW.md" in ARCH_SRC
