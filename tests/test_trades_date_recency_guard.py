"""Guard against trades.db `date` (trading_day) being read as trade recency.

config#1555 — the identical mistake recurred 3x independently in one session
(2026-07-01) across views/6_Execution.py, app.py, and
live/pages/holdings_and_trades.py: each sorted/summarized "recent trades" on
the backward-looking `date` (trading_day) column instead of `created_at` (the
actual fill timestamp), so a trade filled intraday today read as "nothing
traded today" until the NEXT session's close. Fixed in crucible-dashboard#286
and #287.

This test scans the dashboard surfaces where trade data is rendered for any
NEW occurrence of the same anti-pattern, and separately proves the scanner
actually catches it by deliberately reintroducing the pre-fix pattern.
"""

from pathlib import Path

from tests._trades_date_recency_guard import find_recency_violations, scan_paths

REPO_ROOT = Path(__file__).resolve().parent.parent

SCANNED_PATHS = [
    *sorted((REPO_ROOT / "views").glob("*.py")),
    REPO_ROOT / "app.py",
    *sorted((REPO_ROOT / "live" / "pages").glob("*.py")),
]


def test_scanned_paths_are_non_empty():
    """Sanity check the glob patterns still resolve to real files — an empty
    list would make the guard below silently vacuous."""
    assert len(SCANNED_PATHS) >= 3
    assert all(p.is_file() for p in SCANNED_PATHS)


def test_no_recency_reads_off_raw_session_date_column():
    violations = scan_paths(SCANNED_PATHS)
    assert not violations, (
        "Found trade-recency reads keyed on the raw trading_day `date` column "
        "instead of `created_at` (config#1555, recurrence of "
        f"crucible-dashboard#286/#287):\n{violations}"
    )


def test_guard_catches_deliberately_reintroduced_anti_pattern():
    """Prove the scanner is load-bearing: feed it the pre-fix shape of
    views/6_Execution.py (bare `date_col` sorted/maxed directly, the exact
    pattern crucible-dashboard#286 removed) and confirm it fails."""
    regressed_source = '''
date_col = next((c for c in ["date", "trade_date", "timestamp"] if c in trades_df.columns), None)
if date_col:
    trades_df[date_col] = pd.to_datetime(trades_df[date_col])
    trades_df = trades_df.sort_values(date_col, ascending=False).reset_index(drop=True)

if date_col is not None and not trades_df.empty:
    latest_day = trades_df[date_col].max().date()
    latest_rows = trades_df[trades_df[date_col].dt.date == latest_day]
'''
    violations = find_recency_violations(regressed_source, filename="<regressed-fixture>")
    assert violations, "guard failed to catch the reintroduced pre-#286 anti-pattern"
    assert any("sort_values" in v for v in violations)
    assert any("max" in v for v in violations)


def test_guard_allows_the_legitimate_session_keyed_join():
    """The research-score outcome join is genuinely trading_day-keyed and must
    stay green — it uses the raw `date` column for a join key, never for a
    recency op (`.max`/`.min`/`.sort_values`/`.dt.date` comparison)."""
    join_source = '''
date_col = next((c for c in ["date", "trade_date", "timestamp"] if c in trades_df.columns), None)
if date_col:
    filtered["_join_date"] = filtered[date_col].dt.date.astype(str)
'''
    assert find_recency_violations(join_source, filename="<join-fixture>") == []


def test_guard_allows_created_at_preferring_recency_column():
    """The post-fix shape (crucible-dashboard#286): a second variable that
    prefers `created_at` is what recency ops key on, not the raw resolver."""
    fixed_source = '''
date_col = next((c for c in ["date", "trade_date", "timestamp"] if c in trades_df.columns), None)
exec_date_col = "created_at" if "created_at" in trades_df.columns else date_col
if exec_date_col:
    trades_df[exec_date_col] = pd.to_datetime(trades_df[exec_date_col], utc=True)
    trades_df = trades_df.sort_values(exec_date_col, ascending=False).reset_index(drop=True)
    latest_day = trades_df[exec_date_col].max().date()
'''
    assert find_recency_violations(fixed_source, filename="<fixed-fixture>") == []
