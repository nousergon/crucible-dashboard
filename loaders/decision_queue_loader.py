"""Load + act on the human-gated backlog pool (Decision Queue, config#1926).

The console's ONE write scope: the GitHub issue tracker. Rulings made on the
Decision Queue page post an operator-decision comment and strip the ``gate:*``
label so the next tier groom executes the ruling — the console never writes
S3 config, SSM trading params, or any trading state (ARCHITECTURE.md
carve-out, config#1926).

Read side: open issues carrying ``gate:operator`` / ``gate:decision`` across
the four backlog repos, oldest-first, with the structured ``**Ask:**`` block
(config#1923 contract) parsed from the newest gating comment.

Auth: the groom PAT from SSM ``/alpha-engine/groom/github_pat`` (cross-repo
issues r/w — the same identity the groom board-sync uses), falling back to
``FLOW_DOCTOR_GITHUB_TOKEN``/``GH_TOKEN`` env or ``gh auth token`` for local
dev. GitHub is reached via ``urllib`` (NOT ``gh`` — proxy-TLS constraint).
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import streamlit as st

logger = logging.getLogger(__name__)

BACKLOG_REPOS = [
    "nousergon/alpha-engine-config",
    "nousergon/metron-ops",
    "nousergon/vires-ops",
    "nousergon/telos-ops",
]
HUMAN_GATE_LABELS = ("gate:operator", "gate:decision")
SESSION_LABEL = "triage:session"
_GROOM_PAT_SSM_PARAM = "/alpha-engine/groom/github_pat"
_REGION = os.environ.get("AWS_REGION", "us-east-1")
_API = "https://api.github.com"
_CACHE_TTL_S = 300  # page must reflect a just-made ruling on refresh
_COMMENT_TAIL = 10  # newest comments scanned for the gating Ask block

_ASK_RE = re.compile(r"^\*\*Ask:\*\*\s*(.+)$", re.MULTILINE)
_OPTION_RE = re.compile(r"(?:^|\s)([A-D])\)\s*(.+?)(?=\s+[B-D]\)|$)", re.MULTILINE)
_REEXAM_RE = re.compile(r"^Re-exam:\s*\d{4}-\d{2}-\d{2}\s*$", re.MULTILINE)


@dataclass
class DecisionItem:
    repo: str  # owner/repo
    number: int
    title: str
    gate: str  # the gate:* label carried
    age_days: int
    url: str
    ask: str | None = None
    options: list[tuple[str, str]] = field(default_factory=list)  # [("A", text)]
    recommended: str | None = None  # option letter marked "(recommended)"
    excerpt: str | None = None  # newest gate comment fallback when no Ask block
    body: str = ""

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


# ── auth ─────────────────────────────────────────────────────────────────────


def _token_from_ssm() -> str | None:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover
        return None
    try:
        ssm = boto3.client("ssm", region_name=_REGION)
        resp = ssm.get_parameter(Name=_GROOM_PAT_SSM_PARAM, WithDecryption=True)
        return resp["Parameter"]["Value"].strip() or None
    except (BotoCoreError, ClientError) as exc:
        logger.warning("decision_queue: SSM groom-PAT fetch failed: %s", exc)
        return None


def _token_from_env() -> str | None:
    for name in ("FLOW_DOCTOR_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        tok = os.environ.get(name)
        if tok:
            return tok.strip()
    try:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True,
        ).stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


@st.cache_resource(ttl=3600)
def github_token() -> str | None:
    """Groom PAT from SSM (the declared write identity), env/gh fallback."""
    return _token_from_ssm() or _token_from_env()


def _request(method: str, url: str, payload: dict | None = None) -> Any:
    token = github_token()
    if not token:
        raise RuntimeError(
            "No GitHub token — SSM /alpha-engine/groom/github_pat unreadable "
            "(dashboard-role needs ssm:GetParameter on it) and no env fallback."
        )
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "alpha-engine-dashboard-decision-queue",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    return json.loads(body) if body.strip() else None


# ── Ask-block parsing (pure — unit-tested) ───────────────────────────────────


def parse_ask_block(text: str) -> tuple[str | None, list[tuple[str, str]], str | None]:
    """Extract (ask, options, recommended_letter) from a gating comment.

    Contract (config#1923): ``**Ask:** <one line>`` + ``**Options:** A) ...
    (recommended) B) ...``. Returns (None, [], None) when no Ask line exists.
    """
    m = _ASK_RE.search(text or "")
    if not m:
        return None, [], None
    ask = m.group(1).strip()
    options: list[tuple[str, str]] = []
    recommended: str | None = None
    opt_m = re.search(r"\*\*Options:\*\*\s*(.+?)(?=\n\*\*|\Z)", text, re.DOTALL)
    if opt_m:
        for letter, body in _OPTION_RE.findall(opt_m.group(1)):
            body = " ".join(body.split())
            options.append((letter, body))
            if "(recommended)" in body.lower():
                recommended = letter
    return ask, options, recommended


def bump_reexam_line(body: str, new_date: str) -> str:
    """Replace (or append) the parseable ``Re-exam: YYYY-MM-DD`` body line."""
    if _REEXAM_RE.search(body or ""):
        return _REEXAM_RE.sub(f"Re-exam: {new_date}", body, count=1)
    return (body or "").rstrip() + f"\n\nRe-exam: {new_date}\n"


def ruling_comment(option: str, detail: str, when: str) -> str:
    """The exact comment a console ruling posts — parsed by no one, read by
    the next tier groom's executor, so it must be self-contained."""
    line = f"**Operator decision {when}: {option}**"
    if detail:
        line += f" — {detail}"
    return line + "\n\n_Ruled via console Decision Queue (config#1926); gate label removed — actionable for the next tier groom._"


