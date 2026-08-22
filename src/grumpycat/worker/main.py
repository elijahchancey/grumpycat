"""Fargate worker entrypoint.

Input (container overrides set by the state machine):
  GRUMPYCAT_TASK       WorkerTask JSON incl. `task_token`
  GRUMPYCAT_BRIEF_MD   the rendered brief
  GRUMPYCAT_CONFIG*    as for the Lambdas; secrets are injected natively via the task def

Sequence: mint GitHub token → clone (shallow) → checkout (deployed SHA / default branch, or
the existing PR branch for a shepherd run) → optional `prepare` → engine.preflight → engine.run
→ commit exactly what changed (never the brief or the report) → push → SendTaskSuccess with a
`FixOutcome`. Any failure → SendTaskFailure, which the state machine routes to needs_human.

Opening the PR is the GitHub *output's* job (on the PR_OPEN transition), so this process
never needs PR-level API code — only a push-capable token.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import boto3

from grumpycat.core import github_auth
from grumpycat.core.config import load_config
from grumpycat.core.models import EngineResult, FixOutcome, TaskKind, WorkerTask
from grumpycat.core.secrets import load_secrets
from grumpycat.engines import _cli
from grumpycat.plugins import Registry

log = logging.getLogger("grumpycat.worker")

GIT_USER = ("grumpycat[bot]", "grumpycat[bot]@users.noreply.github.com")


class WorkerError(RuntimeError):
    pass


def git(args: Sequence[str], cwd: Path, *, env: Mapping[str, str] | None = None) -> str:
    p = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    if p.returncode != 0:
        msg = f"git {' '.join(args[:2])} failed: {p.stderr.strip()[:500]}"
        raise WorkerError(msg)
    return p.stdout


class Workspace:
    def __init__(self, clone_url: str, task: WorkerTask, root: Path) -> None:
        self.clone_url = clone_url
        self.task = task
        self.dir = root / "repo"
        root.mkdir(parents=True, exist_ok=True)

    def checkout(self) -> str:
        """Return the SHA the engine will work from."""
        t = self.task
        target = t.brief.target
        git(
            [
                "clone",
                "--quiet",
                "--depth",
                "100",
                "--no-single-branch",
                self.clone_url,
                str(self.dir),
            ],
            self.dir.parent,
        )
        git(["config", "user.name", GIT_USER[0]], self.dir)
        git(["config", "user.email", GIT_USER[1]], self.dir)
        _cli.ensure_scratch(self.dir)
        if t.kind is TaskKind.SHEPHERD:
            git(["fetch", "--quiet", "origin", t.branch], self.dir)
            git(["checkout", "--quiet", "-B", t.branch, f"origin/{t.branch}"], self.dir)
            return git(["rev-parse", "HEAD"], self.dir).strip()
        base = target.default_branch
        sha = t.brief.evidence.deployed_sha
        if sha:
            try:
                git(["fetch", "--quiet", "--depth", "1", "origin", sha], self.dir)
                git(["checkout", "--quiet", "-B", t.branch, sha], self.dir)
                return sha
            except WorkerError:
                log.warning("deployed sha %s not fetchable; using %s", sha, base)
        git(["checkout", "--quiet", "-B", t.branch, f"origin/{base}"], self.dir)
        return git(["rev-parse", "HEAD"], self.dir).strip()

    def prepare(self, env: Mapping[str, str]) -> None:
        cmd = self.task.brief.target.prepare
        if not cmd:
            return
        res = _cli.run_cli(["sh", "-lc", cmd], cwd=self.dir, env=env, timeout_s=15 * 60)
        if res.returncode != 0:
            msg = f"prepare command failed ({res.returncode}): {res.stderr[-500:]}"
            raise WorkerError(msg)

    def commit_and_push(self, message: str) -> str | None:
        """Stage everything the engine changed (never our files), commit, push. None if nothing."""
        changed = _cli.changed_paths(self.dir)
        if not changed:
            return None
        # .grumpycat/ and the report are in .git/info/exclude (see _cli.write_brief)
        git(["add", "--all"], self.dir)
        git(["commit", "--quiet", "-m", message], self.dir)
        git(["push", "--quiet", "-u", "origin", self.task.branch], self.dir)
        return git(["rev-parse", "HEAD"], self.dir).strip()


def commit_message(task: WorkerTask, result: EngineResult) -> str:
    e = task.brief.event
    if task.kind is TaskKind.SHEPHERD:
        head = f"fix: address CI/review feedback (attempt {task.attempt})"
    else:
        head = f"fix: {e.title[:60]}"
    body = [
        "",
        result.summary[:1500],
        "",
        f"Source: {e.source} {e.url or ''}".rstrip(),
        f"Fingerprint: {e.fingerprint}",
        "Generated by grumpycat; see the pull request for the brief and review trail.",
    ]
    return head + "\n" + "\n".join(body)


def outcome_for(task: WorkerTask, result: EngineResult, pushed_sha: str | None) -> FixOutcome:
    if pushed_sha is None:
        status = "declined" if (result.report or not result.raw.get("is_error")) else "failed"
        return FixOutcome(
            status=status, summary=result.summary, cost_usd=result.cost_usd, branch=task.branch
        )
    return FixOutcome(
        status="pushed",
        summary=result.summary,
        cost_usd=result.cost_usd,
        branch=task.branch,
        pr_number=task.pr_number,
    )


def run(
    task: WorkerTask,
    brief_md: str,
    *,
    secrets: Mapping[str, str],
    registry: Registry,
    clone_url: str,
    root: Path,
) -> FixOutcome:
    ws = Workspace(clone_url, task, root)
    sha = ws.checkout()
    log.info("checked out %s at %s on %s", task.brief.target.full_name, sha[:10], task.branch)
    engine = registry.engine(task.brief.target.engine)
    problems = engine.preflight(ws.dir)
    if problems:
        msg = "engine preflight failed: " + "; ".join(problems)
        raise WorkerError(msg)
    ws.prepare(dict(secrets))
    result = engine.run(task, ws.dir, brief_md)
    log.info(
        "engine finished: changed=%s cost=%s turns=%s",
        result.changed,
        result.cost_usd,
        result.turns,
    )
    pushed = ws.commit_and_push(commit_message(task, result)) if result.changed else None
    return outcome_for(task, result, pushed)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    task = WorkerTask.model_validate_json(os.environ["GRUMPYCAT_TASK"])
    brief_md = os.environ.get("GRUMPYCAT_BRIEF_MD") or ""
    config = load_config(os.environ.get("GRUMPYCAT_CONFIG") or _config_from_ssm())
    secrets = load_secrets()
    registry = Registry(config, secrets)
    sfn = boto3.client("stepfunctions")
    token = task.task_token
    root = Path(tempfile.mkdtemp(prefix="grumpycat-"))
    try:
        url = github_auth.clone_url(
            task.brief.target.full_name, github_auth.installation_token(secrets)
        )
        outcome = run(task, brief_md, secrets=secrets, registry=registry, clone_url=url, root=root)
        log.info("outcome: %s", outcome.status)
        if token:
            sfn.send_task_success(taskToken=token, output=outcome.model_dump_json())
        return 0
    except Exception as e:
        log.exception("worker failed")
        if token:
            sfn.send_task_failure(taskToken=token, error=type(e).__name__, cause=str(e)[:2000])
        return 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _config_from_ssm() -> str:
    param = os.environ["GRUMPYCAT_CONFIG_PARAM"]
    value: Any = boto3.client("ssm").get_parameter(Name=param, WithDecryption=True)["Parameter"][
        "Value"
    ]
    return str(value)


if __name__ == "__main__":
    sys.exit(main())
