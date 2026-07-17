"""Load + act on the human-gated backlog pool (Decision Queue, config#1926).

The console's ONE write scope: the GitHub issue/PR tracker. Rulings made on
the Decision Queue page post an operator-decision comment and strip the
``gate:*`` label so the next tier groom executes the ruling — the console
never writes S3 config, SSM trading params, or any trading state
(ARCHITECTURE.md carve-out, config#1926).

Read side: open issues AND PRs carrying ``gate:operator`` / ``gate:decision``
/ ``gate:device`` (config#2431: widened from operator/decision only — a
``gate:device`` item is JUST as human-only by definition, no S3/API check can
substitute for physically validating hardware) across the four backlog repos
(issues) plus ``CODE_REPOS`` (PRs — these gates were never scoped to the
backlog repos in the first place; the underlying labels already exist
fleet-wide on PRs today), oldest-first, with the structured ``**Ask:**``
block (config#1923 contract) parsed from the newest gating comment —
including the ``**Summary:**`` / ``**SOTA:**`` / ``**Delta:**`` fields the
groom prompts began emitting alongside Ask/Options/Consequence (config#1923
extension), generalizing the "every recommendation names the SOTA approach
and the delta" rule into the gate contract. All three degrade to ``None`` on
older or unframed comments — the parser never requires them.

config#2431 write-side extension: a ruling on a PR item now also RESOLVES
the gate mechanically — beyond the comment + label-strip every gate already
got, a draft PR with no OTHER ``gate:*`` label remaining is flipped
ready-for-review via GraphQL's ``markPullRequestReadyForReview`` (no plain
REST PATCH exists for this field, and the proxy-TLS constraint below forbids
shelling to ``gh pr ready`` the way the sweep scripts do). "The result of the
Decision Queue should resolve the gate" — Brian, 2026-07-13: ruling on the
console is now sufficient, no separate manual PR-hunting pass required.

Auth (config-I2785): ne-groomer App installation token first (minted via
``nousergon_lib.github_app`` from SSM ``/alpha-engine/groom/github_app_*`` —
the identity that rode through the 2026-07-16 GitHub user-token REST outage,
config-I2784, untouched), falling back to the groom PAT from SSM
``/alpha-engine/groom/github_pat``, then ``FLOW_DOCTOR_GITHUB_TOKEN``/
``GH_TOKEN`` env or ``gh auth token`` for local dev. GitHub is reached via
``urllib`` (NOT ``gh`` — proxy-TLS constraint).

alpha-engine-config-I2560: ``_list_gated_issues``/``_list_gated_prs`` read
``it["labels"]`` off the object itself (only to exclude ``SESSION_LABEL``
items), never trusting ``?labels=``-filtered list membership alone — the
same fleet-wide invariant the sibling ``gate_*_sweep.py`` scripts document
(ARCHITECTURE.md #94). The write side never re-derives gate state from list
membership either: ``post_ruling``/``kill_issue``/``defer_issue`` are
unconditional, human-triggered actions (not predicates over stale list
data), and ``_resolve_pr_gate_followup`` already re-fetches the live PR
immediately before its ready-flip PATCH/GraphQL call — i.e. the issue's
optional hardening (a fresh direct-object read right before a gate-clearing
write) is already standard practice here, not merely for GET-then-decide
disposition logic.
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import streamlit as st

logger = logging.getLogger(__name__)

BACKLOG_REPOS = [
    "nousergon/alpha-engine-config",
    "nousergon/metron-ops",
    "nousergon/vires-ops",
    "nousergon/telos-ops",
]
# Mirrors alpha-engine-config/scripts/gate_pr_actions.CODE_REPOS verbatim
# (config#2431) — PRs across these repos are ALSO scanned for the same
# human-only gate labels. Duplicated (not imported) since this is a
# different repo/deploy surface; kept in sync by inspection, same posture
# as gate_pr_actions.py's own contract test against groom_driver.CODE_REPOS.
CODE_REPOS = [
    "nousergon/alpha-engine-config", "nousergon/metron-ops",
    "nousergon/crucible-executor", "nousergon/nousergon-data",
    "nousergon/crucible-predictor", "nousergon/crucible-research",
    "nousergon/crucible-backtester", "nousergon/crucible-dashboard",
    "nousergon/crucible-evaluator", "nousergon/nousergon-lib",
    "nousergon/nousergon-docs", "nousergon/metron",
    "nousergon/vires", "nousergon/vires-ops",
    "nousergon/telos", "nousergon/telos-ops",
]
# config#2431: gate:device added — just as human-only as operator/decision
# (no S3/API check substitutes for physically validating hardware).
HUMAN_GATE_LABELS = ("gate:operator", "gate:decision", "gate:device")
SESSION_LABEL = "triage:session"
_GROOM_PAT_SSM_PARAM = "/alpha-engine/groom/github_pat"
_REGION = os.environ.get("AWS_REGION", "us-east-1")
_API = "https://api.github.com"
_CACHE_TTL_S = 300  # page must reflect a just-made ruling on refresh
_COMMENT_TAIL = 10  # newest comments scanned for the gating Ask block

_ASK_RE = re.compile(r"^\*\*Ask:\*\*\s*(.+)$", re.MULTILINE)
_OPTION_RE = re.compile(r"(?:^|\s)([A-D])\)\s*(.+?)(?=\s+[B-D]\)|$)", re.MULTILINE)
_REEXAM_RE = re.compile(r"^Re-exam:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^\*\*Summary:\*\*\s*(.+)$", re.MULTILINE)
_SOTA_RE = re.compile(r"^\*\*SOTA:\*\*\s*(.+)$", re.MULTILINE)
_DELTA_RE = re.compile(r"^\*\*Delta:\*\*\s*(.+)$", re.MULTILINE)


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
    summary: str | None = None  # plain-English context (config#1923 extension)
    sota: str | None = None  # the institutional-grade / SOTA approach
    delta: str | None = None  # how the recommended option differs from SOTA
    is_pr: bool = False  # config#2431: True for a CODE_REPOS PR item

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


def _token_from_app() -> str | None:
    """ne-groomer App installation token — the resilient bot identity.

    config-I2785: the 2026-07-16 GitHub partial outage (config-I2784) 503'd
    every cipher813 user-token REST call for ~an hour while App installation
    tokens were unaffected — so the App identity is the primary auth path and
    the operator PAT is the fallback, not the reverse.
    ``nousergon_lib.github_app`` caches and self-refreshes the minted token
    (5-min margin under the 60-min expiry), so calling per request is cheap.
    """
    try:
        from nousergon_lib.github_app import GitHubAppTokenError, installation_token
    except ImportError as exc:  # lib pin predates the module — PAT path still works
        logger.warning("decision_queue: nousergon_lib.github_app unavailable: %s", exc)
        return None
    try:
        return installation_token(region=_REGION)
    except GitHubAppTokenError as exc:
        logger.warning("decision_queue: App-token mint failed — PAT fallback: %s", exc)
        return None


@st.cache_resource(ttl=3600)
def _pat_token() -> str | None:
    """Operator-PAT fallback: SSM groom PAT, then env/gh for local dev."""
    return _token_from_ssm() or _token_from_env()


_auth_path_last_logged: str | None = None


def _log_auth_path(path: str) -> None:
    """One INFO line per auth-path transition (not per request) — the
    I2785 acceptance criterion is knowing WHICH identity served, without
    a log line on all ~30 fan-out calls of every page load."""
    global _auth_path_last_logged
    if path != _auth_path_last_logged:
        logger.info("decision_queue: GitHub auth path → %s", path)
        _auth_path_last_logged = path


def github_token() -> str | None:
    """App installation token first (config-I2785), operator PAT fallback.

    Deliberately NOT ``st.cache_resource``-cached at this level: installation
    tokens expire hourly, and the lib layer already caches + re-mints with a
    safety margin — a 3600s streamlit cache here could serve a dead token.
    Only the PAT fallback (stable secret) keeps the streamlit-level cache.
    """
    tok = _token_from_app()
    if tok is not None:
        _log_auth_path("app-installation-token")
        return tok
    _log_auth_path("groom-pat-fallback")
    return _pat_token()


def _request(method: str, url: str, payload: dict | None = None) -> Any:
    token = github_token()
    if not token:
        raise RuntimeError(
            "No GitHub token — App-token mint failed (SSM /alpha-engine/groom/"
            "github_app_*), groom PAT unreadable (SSM .../github_pat), and no "
            "env fallback. dashboard-role needs ssm:GetParameter on both."
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


def parse_context_block(text: str) -> tuple[str | None, str | None, str | None]:
    """Extract (summary, sota, delta) from a gating comment.

    Contract (config#1923 extension): ``**Summary:** <one line>`` /
    ``**SOTA:** <one line>`` / ``**Delta:** <one line>``, each independent
    of the others and of the Ask/Options block — an older or unframed
    comment simply yields ``None`` per missing field, never raises.
    """
    m_summary = _SUMMARY_RE.search(text or "")
    m_sota = _SOTA_RE.search(text or "")
    m_delta = _DELTA_RE.search(text or "")
    return (
        m_summary.group(1).strip() if m_summary else None,
        m_sota.group(1).strip() if m_sota else None,
        m_delta.group(1).strip() if m_delta else None,
    )


def bump_reexam_line(body: str, new_date: str) -> str:
    """Replace (or append) the parseable ``Re-exam: YYYY-MM-DD`` body line."""
    if _REEXAM_RE.search(body or ""):
        return _REEXAM_RE.sub(f"Re-exam: {new_date}", body, count=1)
    return (body or "").rstrip() + f"\n\nRe-exam: {new_date}\n"


def reexam_snoozed_until(body: str, today: date) -> str | None:
    """A FUTURE ``Re-exam: YYYY-MM-DD`` body line means operator-snoozed.

    This is the read side of the Defer button: ``defer_issue`` bumps the line
    and leaves the gate label standing (``gate_due_sweep.py`` re-arms via
    ``gate-due`` when the date arrives), so the queue MUST exclude the issue
    until then — the gate label alone cannot distinguish deferred from due.
    Returns the ISO date while snoozed, ``None`` when due today or absent.
    A malformed date fails OPEN (issue stays visible) with a WARN — an issue
    silently hidden by a typo is the worse failure mode.
    """
    m = _REEXAM_RE.search(body or "")
    if not m:
        return None
    try:
        parsed = date.fromisoformat(m.group(1))
    except ValueError:
        logger.warning("decision_queue: unparseable Re-exam date %r — showing item", m.group(1))
        return None
    return parsed.isoformat() if parsed > today else None


def ruling_comment(option: str, detail: str, when: str) -> str:
    """The exact comment a console ruling posts — parsed by no one, read by
    the next tier groom's executor, so it must be self-contained."""
    line = f"**Operator decision {when}: {option}**"
    if detail:
        line += f" — {detail}"
    return line + "\n\n_Ruled via console Decision Queue (config#1926); gate label removed — actionable for the next tier groom._"


# ── read side ────────────────────────────────────────────────────────────────


def _newest_gate_comment(repo: str, number: int) -> str:
    # The PER-ISSUE comments endpoint ignores sort/direction (those params
    # exist only on the repo-level endpoint) and always returns ASCENDING —
    # relying on them silently yields the OLDEST comments (bit 2026-07-07:
    # the page would have rendered stale June comments as the Ask). Fetch
    # ascending pages to the end, then scan newest-first.
    comments: list = []
    page = 1
    while True:
        batch = _request(
            "GET", f"{_API}/repos/{repo}/issues/{number}/comments?per_page=100&page={page}",
        ) or []
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    for c in reversed(comments[-_COMMENT_TAIL:]):  # newest-first; first Ask wins
        if _ASK_RE.search(c.get("body") or ""):
            return c["body"]
    return comments[-1]["body"] if comments else ""


def _list_gated_issues(repo: str, label: str) -> list[dict]:
    """Paginate one repo/label's open, non-PR, non-parked issues."""
    out: list[dict] = []
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
            out.append(it)
        if len(batch) < 100:
            break
        page += 1
    return out


def _list_gated_prs(repo: str, label: str) -> list[dict]:
    """Paginate one repo/label's open, non-parked PRs (config#2431) — the
    mirror image of ``_list_gated_issues``'s ``"pull_request" not in it``
    filter."""
    out: list[dict] = []
    page = 1
    while True:
        batch = _request(
            "GET",
            f"{_API}/repos/{repo}/issues?state=open&labels={urllib.parse.quote(label)}"
            f"&per_page=100&page={page}&sort=created&direction=asc",
        ) or []
        for it in batch:
            if "pull_request" not in it:
                continue
            label_names = {l["name"] for l in it["labels"]}
            if SESSION_LABEL in label_names:
                continue
            out.append(it)
        if len(batch) < 100:
            break
        page += 1
    return out


def _build_decision_item(repo: str, label: str, it: dict, now: datetime,
                         *, is_pr: bool = False) -> DecisionItem:
    created = datetime.fromisoformat(it["created_at"].replace("Z", "+00:00"))
    comment_body = _newest_gate_comment(repo, it["number"])
    ask, options, recommended = parse_ask_block(comment_body)
    summary, sota, delta = parse_context_block(comment_body)
    return DecisionItem(
        repo=repo, number=it["number"], title=it["title"],
        gate=label, age_days=(now - created).days,
        url=it["html_url"], ask=ask, options=options,
        recommended=recommended,
        excerpt=None if ask else (comment_body or it.get("body") or "")[:600],
        body=it.get("body") or "",
        summary=summary, sota=sota, delta=delta,
        is_pr=is_pr,
    )


@st.cache_data(ttl=_CACHE_TTL_S, show_spinner="Loading decision queue…")
def load_decision_queue() -> dict:
    """Open human-gated issues split into due vs snoozed, as plain dicts.

    Returns ``{"items": [...oldest-first, DUE...], "snoozed": [...]}`` —
    an issue whose ``Re-exam:`` date is in the future was deferred by the
    operator and MUST NOT re-enter the queue until due (the Defer button's
    whole contract); it's returned in ``snoozed`` so the page can show it's
    parked, not lost. Snoozed issues are filtered BEFORE the comment
    fan-out — no network spent on items that won't render.

    The issue-list fetch (repo x label, 8 calls) is cheap; the per-issue
    gate-comment lookup is not — one blocking GET per pending issue, serially
    that's O(N) round trips on a page load (~10s+ once the pool has 20-30
    items). Comment lookups are independent per issue, so they're fanned out
    across a thread pool rather than looped.
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    # An issue carrying BOTH human gates would otherwise get its comment
    # thread fetched twice — dedupe by (repo, number) BEFORE the network
    # fan-out, not after, so we never double-pay for a shared issue. (repo,
    # number) is a safe key even across the issue/PR scans below — GitHub
    # issues and PRs share one number sequence per repo, so a PR can never
    # collide with an issue of the same number.
    seen: set[tuple[str, int]] = set()
    to_build: list[tuple[str, str, dict, bool]] = []
    snoozed: list[dict] = []

    def _queue(repo: str, label: str, it: dict, *, is_pr: bool) -> None:
        key = (repo, it["number"])
        if key in seen:
            return
        seen.add(key)
        until = reexam_snoozed_until(it.get("body") or "", today)
        if until:
            snoozed.append({
                "key": f"{repo}#{it['number']}", "until": until,
                "title": it["title"], "url": it["html_url"],
            })
            return
        to_build.append((repo, label, it, is_pr))

    for repo in BACKLOG_REPOS:
        for label in HUMAN_GATE_LABELS:
            for it in _list_gated_issues(repo, label):
                _queue(repo, label, it, is_pr=False)
    for repo in CODE_REPOS:
        for label in HUMAN_GATE_LABELS:
            for it in _list_gated_prs(repo, label):
                _queue(repo, label, it, is_pr=True)

    items: list[DecisionItem] = []
    if to_build:
        with ThreadPoolExecutor(max_workers=min(8, len(to_build))) as pool:
            futures = [pool.submit(_build_decision_item, repo, label, it, now, is_pr=is_pr)
                       for repo, label, it, is_pr in to_build]
            items = [f.result() for f in futures]

    items.sort(key=lambda i: -i.age_days)
    snoozed.sort(key=lambda s: s["until"])
    return {"items": [i.__dict__ | {"key": i.key} for i in items], "snoozed": snoozed}


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


def _mark_pr_ready(repo: str, number: int) -> None:
    """Flip a draft PR to ready-for-review (config#2431). No plain REST PATCH
    exists for this field — GraphQL only — and the proxy-TLS constraint
    forbids shelling to ``gh pr ready`` the way the sweep scripts do, so this
    goes through the same ``_request`` urllib path as everything else."""
    pr = _request("GET", f"{_API}/repos/{repo}/pulls/{number}") or {}
    node_id = pr.get("node_id")
    if not node_id:
        logger.warning("decision_queue: could not fetch node_id for %s#%d — "
                       "cannot flip ready", repo, number)
        return
    query = ("mutation($id: ID!) { markPullRequestReadyForReview("
             "input: {pullRequestId: $id}) { pullRequest { id } } }")
    _request("POST", f"{_API}/graphql", {"query": query, "variables": {"id": node_id}})


def _resolve_pr_gate_followup(repo: str, number: int) -> None:
    """After a ruling has ALREADY removed this item's human gate label(s):
    if it's a draft PR with no OTHER ``gate:*`` label still blocking it,
    flip it ready (config#2431 — "the result of the Decision Queue should
    resolve the gate"). Re-fetches the LIVE PR (not a stale pre-ruling
    snapshot) so a concurrently-added label or a manual ready-flip is never
    raced. Never raises — a failed follow-up just leaves the PR draft for a
    human or the next sweep pass to retry; the ruling itself already landed.
    """
    try:
        pr = _request("GET", f"{_API}/repos/{repo}/pulls/{number}") or {}
    except Exception as exc:
        logger.warning("decision_queue: PR follow-up fetch failed for %s#%d: %s",
                       repo, number, exc)
        return
    if not pr.get("draft"):
        return
    remaining = [l["name"] for l in pr.get("labels", []) if l["name"].startswith("gate:")]
    if remaining:
        logger.info("decision_queue: %s#%d stays draft — still gated by %s",
                    repo, number, remaining)
        return
    try:
        _mark_pr_ready(repo, number)
    except Exception as exc:
        logger.warning("decision_queue: ready-flip failed for %s#%d: %s", repo, number, exc)


# Write actions deliberately do NOT clear_queue_cache()/force a reload: the
# view's `dq_done` session-state guard already hides the acted-on item
# instantly on the immediate st.rerun(), so a synchronous full re-fetch of
# every other pending issue on every single click bought nothing but a ~10s
# stall (the very serial fan-out load_decision_queue() pays for on a cache
# miss). Cache invalidation is explicit (the "Refresh queue" button) or via
# the 300s TTL — bounding, not eliminating, cross-session staleness.


def post_ruling(repo: str, number: int, option: str, detail: str = "", *,
                is_pr: bool = False) -> None:
    """Ruling → comment + de-gate (+ PR follow-up, config#2431). The next
    tier groom executes any remaining work; a fully-unblocked draft PR is
    flipped ready immediately rather than waiting on a groom pass to notice."""
    when = datetime.now(timezone.utc).date().isoformat()
    _request("POST", f"{_API}/repos/{repo}/issues/{number}/comments",
             {"body": ruling_comment(option, detail, when)})
    _remove_gate_labels(repo, number)
    if is_pr:
        _resolve_pr_gate_followup(repo, number)


def kill_issue(repo: str, number: int, detail: str = "") -> None:
    when = datetime.now(timezone.utc).date().isoformat()
    _request("POST", f"{_API}/repos/{repo}/issues/{number}/comments",
             {"body": f"**Operator decision {when}: KILL** — {detail or 'not pursuing'}\n\n_Ruled via console Decision Queue (config#1926)._"})
    _request("PATCH", f"{_API}/repos/{repo}/issues/{number}",
             {"state": "closed", "state_reason": "not_planned"})


def defer_issue(repo: str, number: int, new_date: str, body: str = "") -> None:
    """``body`` is the issue body already loaded by the queue — passing it
    avoids a redundant GET (the view has it on hand from `load_decision_queue`)."""
    if not body:
        issue = _request("GET", f"{_API}/repos/{repo}/issues/{number}")
        body = issue.get("body") or ""
    _request("PATCH", f"{_API}/repos/{repo}/issues/{number}",
             {"body": bump_reexam_line(body, new_date)})
    _request("POST", f"{_API}/repos/{repo}/issues/{number}/comments",
             {"body": f"**Operator: deferred to {new_date}** — via console Decision Queue (config#1926). Gate stands; Re-exam line bumped; hidden from the queue until then."})


def send_to_session(repo: str, number: int) -> None:
    """Park for the interactive /backlog-triage session (config#1924)."""
    _request("POST", f"{_API}/repos/{repo}/issues/{number}/labels",
             {"labels": [SESSION_LABEL]})
    _request("POST", f"{_API}/repos/{repo}/issues/{number}/comments",
             {"body": "**Operator: needs discussion** — parked for the interactive `/backlog-triage` session (config#1924) via console Decision Queue."})
