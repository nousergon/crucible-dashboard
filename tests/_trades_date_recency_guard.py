"""AST scanner backing test_trades_date_recency_guard.py (config#1555).

Flags the "trades.db `date` (trading_day) read as recency" anti-pattern that
recurred 3x on the dashboard in one session (2026-07-01, fixed by
crucible-dashboard#286/#287): a bare `date`/`trade_date`/`timestamp` column
resolver — the ``next((c for c in [...] if c in df.columns), None)`` idiom —
fed directly into a recency operation (``.sort_values``, ``.max``, ``.min``,
or a ``.dt.date`` comparison) instead of a `created_at`-preferring column.

`date` is genuinely trading_day-keyed and correct for session joins (e.g. the
research-score outcome join) — this scanner only flags the resolver variable
being used as the *recency* axis, not incidental non-recency uses.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SESSION_DATE_NAMES = {"date", "trade_date", "timestamp"}
_RECENCY_METHODS = {"sort_values", "max", "min"}


def _resolver_candidate_names(node: ast.expr) -> set[str] | None:
    """If `node` is a `next((c for c in [...] if ...), None)`-style column
    resolver, return the set of string literals in its candidate list."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "next"):
        return None
    if not node.args:
        return None
    gen = node.args[0]
    if not isinstance(gen, ast.GeneratorExp):
        return None
    for comp in gen.generators:
        if isinstance(comp.iter, ast.List):
            names = {
                elt.value
                for elt in comp.iter.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
            if names:
                return names
    return None


def _raw_session_date_resolver_vars(tree: ast.AST) -> set[str]:
    """Variable names assigned from a resolver whose candidate list is a
    subset of session-date names and does NOT include `created_at` (i.e. a
    resolver that can ONLY resolve to a backward-looking trading_day column,
    never to the fill timestamp)."""
    raw_vars: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        candidates = _resolver_candidate_names(node.value)
        if candidates is None:
            continue
        if "created_at" in candidates:
            continue
        if not candidates & _SESSION_DATE_NAMES:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                raw_vars.add(target.id)
    return raw_vars


def _names_in(node: ast.expr) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def find_recency_violations(source: str, filename: str = "<module>") -> list[str]:
    """Return human-readable violation descriptions, empty if clean."""
    tree = ast.parse(source, filename=filename)
    raw_vars = _raw_session_date_resolver_vars(tree)
    if not raw_vars:
        return []

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in _RECENCY_METHODS:
                used = _names_in(node.func.value) | {
                    n.id for arg in node.args if isinstance(arg, ast.Name) for n in [arg]
                }
                hit = used & raw_vars
                if hit:
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"{filename}:{lineno}: `.{attr}(...)` keyed on raw session-date "
                        f"resolver {sorted(hit)} — use a created_at-preferring column for "
                        f"recency (see crucible-dashboard#286/#287, config#1555)"
                    )
        # `<raw_var subscript>.dt.date == <today-like>` recency comparisons
        if isinstance(node, ast.Compare):
            left = node.left
            if (
                isinstance(left, ast.Attribute)
                and left.attr == "date"
                and isinstance(left.value, ast.Attribute)
                and left.value.attr == "dt"
            ):
                hit = _names_in(left.value.value) & raw_vars
                if hit:
                    lineno = getattr(node, "lineno", "?")
                    violations.append(
                        f"{filename}:{lineno}: `.dt.date` comparison keyed on raw "
                        f"session-date resolver {sorted(hit)} — use a created_at-preferring "
                        f"column for recency (see crucible-dashboard#286/#287, config#1555)"
                    )
    return violations


def scan_paths(paths: list[Path]) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
    for path in paths:
        source = path.read_text()
        violations = find_recency_violations(source, filename=str(path))
        if violations:
            results[str(path)] = violations
    return results
