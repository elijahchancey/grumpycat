"""Shared plumbing for CLI-based engines: run a subprocess in the checkout, detect what it
changed, read its decline report."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from grumpycat.core.models import EngineResult

BRIEF_DIR = ".grumpycat"
BRIEF_FILE = f"{BRIEF_DIR}/BRIEF.md"
REPORT_FILE = "GRUMPYCAT_REPORT.md"
# Paths the worker never commits and the engine never "changes" from our point of view.
IGNORED_PATHS = (BRIEF_DIR + "/", REPORT_FILE)


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_cli(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: int,
    stdin: str | None = None,
) -> RunResult:
    """Run with a clean environment: only PATH/HOME basics plus what the engine declared."""
    base = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(cwd)),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "CI": "1",
    }
    try:
        p = subprocess.run(  # noqa: S603 - argv is built by us, never from input
            list(argv),
            cwd=cwd,
            env={**base, **env},
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return RunResult(returncode=-1, stdout=str(out), stderr=str(err), timed_out=True)
    return RunResult(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)


def which(name: str) -> str | None:
    return shutil.which(name)


def ensure_scratch(workdir: Path) -> Path:
    """Create `.grumpycat/` (scratch for prepare/engines) and keep it and the report out of git."""
    scratch = workdir / BRIEF_DIR
    scratch.mkdir(parents=True, exist_ok=True)
    exclude = workdir / ".git" / "info" / "exclude"
    if exclude.parent.exists():
        existing = exclude.read_text() if exclude.exists() else ""
        lines = [line for line in (BRIEF_DIR + "/", REPORT_FILE) if line not in existing]
        if lines:
            exclude.write_text(existing.rstrip("\n") + "\n" + "\n".join(lines) + "\n")
    return scratch


def write_brief(workdir: Path, brief_md: str) -> Path:
    ensure_scratch(workdir)
    path = workdir / BRIEF_FILE
    path.write_text(brief_md)
    return path


def changed_paths(workdir: Path) -> list[str]:
    """Tracked and untracked changes, minus our own files."""
    p = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],  # noqa: S607
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    out: list[str] = []
    for line in p.stdout.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith(IGNORED_PATHS) or path == REPORT_FILE:
            continue
        out.append(path)
    return out


def read_report(workdir: Path) -> str | None:
    path = workdir / REPORT_FILE
    return path.read_text()[:20000] if path.exists() else None


def summarize(
    result: RunResult, *, changed: list[str], report: str | None, text: str
) -> EngineResult:
    """Common EngineResult shape; engines add cost/turns/raw on top."""
    if result.timed_out:
        return EngineResult(changed=bool(changed), summary="engine timed out", report=report)
    if changed:
        summary = text.strip()[:2000] or f"changed {len(changed)} file(s)"
        return EngineResult(changed=True, summary=summary, report=report)
    return EngineResult(
        changed=False,
        summary=(report or text or "engine made no changes").strip()[:2000],
        report=report,
    )
