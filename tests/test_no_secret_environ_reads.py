"""Regression: no module in this repo reads a secret via ``os.environ.get``
or ``os.getenv``.

Dashboard had zero secret reads at migration time (2026-05-12, PR 7 of
the .env-to-SSM arc) — it's a read-only Streamlit service that authenticates
to AWS via the EC2 instance role and reads from S3 + SQLite, no third-party
APIs. This test is preventive: if a future dashboard feature adds a secret
read (e.g. for a new data source), CI fails here, forcing the author to use
``nousergon_lib.secrets.get_secret()`` instead.

``ssm_secrets.py`` is allowlisted — it's the per-repo bulk-load shim
that stays alive until PR 9 of the arc.

alpha-engine-config-I7963: this repo-tree scan is blind to a first-party
*dependency* reading a pinned secret via ``os.environ.get`` from
``site-packages`` — exactly how ``nousergon_lib.preflight`` reading
``GITHUB_TOKEN`` bypassed this very invariant and halted preopen trading
(alpha-engine-config-I7924). ``test_no_secret_environ_reads_in_installed_dependencies``
below closes that gap using the scanner shared via
``nousergon_lib.testing.secret_scan`` (``crucible-predictor-PR536`` /
``nousergon-data-PR1483`` migrated first; this is the third and fourth
adoption, not a fresh copy — see ``nousergon-lib#345``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nousergon_lib.testing.secret_scan import scan_installed_packages

_REPO_ROOT = Path(__file__).resolve().parent.parent

# First-party packages this repo installs whose installed tree must also be
# clean of a pinned-secret os.environ.get read — the repo-tree scan below
# cannot see inside site-packages.
_DEPENDENCY_PACKAGES = ("nousergon_lib", "krepis")

_PINNED_SECRETS = frozenset(
    [
        "ANTHROPIC_API_KEY",
        "LANGCHAIN_API_KEY",
        "LANGSMITH_API_KEY",
        "VOYAGE_API_KEY",
        "POLYGON_API_KEY",
        "FMP_API_KEY",
        "FINNHUB_API_KEY",
        "FRED_API_KEY",
        "GMAIL_APP_PASSWORD",
        "GITHUB_TOKEN",
        "RAG_DATABASE_URL",
        "EDGAR_IDENTITY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        # alpha-engine-config-I2997 (2026-07-19): live/morning_brief.py's
        # OpenRouter migration — pinned so a future regression can't
        # os.environ.get/getenv this one either.
        "OPENROUTER_API_KEY",
    ]
)

_ALLOWED_FILES = frozenset(["ssm_secrets.py"])

_ENV_READ_RE = re.compile(
    r'os\.(?:environ\.get|getenv)\(\s*["\']([A-Z_][A-Z0-9_]*)["\']'
)


def _iter_python_files():
    for path in _REPO_ROOT.rglob("*.py"):
        parts = set(path.parts)
        if parts & {".venv", "build", "tests", "node_modules", "package"}:
            continue
        if path.name in _ALLOWED_FILES:
            continue
        yield path


def test_no_secret_environ_reads():
    violations: list[tuple[Path, int, str]] = []
    for path in _iter_python_files():
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _ENV_READ_RE.finditer(line):
                name = match.group(1)
                if name in _PINNED_SECRETS:
                    violations.append((path.relative_to(_REPO_ROOT), lineno, name))
    assert not violations, (
        "Found os.environ.get / os.getenv reads of pinned secrets — use "
        "`from nousergon_lib.secrets import get_secret` instead:\n"
        + "\n".join(f"  {p}:{ln}  {name}" for p, ln, name in violations)
    )


def test_no_secret_environ_reads_in_installed_dependencies():
    """The repo-tree scan above cannot see an installed dependency's source.

    ``nousergon_lib.preflight._github_auth_headers()`` reading
    ``GITHUB_TOKEN`` via a literal ``os.environ.get`` from site-packages is
    exactly the surface that let an expired credential halt preopen trading
    (alpha-engine-config-I7924) while this repo's own scan reported clean.
    """
    violations, missing = scan_installed_packages(_DEPENDENCY_PACKAGES, _PINNED_SECRETS)
    if violations:
        raise AssertionError(
            "Found os.environ.get reads of pinned secrets inside an "
            "INSTALLED first-party dependency — this repo's own tree scan "
            "cannot see this surface:\n"
            + "\n".join(f"  {v}" for v in violations)
        )
    if missing:
        pytest.skip(
            "first-party package(s) not importable in this environment — "
            f"the invariant is unverified against them this run: {', '.join(missing)}"
        )
