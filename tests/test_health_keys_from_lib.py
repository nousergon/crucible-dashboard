"""config#1728 — dashboard health consumers derive from lib constants."""

from nousergon_lib.health import (
    DASHBOARD_HEALTH_MODULES,
    HEALTH_CHECK_CANDIDATES,
    REGISTRY_HEALTH_ARTIFACTS,
    health_key,
)


def test_health_check_candidates_cover_registry_primary_keys():
    """Every registry-monitored health artifact filename appears in a candidate list."""
    registry_filenames = {k.split("/", 1)[1] for k in REGISTRY_HEALTH_ARTIFACTS.values()}
    candidate_filenames = set()
    for filenames in HEALTH_CHECK_CANDIDATES.values():
        candidate_filenames.update(filenames)
    assert registry_filenames <= candidate_filenames


def test_dashboard_modules_resolve_via_health_key():
    for module_name, _bucket, _stale in DASHBOARD_HEALTH_MODULES:
        assert health_key(module_name).startswith("health/")