# ── read side ────────────────────────────────────────────────────────────────


def _newest_gate_comment(repo: str, number: int) -> str:
    comments = _request(
        "GET", f"{_API}/repos/{repo}/issues/{number}/comments?per_page={_COMMENT_TAIL}&sort=created&direction=desc",
    ) or []
    for c in comments:  # newest-first; first Ask block wins
        if _ASK_RE.search(c.get("body") or ""):
            return c["body"]
    return comments[0]["body"] if comments else ""


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner="Loading decision queue…")
def load_decision_queue() -> list[dict]:
    """All open human-gated issues, oldest-first, as plain dicts (cacheable)."""
    now = datetime.now(timezone.utc)
    items: list[DecisionItem] = []
    for repo in BACKLOG_REPOS:
        for label in HUMAN_GATE_LABELS:
            page = 1
            while True:
                batch = _request(
                    "GET",
                    f"{_API}/repos/{repo}/issues?state=open&labels={urllib.parse.quote(label)}"
                    f"&per_page=100&page={page}&sort=created&direction=asc",
                ) or []
                for it in batch:
                    if "pull_request" in it:
                        continue
                    label_names = {l["name"] for l in it["labels"]}
                    if SESSION_LABEL in label_names:
                        continue  # parked for the /backlog-triage session (config#1924)
                    created = datetime.fromisoformat(it["created_at"].replace("Z", "+00:00"))
                    comment_body = _newest_gate_comment(repo, it["number"])
                    ask, options, recommended = parse_ask_block(comment_body)
                    items.append(DecisionItem(
                        repo=repo, number=it["number"], title=it["title"],
                        gate=label, age_days=(now - created).days,
                        url=it["html_url"], ask=ask, options=options,
                        recommended=recommended,
                        excerpt=None if ask else (comment_body or it.get("body") or "")[:600],
                        body=it.get("body") or "",
                    ))
                if len(batch) < 100:
                    break
                page += 1
    # An issue carrying BOTH human gates appears once per label — dedupe.
    seen: set[str] = set()
    deduped = [i for i in items if not (i.key in seen or seen.add(i.key))]
    deduped.sort(key=lambda i: -i.age_days)
    return [i.__dict__ | {"key": i.key} for i in deduped]


def clear_queue_cache() -> None:
    load_decision_queue.clear()


# ── write side (the console's single write scope: the issue tracker) ────────


def _remove_gate_labels(repo: str, number: int) -> None:
    for label in HUMAN_GATE_LABELS:
        try:
            _request("DELETE", f"{_API}/repos/{repo}/issues/{number}/labels/{urllib.parse.quote(label)}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:  # 404 = label wasn't present; anything else is real
                raise


def post_ruling(repo: str, number: int, option: str, detail: str = "") -> None:
    """Ruling → comment + de-gate. The next tier groom executes."""
    when = datetime.now(timezone.utc).date().isoformat()
    _request("POST", f"{_API}/repos/{repo}/issues/{number}/comments",
             {"body": ruling_comment(option, detail, when)})
    _remove_gate_labels(repo, number)
    clear_queue_cache()


def kill_issue(repo: str, number: int, detail: str = "") -> None:
    when = datetime.now(timezone.utc).date().isoformat()
    _request("POST", f"{_API}/repos/{repo}/issues/{number}/comments",
             {"body": f"**Operator decision {when}: KILL** — {detail or 'not pursuing'}\n\n_Ruled via console Decision Queue (config#1926)._"})
    _request("PATCH", f"{_API}/repos/{repo}/issues/{number}",
             {"state": "closed", "state_reason": "not_planned"})
    clear_queue_cache()


def defer_issue(repo: str, number: int, new_date: str) -> None:
    issue = _request("GET", f"{_API}/repos/{repo}/issues/{number}")
    _request("PATCH", f"{_API}/repos/{repo}/issues/{number}",
             {"body": bump_reexam_line(issue.get("body") or "", new_date)})
    _request("POST", f"{_API}/repos/{repo}/issues/{number}/comments",
             {"body": f"**Operator: deferred to {new_date}** — via console Decision Queue (config#1926). Gate stands; Re-exam line bumped."})
    clear_queue_cache()


def send_to_session(repo: str, number: int) -> None:
    """Park for the interactive /backlog-triage session (config#1924)."""
    _request("POST", f"{_API}/repos/{repo}/issues/{number}/labels",
             {"labels": [SESSION_LABEL]})
    _request("POST", f"{_API}/repos/{repo}/issues/{number}/comments",
             {"body": "**Operator: needs discussion** — parked for the interactive `/backlog-triage` session (config#1924) via console Decision Queue."})
    clear_queue_cache()
