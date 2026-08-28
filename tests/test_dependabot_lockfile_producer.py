"""The compiled lockfile has exactly one producer, and it is not Dependabot.

alpha-engine-config-I9060. `requirements.txt` here is a `uv pip compile`
artifact of `requirements.in`, proven so on every PR by
`lockfile-reproducible` (landed by crucible-dashboard#774). Dependabot's pip
ecosystem edits the compiled file and knows nothing about the source file, so
every pip PR it opened failed that guard before a dependency was graded — and
Dependabot PRs are a STANDING auto-merge exception, so the fleet believed
this repo had automated Python dependency maintenance while it had none. The
human workaround already cost deploys: crucible-dashboard#778 moved
requirements.in by hand because "every deploy has been failing since #775".

These tests assert the BEHAVIOUR that keeps that closed:

  1. no pip ecosystem may point Dependabot at a directory holding a
     `requirements.in` (npm is untouched — package-lock.json is a lock npm
     maintains from package.json, and Dependabot moves both together);
  2. a producer exists at the canonical path the fleet sweep discovers;
  3. the producer and the verifier issue the SAME compile — proven by
     capturing the actual `uv` argv of both, not by reading the files.

(3) is the one that matters most: a verifier and a producer each carrying
their own copy of the flags drift into a lockfile the guard can never pass,
which is the same dead-exception failure in a new costume. All three of this
repo's compile flags are load-bearing (see
infrastructure/check_lock_reproducible.sh), so the drift surface is real.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
PRODUCER = ROOT / ".github" / "upgrade_lock.sh"
FLAGS = ROOT / ".github" / "lockfile_compile.sh"
VERIFIER = ROOT / "infrastructure" / "check_lock_reproducible.sh"


def _pip_updates() -> list[dict]:
    cfg = yaml.safe_load(DEPENDABOT.read_text()) or {}
    return [u for u in (cfg.get("updates") or [])
            if u.get("package-ecosystem") == "pip"]


def test_no_pip_dependabot_entry_targets_a_compiled_lockfile():
    """A pip entry whose directory holds a requirements.in is the defect."""
    offenders = []
    for update in _pip_updates():
        dirs = update.get("directories") or [update.get("directory", "/")]
        for d in dirs:
            if (ROOT / d.lstrip("/") / "requirements.in").exists():
                offenders.append(d)
    assert not offenders, (
        "Dependabot's pip ecosystem is pointed at "
        f"{offenders}, where requirements.txt is compiled from "
        "requirements.in. Dependabot edits the compiled file and cannot "
        "recompile it, so `lockfile-reproducible` fails every such PR by "
        "construction (alpha-engine-config-I9060). Upgrades belong to "
        ".github/upgrade_lock.sh."
    )


def test_producer_exists_at_the_canonical_path_and_is_executable():
    """`lockfile-upgrade-sweep` in alpha-engine-config discovers the producer
    by this exact path; a repo with a requirements.in and no producer has no
    dependency updates at all."""
    assert (ROOT / "requirements.in").exists()
    assert PRODUCER.exists(), f"{PRODUCER} is missing — nothing upgrades this lockfile"
    assert PRODUCER.stat().st_mode & stat.S_IXUSR, f"{PRODUCER} is not executable"


def test_producer_rejects_an_unknown_argument():
    """Fail loud: an unrecognised flag must not be silently ignored and then
    silently write requirements.txt."""
    proc = subprocess.run(["bash", str(PRODUCER), "--nope"],
                          capture_output=True, text=True)
    assert proc.returncode == 2, proc.stderr


def _capture_uv_argv(tmp_path: Path, script: Path, args: list[str]) -> list[str]:
    """Run `script` with a stub `uv` first on PATH that records its argv and
    leaves the --output-file untouched, then return the recorded argv."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "argv"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "uv"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{log}"\n'
        f'printf "%s" "${{UV_CUSTOM_COMPILE_COMMAND:-}}" > "{log}.header"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    child = dict(os.environ)
    child["PATH"] = f"{stub_dir}{os.pathsep}{os.environ['PATH']}"
    subprocess.run(["bash", str(script), *args], env=child,
                   capture_output=True, text=True, check=False)
    assert log.exists(), f"{script} never invoked uv"
    return log.read_text().splitlines()


def _normalise(argv: list[str]) -> list[str]:
    """argv with the two deliberate differences removed: --upgrade (producer
    only, by definition) and the temp output path / --quiet noise."""
    out: list[str] = []
    skip = False
    for tok in argv:
        if skip:
            skip = False
            continue
        if tok in {"--quiet", "--upgrade"}:
            continue
        if tok == "--output-file":
            out.extend(["--output-file", "<tmp>"])
            skip = True
            continue
        out.append(tok)
    return out


