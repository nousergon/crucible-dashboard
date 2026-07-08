"""Trust-battery leg registry — WHAT is verified, WHERE, and what it proves.

The registry is configuration (which named test suites constitute the trust
battery, config#1958), not results: the RESULTS come live from each repo's
main-branch CI run via ``loaders.trust_battery_loader`` — derive, don't
transcribe. A leg listed here with a test path that stops existing upstream
would show as a failing/missing CI signal, not as a silently stale claim.

Legs verified against the actual repos 2026-07-08 (I1958 discovery): the
backtester legs shipped under config#637; the evaluator legs under
crucible-evaluator PR100.
"""
from __future__ import annotations

# Each leg: stable key, repo (owner fixed: nousergon), the CI workflow whose
# main-branch conclusion vouches for it, the test path(s) inside that suite,
# a one-line "proves" for the prosumer reader, and an honest caveat where one
# exists. Ordering = display order (backtest engine first, then the grader).
BATTERY_LEGS: list[dict] = [
    {
        "key": "null_calibration",
        "title": "Null calibration",
        "repo": "crucible-backtester",
        "workflow": "ci.yml",
        "tests": ["tests/test_null_calibration.py"],
        "proves": "Fed pure noise (random-walk prices, uninformative signals), the engine finds nothing: alpha CI covers zero, the significance gate's false-positive rate stays at its nominal level, and fees only ever subtract.",
        "caveat": None,
    },
    {
        "key": "closed_form_oracle",
        "title": "Closed-form scenarios + independent oracle",
        "repo": "crucible-backtester",
        "workflow": "ci.yml",
        "tests": ["tests/test_closed_form_scenarios.py"],
        "proves": "Hand-computable scenarios (every fill, fee and NAV row derivable on paper) match the production engine to machine precision, and an independent NumPy re-implementation agrees across a fee/slippage grid.",
        "caveat": None,
    },
    {
        "key": "golden_benchmarks",
        "title": "Golden external benchmarks",
        "repo": "crucible-backtester",
        "workflow": "ci.yml",
        "tests": ["tests/test_golden_benchmarks.py"],
        "proves": "The engine reproduces published reality: SPY 2023 buy-and-hold total return within 1 point of the published figure, and the NVDA 2024 10:1 split produces no phantom return through the split date.",
        "caveat": None,
    },
    {
        "key": "lookahead_audit",
        "title": "Lookahead audit with tolerance bands",
        "repo": "crucible-backtester",
        "workflow": "ci.yml",
        "tests": ["tests/test_parity_alarms.py", "tests/test_pit_parity.py"],
        "proves": "Every weekly run re-scores the strategy on point-in-time data and reports the delta against the lookahead-contaminated view, with per-metric tolerance bands.",
        "caveat": "Observe mode: bands compute and record every week; automated paging on breach is deliberately operator-gated and not yet flipped.",
    },
    {
        "key": "source_verification",
        "title": "Grader source verification",
        "repo": "crucible-evaluator",
        "workflow": "ci.yml",
        "tests": ["tests/test_source_verification.py"],
        "proves": "Graded values (alpha vs SPY, max drawdown, Sharpe) are re-derived from the source ledger by independent implementations and must agree to 1e-9 — the grader is checked against its sources, not against its own author.",
        "caveat": None,
    },
    {
        "key": "threshold_audit",
        "title": "Status thresholds + critical gates",
        "repo": "crucible-evaluator",
        "workflow": "ci.yml",
        "tests": ["tests/test_module_agg.py", "tests/test_metric_record.py"],
        "proves": "Every GREEN/WATCH/RED boundary and the critical-gate roll-up is exercised, including the rule that an ungraded metric can never produce a false GREEN.",
        "caveat": None,
    },
    {
        "key": "trend_threading",
        "title": "Trend integrity across gaps",
        "repo": "crucible-evaluator",
        "workflow": "ci.yml",
        "tests": ["tests/test_history.py"],
        "proves": "Multi-week trends skip ungraded weeks instead of zero-filling them, and a real storage error fails loudly rather than degrading to an empty history.",
        "caveat": "Trend threading is asserted end-to-end for 2 of 9 tiles today; extending the assertion to the rest is tracked open work.",
    },
]

# Curated findings ledger — defects the battery caught, with receipts. This is
# editorial provenance (what the process found), each entry anchored to a
# merged PR so every claim is checkable.
BATTERY_FINDINGS: list[dict] = [
    {
        "date": "2026-07-08",
        "found_by": "threshold_audit",
        "finding": "False-GREEN path: an ungraded (N/A) portfolio_outcome tile let the overall card claim GREEN off the remaining tiles alone. Fixed to hold WATCH per the documented never-a-false-GREEN rule.",
        "fix": "crucible-evaluator#100",
    },
    {
        "date": "2026-07-08",
        "found_by": "source_verification",
        "finding": "Producer/consumer naming drift: the EOD ledger's CSV column is daily_alpha_pct while the same producer's data manifest calls it alpha_pct — a consumer reading the documented name computed nothing.",
        "fix": "crucible-dashboard#349",
    },
    {
        "date": "2026-06-10",
        "found_by": "null_calibration",
        "finding": "The scoring-weight promotion gate accepted noise ~10% of the time under null input; the significance-floor enforcement arc followed from this finding.",
        "fix": "alpha-engine-config#1426",
    },
]
