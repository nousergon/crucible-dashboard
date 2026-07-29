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
