"""Load recently merged fleet PRs + human/agent merge attribution.

Primary merge list comes from the GitHub Search API (org:nousergon merged PRs).
Authoritative agent-vs-human overrides live in S3 at
``ops/pr_merge_attribution/latest.json`` — agents append via
``alpha-engine-config/scripts/record_agent_merge.py`` at self-merge time
(because ``mergedBy`` is always ``cipher813`` when using the operator PAT).

Heuristic fallbacks (lower confidence): ``agent-merged`` label, Dependabot
author, groom title prefix ``[P0``–``[P3``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any

import streamlit as st

from loaders.s3_loader import _fetch_s3_json, _research_bucket

logger = logging.getLogger(__name__)

_ATTRIBUTION_KEY = "ops/pr_merge_attribution/latest.json"
_GROOM_TITLE = re.compile(r"^\[P[0-3]")


def _github_token() -> str | None:
    for name in ("FLOW_DOCTOR_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        tok = os.environ.get(name)
        if tok:
            return tok.strip()
    try:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _github_graphql(query: str, variables: dict[str, Any] | None = None) -> dict:
    token = _github_token()
    if not token:
        raise RuntimeError(
            "No GitHub token — set FLOW_DOCTOR_GITHUB_TOKEN (hydrated on the box) "
            "or run `gh auth login` locally."
        )
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "alpha-engine-dashboard-pr-merge-loader",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if body.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {body['errors']}")
    return body["data"]


def pr_key(repo_full: str, number: int) -> str:
    return f"{repo_full}#{number}"


def load_merge_attribution() -> dict[str, dict[str, Any]]:
    """Return S3 attribution entries keyed by ``owner/repo#number``."""
    data = _fetch_s3_json(_research_bucket(), _ATTRIBUTION_KEY)
    if not isinstance(data, dict):
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    return entries


def classify_merge_source(
    row: dict[str, Any],
    attribution: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    """Return (source, confidence) where source ∈ human|agent|dependabot|bot."""
    key = pr_key(row["repo"], row["number"])
    recorded = attribution.get(key)
    if isinstance(recorded, dict) and recorded.get("merge_source"):
        return str(recorded["merge_source"]), "recorded"

    author = (row.get("author") or "").lower()
    merged_by = (row.get("merged_by") or "").lower()
    labels = {str(x).lower() for x in (row.get("labels") or [])}

    if "dependabot" in author:
        return "dependabot", "inferred"
    if author.endswith("[bot]") or merged_by.endswith("[bot]"):
        if "github-actions" in author or "github-actions" in merged_by:
            return "bot", "inferred"
        return "bot", "inferred"
    if "agent-merged" in labels:
        return "agent", "labeled"

    title = row.get("title") or ""
    if _GROOM_TITLE.match(title):
        return "agent", "heuristic-groom"

    return "human", "default"


@st.cache_data(ttl=900)
def load_merged_prs(days: int = 14) -> tuple[list[dict[str, Any]], int | None]:
    """Return (rows with merge_source + confidence, total_search_count).

    Each row adds: merge_source, confidence, pr (display), link.
    """
    until = date.today()
    since = until - timedelta(days=max(days - 1, 0))
    try:
        attribution = load_merge_attribution()
    except Exception as e:
        logger.warning("S3 attribution load failed (continuing without): %s", e)
        attribution = {}

    try:
        # Re-run search to get issueCount — cached wrapper calls once; embed count
        token = _github_token()
        if not token:
            raise RuntimeError("No GitHub token available")
        q = (
            f"org:nousergon is:pr is:merged "
            f"merged:{since.isoformat()}..{until.isoformat()}"
        )
        query = """
        query($q: String!, $first: Int!) {
          search(query: $q, type: ISSUE, first: $first) {
            issueCount
            nodes {
              ... on PullRequest {
                number title url mergedAt
                author { login }
                mergedBy { login }
                labels(first: 15) { nodes { name } }
                repository { nameWithOwner }
              }
            }
          }
        }
        """
        data = _github_graphql(query, {"q": q, "first": 100})
        search = data.get("search") or {}
        issue_count = search.get("issueCount")
        rows: list[dict[str, Any]] = []
        for node in search.get("nodes") or []:
            if not node:
                continue
            repo = (node.get("repository") or {}).get("nameWithOwner") or ""
            label_nodes = (node.get("labels") or {}).get("nodes") or []
            row = {
                "repo": repo,
                "number": node.get("number"),
                "title": node.get("title") or "",
                "url": node.get("url") or "",
                "merged_at": (node.get("mergedAt") or "")[:19].replace("T", " "),
                "author": (node.get("author") or {}).get("login") or "",
                "merged_by": (node.get("mergedBy") or {}).get("login") or "",
                "labels": [n.get("name") for n in label_nodes if n.get("name")],
            }
            source, confidence = classify_merge_source(row, attribution)
            row["merge_source"] = source
            row["confidence"] = confidence
            row["pr"] = f"#{row['number']}"
            row["link"] = row["url"]
            rows.append(row)
        rows.sort(key=lambda r: r.get("merged_at") or "", reverse=True)
        return rows, issue_count
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as e:
        logger.error("GitHub merged-PR fetch failed: %s", e)
        raise
