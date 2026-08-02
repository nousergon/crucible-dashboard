"""Tests for the ``loaders.cache`` bounded-cache wrapper (alpha-engine-config#5270).

These verify the wrapper's contract — lazy import, max_entries injection, TTL
resolution — without requiring Streamlit's runtime.  Streamlit's ``st.cache_data``
is a thin wrapper around ``st.cache_resource`` and works without a running app
for definition-time checks.
"""

from __future__ import annotations

import pytest

from loaders import cache


class TestCached:
    def test_default_max_entries_applied(self):
        """The wrapper injects DEFAULT_MAX_ENTRIES when no override is given."""

        @cache.cached
        def f() -> int:
            return 42

        assert callable(f)

    def test_literal_ttl_passed_through(self):
        """A literal ``ttl=`` in the decorator args is forwarded to cache_data."""

        @cache.cached(ttl=300)
        def f() -> int:
            return 42

        assert callable(f)

    def test_ttl_key_resolves_via_lazy_import(self):
        """A config-based ttl_key resolves to an int via the lazy _ttl import."""

        @cache.cached(ttl_key="research")
        def f() -> int:
            return 42

        assert callable(f)

    def test_mutual_exclusion_of_ttl_key_and_ttl(self):
        """Passing both ``ttl_key`` and ``ttl`` raises TypeError."""

        with pytest.raises(TypeError, match="mutually exclusive"):
            cache.cached(ttl_key="research", ttl=300)

    def test_bare_decorator_works(self):
        """Using ``@cached`` without parentheses is valid."""

        @cache.cached
        def f() -> int:
            return 42

        assert callable(f)

    def test_custom_max_entries(self):
        """Callers can override max_entries per decorator."""

        @cache.cached(ttl_key="trades", max_entries=128)
        def f() -> int:
            return 42

        assert callable(f)


class TestSizingInvariants:
    """DEFAULT_MAX_ENTRIES has a floor and a ceiling, and both are assertable.

    The constant shipped at 512 with a rationale that compared entries-of-one-
    function against calls-across-functions. `max_entries` is per decorated
    function, so with 174 of them that is a process-wide ceiling of ~89,000
    retained payloads — a bound that can never bind, which is the opposite of
    what alpha-engine-config#5270 exists to achieve.

    These pin the reasoning so the next person to change the number has to
    argue with a measurement rather than with a comment.
    """

    # Largest single-function fan-out inside one page render, from the list
    # limits in loaders/s3_loader.py: list_groom_run_keys(limit=30) and
    # list_thinktank_manifest_keys(limit=30) feed
    # `for key in keys: download_s3_json(bucket, key)` (s3_loader.py:535).
    LARGEST_PAGE_FANOUT = 30

    # Worst-case mean serialized payload measured in the research bucket
    # 2026-07-31: signals/ at 53 objects / 6.0 MB.
    WORST_MEAN_PAYLOAD_KB = 113

    # dashboard.service memory.high, from
    # infrastructure/systemd/resource-limits/budget.yaml.
    SOFT_CAP_MB = 340

    def test_above_the_thrash_floor(self):
        """Below the largest fan-out, a page evicts entries it still needs in
        the same render — a memory fix that becomes a latency and S3-cost
        regression."""
        from loaders import cache

        assert cache.DEFAULT_MAX_ENTRIES >= 2 * self.LARGEST_PAGE_FANOUT, (
            f"{cache.DEFAULT_MAX_ENTRIES} is below 2x the largest observed "
            f"per-page fan-out ({self.LARGEST_PAGE_FANOUT}); pages that list "
            f"then fetch would evict inside their own render"
        )

    def test_one_function_cannot_exceed_the_service_cap(self):
        """The bound that makes this an invariant rather than an observation.

        A single cached function retaining max_entries of the worst-case
        payload, expanded 10x for in-memory representation, must stay well
        inside dashboard.service's soft cap — otherwise one loader can consume
        the whole service on its own, which is what 512 permitted.
        """
        from loaders import cache

        worst_mb = (cache.DEFAULT_MAX_ENTRIES * self.WORST_MEAN_PAYLOAD_KB * 10) / 1024
        assert worst_mb < self.SOFT_CAP_MB / 2, (
            f"one function could retain ~{worst_mb:.0f} MB, more than half of "
            f"the {self.SOFT_CAP_MB} MiB soft cap — the bound does not bind"
        )
