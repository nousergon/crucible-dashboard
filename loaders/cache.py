"""Shared Streamlit cache wrapper — bounded footprint for the dashboard.

Replaces bare ``@st.cache_data`` to enforce ``max_entries`` on every cached
function, converting the console's memory plateau from an observation into an
invariant (alpha-engine-config#5270).

Usage
-----

    @cached(ttl_key="research")
    def load_portfolio(...): ...

    @cached(ttl=120)          # literal TTL, bypasses the config-based _ttl()
    def load_something(...): ...

The wrapper injects ``max_entries=DEFAULT_MAX_ENTRIES`` and
``show_spinner=False`` unless the caller overrides them.  ``max_entries`` is
sized from the dashboard's per-page working set — see the docstring at the
module level for the sizing rationale.

``@st.cache_resource`` (2 sites, singleton clients/connections) is intentionally
left unchanged — see alpha-engine-config#5270 scope.
"""

from __future__ import annotations

import streamlit as st

# Sizing rationale (alpha-engine-config#5270 scope item 3):
#
# The dashboard's per-page working set is ~6-12 cached function calls (charts,
# tables, metrics).  512 entries gives 40x headroom for the most complex page
# before eviction, keeping the plateau well within the 450 MiB memory.high cap.
#
# **This is a provisional default.**  The issue requires measurement-driven
# tuning: instrument ``st.cache_data`` hit rates before and after (see
# ``tests/test_cache_hit_rate.py``), then adjust this constant.  A value
# causing thrash (increased S3 GET cost + page latency) is worse than no
# cap — size conservatively upward until the hit-rate floor is met.
DEFAULT_MAX_ENTRIES = 512


def cached(
    func=None,
    *,
    ttl_key: str | None = None,
    ttl: int | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    **kwargs,
):
    """Bounded ``@st.cache_data`` with centralized TTL lookup.

    Can be used as a bare decorator (``@cached``), with a config-based TTL
    key (``@cached(ttl_key="research")``), or with a literal TTL
    (``@cached(ttl=120)``).  ``max_entries`` and ``show_spinner`` are set to
    bounded defaults unless overridden.

    Parameters
    ----------
    func : callable, optional
        The decorated function (bare decorator mode).
    ttl_key : str, optional
        Key into ``config.yaml``'s ``cache_ttl`` dict (via ``_ttl()``).
    ttl : int, optional
        Literal TTL in seconds.  Mutually exclusive with ``ttl_key``.
    max_entries : int, optional
        Maximum entries in the LRU cache (default ``DEFAULT_MAX_ENTRIES``).
    **kwargs
        Additional keyword arguments forwarded to ``st.cache_data``.
    """
    if ttl_key is not None and ttl is not None:
        raise TypeError("cached: ttl_key and ttl are mutually exclusive")

    # Lazy import to avoid circular dependency — loaders/s3_loader.py imports
    # cached from this module at module level while _ttl is defined after that
    # import point. By the time cached() is called (at function decoration time),
    # s3_loader.py has finished loading and _ttl is available.
    if ttl_key is not None:
        from loaders.s3_loader import _ttl  # noqa: PLC0415
        resolved_ttl = _ttl(ttl_key)
    else:
        resolved_ttl = ttl

    def decorator(f):
        return st.cache_data(
            ttl=resolved_ttl,
            max_entries=max_entries,
            show_spinner=kwargs.pop("show_spinner", False),
            **kwargs,
        )(f)

    if func is not None:
        return decorator(func)
    return decorator
