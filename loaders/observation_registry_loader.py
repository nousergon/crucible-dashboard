"""
OBSERVATION_REGISTRY.yaml loader for the dashboard.

The registry is the SoT for in-flight observe-mode rollouts (sibling
axis to ARTIFACT_REGISTRY: freshness asks "does the artifact land?" /
observation asks "is the consumer plumbed to read it yet?"). It lives
in the private `alpha-engine-config` repo, which is gitignored from
the dashboard repo but co-located on the EC2 console instance via
`infrastructure/boot-pull.sh` (pulls `/home/ec2-user/alpha-engine-config`
on every boot).

This loader reads the YAML directly from disk — no Lambda or S3 hop
needed (unlike ARTIFACT_REGISTRY, which has a freshness-monitor Lambda
writing aggregated state to S3). The registry IS the data; the
dashboard renders it as-is.

Path resolution (in priority order):

  1. ``OBSERVATION_REGISTRY_PATH`` env var if set (test override).
  2. ``/home/ec2-user/alpha-engine-config/private-docs/OBSERVATION_REGISTRY.yaml``
     (the EC2 console path that ``boot-pull.sh`` populates).
  3. ``~/Development/alpha-engine-config/private-docs/OBSERVATION_REGISTRY.yaml``
     (local-dev path — sibling-directory layout under ``~/Development``).
  4. Sibling-relative: ``<dashboard-repo>/../alpha-engine-config/private-docs/OBSERVATION_REGISTRY.yaml``
     (catches non-default layouts where both repos are checked out as
     siblings under an arbitrary parent).

Returns ``None`` if no path resolves — caller renders a "registry not
found" panel rather than crashing the page.

Defaults block in the YAML merges into each entry (so
`verification_status: audit-found-needs-curation` survives without
per-row repetition); this loader applies the same merge.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


OBSERVATION_REGISTRY_FILENAME = "OBSERVATION_REGISTRY.yaml"


def private_docs_candidate_paths(filename: str, env_var: str) -> list[Path]:
    """Candidate locations for one file under ``alpha-engine-config/private-docs/``.

    The 4-tier convention documented at the top of this module, shared so
    every private-docs file the dashboard reads resolves the same way
    (``loaders/pipeline_diagrams_loader.py`` reuses it, config#13945):
    ``env_var`` override, EC2 console path, ``~/Development`` sibling, then
    sibling-relative to this checkout.
    """
    candidates: list[Path] = []

    env_override = os.environ.get(env_var)
    if env_override:
        candidates.append(Path(env_override))

    candidates.append(Path("/home/ec2-user/alpha-engine-config/private-docs") / filename)
    candidates.append(Path.home() / "Development" / "alpha-engine-config" / "private-docs" / filename)

    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "alpha-engine-config" / "private-docs" / filename)

    return candidates


def _candidate_paths() -> list[Path]:
    return private_docs_candidate_paths(OBSERVATION_REGISTRY_FILENAME, "OBSERVATION_REGISTRY_PATH")


def _resolve_path() -> Path | None:
    for p in _candidate_paths():
        if p.exists():
            return p
    return None


def _apply_defaults(entry: dict, defaults: dict) -> dict:
    """Top-level ``defaults`` block merges into each entry. Entry-level
    fields win over defaults (only fills in unspecified keys)."""
    merged = dict(defaults)
    merged.update(entry)
    return merged


def load_observation_registry() -> dict[str, Any] | None:
    """Return the parsed registry dict (with defaults merged into each
    observation), or None if the file cannot be located/parsed.

    Shape:

        {
            "schema_version": 1,
            "defaults": {...},
            "observations": [
                {observation_id, producer_repo, ..., verification_status, ...},
                ...
            ],
            "_source_path": "<path the file was read from>",
        }
    """
    path = _resolve_path()
    if path is None:
        return None

    try:
        data = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError):
        return None

    if not isinstance(data, dict):
        return None

    observations_raw = data.get("observations")
    if not isinstance(observations_raw, list):
        return None

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        defaults = {}

    merged_observations = [
        _apply_defaults(entry, defaults) if isinstance(entry, dict) else entry
        for entry in observations_raw
    ]

    return {
        "schema_version": data.get("schema_version"),
        "defaults": defaults,
        "observations": merged_observations,
        "_source_path": str(path),
    }


def summarize_by_state(observations: list[dict]) -> dict[str, int]:
    """Counts by ``state`` field — feeds the System Health KPI strip."""
    counts: dict[str, int] = {"always-on": 0, "gated-on": 0, "gated-off": 0}
    for obs in observations:
        state = obs.get("state")
        if state in counts:
            counts[state] += 1
    return counts


def summarize_by_phase(observations: list[dict]) -> dict[str, int]:
    """Counts by ``phase`` field — secondary breakdown for the
    deep-dive page."""
    counts: dict[str, int] = {
        "substrate": 0,
        "observe": 0,
        "cutover": 0,
        "promoted": 0,
    }
    for obs in observations:
        phase = obs.get("phase")
        if phase in counts:
            counts[phase] += 1
    return counts
