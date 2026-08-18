"""Guard: no test file resolves a path outside this repo's root without a
documented, CI-provisioned fallback (alpha-engine-config-I7605).

The defect this guards against: TestBlockedBySlugContractParity read
crucible-backtester's producer_champion_audit.schema.json from a sibling
checkout at ``~/Development/crucible-backtester`` (or a
``BACKTESTER_CONTRACTS_DIR`` env var CI set). Its pass/fail therefore
depended on which branch/state that checkout happened to be in on the
machine running the suite — not on the published contract. Fixed by moving
the schema into ``nousergon_lib.contracts`` (I7605): the frozen resource now
reaches this repo the same way every other cross-repo contract does, via
``pip install``, never a filesystem walk of another repo's working tree.

That fix is not generalizable to every cross-repo check this repo runs —
``test_pipeline_status_registry_drift.py`` and
``test_pipeline_status_reliability.py`` walk crucible-data's live Step
Function JSON DEFINITIONS (source files, not a versioned data contract) to
catch pipeline-status registry drift; there is no JSON Schema to lift into
nousergon-lib for those, so ci.yml deliberately checks that repo out into
``.sf-defs`` and sets ``SF_DEFS_DIR`` — a controlled, CI-provisioned sibling
checkout, not a naive walk of whatever happens to be on the runner. That
pattern is a documented, sanctioned exception here (allowlisted below); a
NEW resolution of this shape is not, until it is reviewed and added.

This guard fails on any test file matching the sibling-checkout-path
tell (``Path.home() / "Development" / <repo>`` or an
``os.environ["..._DIR"]``-gated variant of the same) that isn't in the
allowlist. Passing plain `pytest` collects and runs it — no extra
infrastructure needed to prove the invariant on a laptop with zero sibling
checkouts present.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TESTS_DIR = Path(__file__).parent

#: Sanctioned pre-existing sibling-checkout reads. Each entry names why it
#: cannot use the nousergon-lib-contract fix (I7605's actual fix) and
#: confirms the file has both a CI env-var override AND a hard-fail-on-CI
#: guard — the two properties that make "sibling checkout" safe rather than
#: "verdict depends on laptop state" (see module docstring).
_ALLOWLIST = {
    "test_pipeline_status_registry_drift.py": (
        "Walks nousergon-data's live SF JSON definitions (source files, not "
        "a JSON-Schema-able data contract). ci.yml checks that repo out into "
        ".sf-defs and sets SF_DEFS_DIR; _ON_CI hard-fails when absent on a "
        "runner rather than silently skipping."
    ),
    "test_pipeline_status_reliability.py": (
        "Same SF-definition boundary as the guard above (walks "
        "infrastructure/{step_function*}.json for stage-order parity). Fixed "
        "under I7605 to consult the same SF_DEFS_DIR ci.yml already sets and "
        "hard-fail on CI, matching the sibling guard's pattern — was "
        "previously a silent-skip-forever-on-CI defect of the same class "
        "this guard exists to catch."
    ),
}

# The tell: a path built by joining onto a sibling checkout, whether hardcoded
# to ~/Development/<repo> or gated behind an env var that names a directory
# (the CI-provisioned override for the same laptop convention).
_SIBLING_CHECKOUT_TELL = re.compile(
    r'Path\.home\(\)\s*/\s*["\']Development["\']'
    r'|os\.environ\[["\'][A-Z_]*_DIR["\']\]'
)


#: This guard's own file necessarily quotes the tell pattern in its module
#: docstring and _ALLOWLIST reasons (documenting what it looks for and why
#: the allowlisted files are safe) — exclude it from the scan of itself.
_THIS_FILE = Path(__file__).name


def _test_files():
    return sorted(
        p for p in TESTS_DIR.glob("test_*.py") if p.is_file() and p.name != _THIS_FILE
    )


def test_no_undocumented_sibling_checkout_path_resolution():
    offenders = []
    for path in _test_files():
        if path.name in _ALLOWLIST:
            continue
        text = path.read_text()
        if _SIBLING_CHECKOUT_TELL.search(text):
            offenders.append(path.name)
    assert not offenders, (
        f"test file(s) resolve a path outside this repo's root with no "
        f"documented CI-provisioned fallback: {offenders}. Either fix at the "
        f"contract layer (nousergon_lib.contracts, the I7605 pattern — "
        f"preferred whenever the sibling repo publishes a JSON Schema), or "
        f"add a reviewed entry to _ALLOWLIST in this file naming why not "
        f"and confirming the CI env-var override + hard-fail-on-CI guard "
        f"are both present."
    )


def test_allowlist_entries_still_exist_and_are_still_safe():
    """The allowlist names files, not a blanket exemption — if one is
    deleted or its CI-safety properties are removed, this must notice."""
    for name, _reason in _ALLOWLIST.items():
        path = TESTS_DIR / name
        assert path.exists(), f"allowlisted {name} no longer exists — remove its entry"
        text = path.read_text()
        assert "_ON_CI" in text, (
            f"{name} is allowlisted as a CI-provisioned sibling checkout but "
            f"no longer carries the hard-fail-on-CI guard (_ON_CI) — it can "
            f"silently skip on a runner again, exactly the defect class this "
            f"guard exists to catch."
        )
        assert "_DIR" in text, (
            f"{name} is allowlisted as CI-provisioned but no longer "
            f"references a *_DIR env var override."
        )
