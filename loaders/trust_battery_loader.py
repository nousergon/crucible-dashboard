"""Live CI verdicts for the trust battery (config#1958).

The battery legs are pytest suites that run in each repo's main-branch CI —
so the freshest honest answer to "does the battery pass?" is the latest
completed main-branch run of that CI workflow, read live from the GitHub API
(same token path as ``pr_merge_loader``). No hand-kept results, no staleness:
a red main build shows red here.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import streamlit as st

from loaders.pr_merge_loader import _github_token

logger = logging.getLogger(__name__)

_OWNER = "nousergon"


@st.cache_data(ttl=300)
def load_ci_verdicts(repos: tuple[str, ...], workflow: str = "ci.yml") -> dict[str, dict]:
    """Latest completed main-branch run of ``workflow`` per repo.

    Returns ``{repo: {conclusion, head_sha, updated_at, html_url}}``; a repo
    whose lookup fails maps to ``{"conclusion": "unavailable", "error": …}``
    — rendered honestly, never dropped.
    """
    token = _github_token()
    out: dict[str, dict] = {}
    for repo in repos:
        if not token:
            out[repo] = {"conclusion": "unavailable", "error": "no GitHub token on this host"}
            continue
        url = (
            f"https://api.github.com/repos/{_OWNER}/{repo}/actions/workflows/"
            f"{workflow}/runs?branch=main&status=completed&per_page=1"
        )
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            runs = data.get("workflow_runs") or []
            if not runs:
                out[repo] = {"conclusion": "unavailable", "error": "no completed main-branch runs"}
                continue
            run = runs[0]
            out[repo] = {
                "conclusion": run.get("conclusion") or "unknown",
                "head_sha": (run.get("head_sha") or "")[:7],
                "updated_at": (run.get("updated_at") or "")[:16].replace("T", " "),
                "html_url": run.get("html_url") or "",
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            logger.warning("trust battery CI lookup failed for %s: %s", repo, e)
            out[repo] = {"conclusion": "unavailable", "error": str(e)[:120]}
    return out
