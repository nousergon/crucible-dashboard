"""Burn-down guard v2 -- forbid NEW reads of the wide horizon-suffixed
score_performance columns (EPIC config#1483 Phase 3, consumer cutover
config#1531).

The eval horizon was historically encoded in scattered wide-suffixed column
names (`beat_spy_5d`, `beat_spy_21d`, `spy_21d_return`, `return_5d`,
`log_alpha_21d`, ...). Changing a horizon meant a fleet-wide rename, and an
incomplete rename silently starved consumers (config#1451/#1452/#1456). The
config#1483 fix makes the horizon a PARAMETER: consumers read the
long-format `score_performance_outcomes` store (one row per
signal/date/horizon) filtered by
`nousergon_lib.quant.horizons.HorizonPolicy`, instead of hardcoding `_Nd`
outcome-column literals.

This is a thin repo-side wrapper around the shared ratchet mechanics in
`nousergon_lib.quant.horizon_guard` (lifted to the lib on the SECOND
adoption -- crucible-backtester was first, nousergon-lib#149) rather than a
divergent local reimplementation:

  * any production (non-test) read of a wide horizon-suffixed outcome
    column fails CI, UNLESS the file is on `_MIGRATING` (the seed allowlist
    of files that still read the wide columns as of cutover kickoff) or
    `_ATTRIBUTION_JSON_EXEMPT` (files reading the SAME literal column names
    as dict keys inside a DIFFERENT artifact -- see below);
  * a `_MIGRATING` file that is now CLEAN also fails -- forcing the
    allowlist to burn down to {} as each consumer-cutover PR lands.

config#1531 (the dashboard consumer cutover) migrates the full cluster
(`loaders/db_loader.py` + `loaders/outcome_store.py` + `charts/
accuracy_chart.py` + the 5 views that actually read score_performance
outcome columns) in a single PR, so this guard SEEDS with an empty
`_MIGRATING` set -- there is nothing left to burn down for this repo.

`charts/attribution_chart.py` is a permanent exemption, NOT a migrating
entry: it reads `beat_spy_21d` / `return_21d` as dict keys inside
`attribution.json`, an S3 artifact produced by crucible-backtester's
`analysis/attribution.py` (a *different* physical store than
`score_performance` / `score_performance_outcomes`). Renaming those keys is
gated on the backtester's own producer schema (config#1481), not this
issue's `research.db` migration -- so it can never "migrate" via this
guard and would otherwise block `_MIGRATING` from ever reaching {}.
"""

from __future__ import annotations

from pathlib import Path

from nousergon_lib.quant.horizon_guard import (
    DEFAULT_EXCLUDE_PREFIXES,
    check_burndown,
    wide_columns_in,
)

_REPO = Path(__file__).resolve().parent.parent

# Files that read wide horizon-suffixed literals as dict keys into
# attribution.json (a crucible-backtester S3 artifact), never into
# score_performance / score_performance_outcomes. Permanently exempt --
# verified honest by test_attribution_json_exempt_entries_are_honest below.
_ATTRIBUTION_JSON_EXEMPT = frozenset({
    "charts/attribution_chart.py",
})

# Seeded EMPTY: the config#1531 cutover PR migrated every score_performance
# consumer (loader + charts/accuracy_chart.py + the 5 outcome-reading views)
# in one pass. A future regression that reintroduces a hardcoded wide-column
# read anywhere in this repo fails CI immediately.
_MIGRATING: frozenset[str] = frozenset()

_EXCLUDE_PREFIXES = DEFAULT_EXCLUDE_PREFIXES + ("synthetic/gate_calibration.py",)


def _report():
    return check_burndown(
        _REPO,
        migrating=_MIGRATING,
        exempt=_ATTRIBUTION_JSON_EXEMPT,
        exclude_prefixes=_EXCLUDE_PREFIXES,
    )


def test_no_ungrandfathered_wide_column_reads():
    """A NEW file reading a wide horizon column (not exempt) fails -- the bug
    class cannot be reintroduced now that this repo's cutover is complete."""
    report = _report()
    assert not report.violations, (
        "Production reads of wide horizon-suffixed score_performance columns "
        "(config#1483). Read the long-format score_performance_outcomes "
        "store via loaders.outcome_store (filtered by "
        "nousergon_lib.quant.horizons.HorizonPolicy) instead, or -- only if "
        "genuinely unavoidable -- add to _MIGRATING with a tracking "
        f"note:\n{dict(report.violations)}"
    )


def test_migrating_set_has_no_stale_entries():
    """Ratchet: a _MIGRATING file that no longer reads any wide column must
    be REMOVED from the allowlist. _MIGRATING is already {} for this repo, so
    this is a no-op guard against a future PR re-adding a stale entry."""
    assert not _report().stale_migrating


def test_attribution_json_exempt_entries_are_honest():
    """Every _ATTRIBUTION_JSON_EXEMPT file must (a) still read a wide-column
    literal and (b) read it from attribution_data / attribution.json, not
    query_research_db / score_performance -- so the permanent exemption
    can't silently hide a real score_performance read."""
    problems = {}
    for rel in _ATTRIBUTION_JSON_EXEMPT:
        path = _REPO / rel
        text = path.read_text(errors="ignore")
        if not wide_columns_in(path):
            problems[rel] = "no longer reads any wide-column literal -- remove from exempt"
        elif "score_performance" in text or "query_research_db" in text:
            problems[rel] = (
                "reads score_performance / query_research_db, not "
                "attribution_data -- move to _MIGRATING"
            )
    assert not problems, f"dishonest _ATTRIBUTION_JSON_EXEMPT entries: {problems}"


def test_seed_allowlist_matches_current_scan():
    """Sanity: every tracked entry exists + still reads a wide column, and no
    un-listed production file does. A drift in either direction is obvious
    at seed time (and on every CI run thereafter, since _MIGRATING is {})."""
    report = _report()
    assert not report.missing_entries
    assert not report.violations
    assert not report.stale_exempt
