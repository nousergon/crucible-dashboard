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

# Sizing rationale (alpha-engine-config#5270 scope item 3). MEASURED
# 2026-07-31, replacing the provisional 512 this file shipped with.
#
# ``max_entries`` is PER DECORATED FUNCTION, not per process. The original
# note read "the per-page working set is ~6-12 cached function calls, so 512
# gives 40x headroom" — that arithmetic compares entries-of-one-function
# against calls-across-functions, which are different axes. With 174 decorated
# functions the process-wide ceiling at 512 is ~89,000 retained payloads, a
# bound that can never bind. The issue's stated purpose is to convert the
# memory plateau from an observation into an invariant, and 512 does not.
#
# The bound has a FLOOR and a CEILING, and both are measurable.
#
# FLOOR — thrash. A value below the largest single-function fan-out inside one
# page render evicts entries the same render still needs, converting a memory
# problem into a latency-and-S3-cost problem. Largest observed fan-out is ~30:
# ``list_groom_run_keys(limit=30)`` and ``list_thinktank_manifest_keys(limit=30)``
# feed ``for key in keys: download_s3_json(bucket, key)`` (s3_loader.py:535),
# and ``load_model_zoo_history(limit=26)`` / ``list_groom_usage_records(days=21)``
# are the same shape. 64 is >2x that, so no page evicts inside its own render.
#
# CEILING — bytes, which is what actually matters and what an entry count only
# proxies. Measured payloads in the research bucket 2026-07-31:
#   decision_artifacts/  38,492 objects / 261.8 MB  => ~6.8 KB mean
#   signals/                 53 objects /   6.0 MB  => ~113 KB mean
# The generic key-addressed loaders (``download_s3_json``, ``download_s3_csv``,
# ``download_s3_text``) are the exposure: their key space is the bucket, so
# every distinct key browsed within one TTL is retained. At 64 entries the
# worst-case single-function retention is ~7 MB serialized, roughly 70 MB after
# a 10x in-memory expansion for a parsed DataFrame — inside the 340 MiB soft
# cap alongside the rest of the console. At 512 the same arithmetic gives
# ~58 MB serialized and ~580 MB resident for ONE function, which exceeds this
# service's entire 450 MiB hard cap on its own.
#
# So: 64. Above the thrash floor by 2x, and byte-bounded well inside the cap.
DEFAULT_MAX_ENTRIES = 64


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

    def _resolve_ttl(f):
        """Resolve the TTL using the DECORATED FUNCTION'S OWN module.

        Not `from loaders.s3_loader import _ttl`. That binds whichever module
        object the package import yields, which is not necessarily the one the
        decorated function lives in — the two differ whenever a module is
        loaded from a file path rather than by package name, which both the
        live app's test harness and any importlib-from-file loader do. The
        consequence is a config instance read from one module object while the
        function that uses it reads another, so a config mocked (or simply
        loaded) for one is invisible to the other.

        In production both resolve to the same object, which is exactly why
        this was invisible: it surfaced only as a FileNotFoundError in
        tests/test_system_pulse_loader.py. Reading `_ttl` out of the
        function's own globals is correct in both worlds and needs no import.
        """
        if ttl_key is None:
            return ttl
        own_ttl = f.__globals__.get("_ttl")
        if own_ttl is not None:
            return own_ttl(ttl_key)
        # Fall back for a decorated function defined outside a loader module.
        # Deliberately not silent about the distinction: if this import also
        # fails, the caller passed a ttl_key no module in scope can resolve,
        # and a wrong TTL is worse than a loud failure.
        from loaders.s3_loader import _ttl  # noqa: PLC0415
        return _ttl(ttl_key)

    def decorator(f):
        return st.cache_data(
            ttl=_resolve_ttl(f),
            max_entries=max_entries,
            show_spinner=kwargs.pop("show_spinner", False),
            **kwargs,
        )(f)

    if func is not None:
        return decorator(func)
    return decorator
