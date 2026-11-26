"""
PIPELINE_DIAGRAMS_GENERATED.md loader for the Architecture page (config#13945).

The three orchestrated pipelines used to be drawn on ``views/10_Architecture.py``
as hand-authored Mermaid strings, which drifted from the deployed Step
Functions. ``alpha-engine-config``'s ``scripts/gen_pipeline_diagrams.py``
(config#13944) now renders them from the live state-machine definitions into
``private-docs/PIPELINE_DIAGRAMS_GENERATED.md``; this loader reads that file
and hands the page one Mermaid source per state machine.

Path resolution is the observation registry's 4-tier convention
(``loaders/observation_registry_loader.private_docs_candidate_paths``):
``PIPELINE_DIAGRAMS_PATH`` env override, the EC2 console checkout that
``boot-pull.sh`` populates, ``~/Development`` sibling, sibling-relative.

Document contract (the generator's side of it): for each state machine in
``PIPELINE_STATE_MACHINES`` there is exactly one Markdown heading whose text
contains the state machine's name, and inside that heading's section (up to
the next heading of the same or a shallower level) a fenced ```` ```mermaid ````
block holding its diagram. The first such block in the section is the
diagram. Headings inside fenced blocks are ignored.

Nothing here falls back to an older diagram: a missing file, an unreadable
file, or a section without a usable diagram is reported in ``errors`` so the
page can show a visible diagnostic instead of stale pipelines.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from loaders.observation_registry_loader import private_docs_candidate_paths

PIPELINE_DIAGRAMS_FILENAME = "PIPELINE_DIAGRAMS_GENERATED.md"
PIPELINE_DIAGRAMS_ENV_VAR = "PIPELINE_DIAGRAMS_PATH"

# Live state machines the generator renders, in page order. The retired
# advisory / modelzoo-sunday machines are deliberately absent (config#13944).
PIPELINE_STATE_MACHINES: tuple[str, ...] = (
    "ne-weekly-freshness-pipeline",
    "ne-preopen-trading-pipeline",
    "ne-postclose-trading-pipeline",
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^`\s]*)")


@dataclass(frozen=True)
class PipelineDiagrams:
    """Result of one load: diagrams keyed by state machine, plus diagnostics.

    ``errors`` is empty only when every state machine in
    ``PIPELINE_STATE_MACHINES`` has a non-empty diagram.
    """

    source_path: Path | None
    searched: tuple[Path, ...]
    diagrams: dict[str, str] = field(default_factory=dict[str, str])
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def candidate_paths() -> list[Path]:
    return private_docs_candidate_paths(PIPELINE_DIAGRAMS_FILENAME, PIPELINE_DIAGRAMS_ENV_VAR)


def resolve_path(candidates: Sequence[Path]) -> Path | None:
    for path in candidates:
        if path.exists():
            return path
    return None


@dataclass(frozen=True)
class _Heading:
    line_index: int
    level: int
    text: str


@dataclass(frozen=True)
class _Fence:
    start: int
    end: int | None  # index of the closing fence line; None when unterminated
    info: str


def _scan(lines: Sequence[str]) -> tuple[list[_Heading], list[_Fence]]:
    """Headings outside fences, and every fenced block, in document order."""
    headings: list[_Heading] = []
    fences: list[_Fence] = []
    open_marker: str | None = None
    open_start = 0
    open_info = ""
    for i, line in enumerate(lines):
        fence = _FENCE_RE.match(line)
        if open_marker is not None:
            if (
                fence is not None
                and fence.group(1)[0] == open_marker[0]
                and len(fence.group(1)) >= len(open_marker)
                and not fence.group(2)
            ):
                fences.append(_Fence(open_start, i, open_info))
                open_marker = None
            continue
        if fence is not None:
            open_marker, open_start, open_info = fence.group(1), i, fence.group(2).lower()
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            headings.append(_Heading(i, len(heading.group(1)), heading.group(2)))
    if open_marker is not None:
        fences.append(_Fence(open_start, None, open_info))
    return headings, fences


def _heading_names(text: str, name: str) -> bool:
    """True when ``name`` appears in ``text`` as a whole token, so
    ``ne-preopen-trading-pipeline`` never matches a longer name."""
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def extract_diagrams(
    text: str, state_machines: Sequence[str] = PIPELINE_STATE_MACHINES
) -> tuple[dict[str, str], list[str]]:
    """Pull one Mermaid source per state machine out of the generated doc.

    Returns ``(diagrams, errors)``; a state machine is in ``diagrams`` only
    when its section is unambiguous and holds a non-empty, terminated
    ``mermaid`` block.
    """
    lines = text.splitlines()
    headings, fences = _scan(lines)
    diagrams: dict[str, str] = {}
    errors: list[str] = []

    for name in state_machines:
        matches = [h for h in headings if _heading_names(h.text, name)]
        if not matches:
            errors.append(f"`{name}`: no section heading names this state machine.")
            continue
        if len(matches) > 1:
            errors.append(f"`{name}`: {len(matches)} section headings name this state machine; expected one.")
            continue
        heading = matches[0]
        section_end = next(
            (h.line_index for h in headings if h.line_index > heading.line_index and h.level <= heading.level),
            len(lines),
        )
        block = next(
            (
                f
                for f in fences
                if heading.line_index < f.start < section_end and f.info == "mermaid"
            ),
            None,
        )
        if block is None:
            errors.append(f"`{name}`: section has no ```mermaid block.")
            continue
        if block.end is None:
            errors.append(f"`{name}`: ```mermaid block is never closed.")
            continue
        source = "\n".join(lines[block.start + 1 : block.end]).strip()
        if not source:
            errors.append(f"`{name}`: ```mermaid block is empty.")
            continue
        diagrams[name] = source

    return diagrams, errors


def load_pipeline_diagrams(candidates: Sequence[Path] | None = None) -> PipelineDiagrams:
    """Resolve, read and parse the generated pipeline-diagram document.

    Never raises for a missing or malformed document; the failure is carried
    in ``PipelineDiagrams.errors`` for the page to display.
    """
    searched = tuple(candidate_paths() if candidates is None else candidates)
    path = resolve_path(searched)
    if path is None:
        return PipelineDiagrams(
            source_path=None,
            searched=searched,
            errors=(f"{PIPELINE_DIAGRAMS_FILENAME} was not found on any candidate path.",),
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return PipelineDiagrams(
            source_path=path,
            searched=searched,
            errors=(f"{path} could not be read: {type(exc).__name__}.",),
        )
    diagrams, errors = extract_diagrams(text)
    return PipelineDiagrams(source_path=path, searched=searched, diagrams=diagrams, errors=tuple(errors))


def diagram_height(source: str) -> int:
    """Iframe height for a diagram: grows with its line count, bounded so a
    large state machine scrolls instead of pushing the page apart."""
    return max(360, min(1400, 160 + 26 * len(source.splitlines())))