def test_producer_and_verifier_compile_with_identical_flags(tmp_path):
    """The invariant, measured on the real argv both scripts hand to uv.

    They must agree on the input file and every resolution flag. They differ
    in exactly two deliberate ways: the verifier passes no --upgrade (it
    seeds with the committed lock and asks whether today's constraints still
    hold those pins), and each may pass --quiet. Any other difference means a
    produced lockfile the guard cannot verify.
    """
    prod = _capture_uv_argv(tmp_path / "p", PRODUCER, ["--check"])
    ver = _capture_uv_argv(tmp_path / "v", VERIFIER, [])

    assert _normalise(prod) == _normalise(ver), (
        "the lockfile producer and the lockfile verifier issue different "
        "`uv pip compile` invocations:\n"
        f"  producer: {prod}\n  verifier: {ver}\n"
        "Both must source .github/lockfile_compile.sh and pass "
        "LOCK_COMPILE_FLAGS unchanged."
    )
    assert "--upgrade" in prod, "the producer must upgrade; it is the producer"
    assert "--upgrade" not in ver, (
        "the verifier must NOT upgrade — a seeded compile is what makes it "
        "fail for a reason someone in this repo caused"
    )


def test_the_flag_parity_check_fails_on_a_seeded_drift(tmp_path):
    """The check above is only worth having if it fails on the condition it
    exists for. Seed the drift — a producer that rolls its own flags instead
    of sourcing the shared file — and assert the comparison rejects it."""
    fake = tmp_path / "repo"
    fake.mkdir()
    shutil.copytree(ROOT / ".github", fake / ".github")
    (fake / "infrastructure").mkdir()
    shutil.copy(VERIFIER, fake / "infrastructure" / VERIFIER.name)
    for name in (".python-version", "requirements.in", "requirements.txt"):
        shutil.copy(ROOT / name, fake / name)

    producer = fake / ".github" / "upgrade_lock.sh"
    producer.write_text(producer.read_text().replace(
        'lockfile_compile "$OUT" --upgrade --quiet',
        'uv pip compile "$ROOT/requirements.in" --output-file "$OUT" '
        '--python 3.99 --upgrade --quiet',
    ))

    prod = _capture_uv_argv(tmp_path / "dp", producer, ["--check"])
    ver = _capture_uv_argv(tmp_path / "dv", fake / "infrastructure" / VERIFIER.name, [])
    assert _normalise(prod) != _normalise(ver), (
        "a producer pinned to Python 3.99 while the verifier uses the SSoT "
        "interpreter was not detected — the parity check proves nothing"
    )


@pytest.mark.parametrize("workflow", sorted(
    p.name for p in (ROOT / ".github" / "workflows").glob("*.yml")))
def test_workflows_have_no_duplicate_keys(workflow):
    """`yaml.safe_load` silently keeps the LAST of duplicate keys, while
    GitHub startup-fails the workflow at parse — a workflow that has never
    once executed, invisible to any checker built on safe_load
    (alpha-engine-config-I8729). Parse strictly instead."""

    class Strict(yaml.SafeLoader):
        pass

    def no_dupes(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            assert key not in seen, f"duplicate key {key!r} in {workflow}"
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dupes)
    # Driven through the loader directly rather than `yaml.load(...,
    # Loader=Strict)`: `Strict` subclasses SafeLoader and only ADDS a
    # duplicate-key assertion, but the `yaml.load` spelling trips ruff S506
    # on the loader NAME alone.
    loader = Strict((ROOT / ".github" / "workflows" / workflow).read_text())
    try:
        loader.get_single_data()
    finally:
        loader.dispose()


def test_the_compile_header_is_pinned_to_a_path_free_command(tmp_path):
    """uv stamps the invoking command line into the lockfile header. Left
    unpinned it records the absolute checkout and temp paths of whatever
    produced the file, so requirements.txt churns on every run and differs
    between a laptop and a runner — a diff carrying no dependency
    information, on the one file a reviewer is supposed to read.

    Assert the producer pins UV_CUSTOM_COMPILE_COMMAND, and that it is
    exactly the header the committed lockfile already carries.
    """
    log_dir = tmp_path / "p"
    _capture_uv_argv(log_dir, PRODUCER, ["--check"])
    header = (log_dir / "argv.header").read_text()
    assert header, "the producer did not pin UV_CUSTOM_COMPILE_COMMAND"
    pathless = header.replace("requirements.in", "").replace("requirements.txt", "")
    assert "/" not in pathless, f"the pinned header carries a filesystem path: {header!r}"

    committed = (ROOT / "requirements.txt").read_text().splitlines()
    stamped = [ln for ln in committed[:6]
               if ln.startswith("#") and "uv pip compile" in ln]
    assert stamped, "requirements.txt carries no uv provenance header"
    assert stamped[0].lstrip("# ").strip() == header.strip(), (
        "the committed lockfile header and the header the producer would "
        f"write disagree:\n  committed: {stamped[0]!r}\n  producer:  {header!r}")
