"""Codex engine: `codex exec` in the checkout. Codex loads the repo's AGENTS.md hierarchy.

Guardrails: Codex's own OS-level sandbox (`--sandbox workspace-write`, no network by
default), a turn/time budget, and the brief's instructions. Codex has no per-command deny
list like Claude Code, so test-runner avoidance is instruction-based here; the worker still
never commits anything the brief forbade.

Auth (`auth:` in the engine config):
  api_key  OPENAI_API_KEY in the secrets map — billed per token.
  chatgpt  CODEX_AUTH_JSON in the secrets map: the contents of `~/.codex/auth.json` after
           `codex login` on a ChatGPT Plus/Pro/Team/Enterprise plan. Written to a private
           CODEX_HOME for the run and removed afterwards. Tied to one person's plan and its
           limits; refresh tokens rotate, so expect to re-export it occasionally.
Usage (tokens) is reported from the JSONL event stream; cost in USD is not computed because
list prices vary by model and plan.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from grumpycat.core.models import EngineResult, WorkerTask
from grumpycat.engines import _cli
from grumpycat.plugins.spec import EnginePlugin, PluginKind, PluginSpec


class CodexConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    auth: Literal["api_key", "chatgpt"] = "api_key"
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    timeout_minutes: int = Field(default=40, ge=1, le=180)
    network: bool = Field(default=False, description="Allow network inside the sandbox")
    extra_args: list[str] = Field(default_factory=list)
    binary: str = "codex"


PROMPT = (
    f"Read `{_cli.BRIEF_FILE}` in this repository and do exactly what it says. Start by reading "
    "the repository's AGENTS.md and any guidelines it references. Do not run the full test "
    "suite and do not commit; leave your changes in the working tree."
)


class CodexEngine(EnginePlugin):
    spec = PluginSpec(
        name="codex",
        kind=PluginKind.ENGINE,
        config_schema=CodexConfig,
        required_secrets=(),  # OPENAI_API_KEY or CODEX_AUTH_JSON; checked in preflight
        optional_tools=("codex",),
    )
    config: CodexConfig

    def preflight(self, workdir: Path) -> list[str]:
        problems = []
        if _cli.which(self.config.binary) is None:
            problems.append(f"`{self.config.binary}` not on PATH in the worker image")
        key = "CODEX_AUTH_JSON" if self.config.auth == "chatgpt" else "OPENAI_API_KEY"
        if not self.secrets.get(key):
            problems.append(f"{key} missing from the secrets map (auth: {self.config.auth})")
        if self.config.auth == "chatgpt":
            try:
                json.loads(self.secrets.get("CODEX_AUTH_JSON") or "{}")
            except json.JSONDecodeError:
                problems.append("CODEX_AUTH_JSON is not valid JSON (export ~/.codex/auth.json)")
        return problems

    def _env(self, codex_home: Path) -> dict[str, str]:
        env = {k: v for k, v in self.secrets.items() if k != "CODEX_AUTH_JSON"}
        env["CODEX_HOME"] = str(codex_home)
        if self.config.auth == "chatgpt":
            env.pop("OPENAI_API_KEY", None)  # an API key would take precedence over the login
        else:
            env["OPENAI_API_KEY"] = self.secrets["OPENAI_API_KEY"]
        return env

    def _prepare_home(self, workdir: Path) -> Path:
        """A per-run CODEX_HOME so nothing persists between runs and no shared ~/.codex is
        touched. With `auth: chatgpt` the exported auth.json is written there (0600)."""
        home = _cli.ensure_scratch(workdir) / "codex-home"
        home.mkdir(mode=0o700, exist_ok=True)
        if self.config.auth == "chatgpt":
            auth = home / "auth.json"
            auth.write_text(self.secrets["CODEX_AUTH_JSON"])
            auth.chmod(0o600)
        return home

    def argv(self, task: WorkerTask, last_message: Path) -> list[str]:
        model = task.brief.target.model or self.config.model
        argv = [
            self.config.binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            self.config.sandbox,
            "--output-last-message",
            str(last_message),
        ]
        if self.config.network:
            argv += ["-c", "sandbox_workspace_write.network_access=true"]
        if model:
            argv += ["-m", model]
        return [*argv, *self.config.extra_args, PROMPT]

    def run(self, task: WorkerTask, workdir: Path, brief_md: str) -> EngineResult:
        _cli.write_brief(workdir, brief_md)
        last = workdir / _cli.BRIEF_DIR / "last_message.txt"
        home = self._prepare_home(workdir)
        try:
            res = _cli.run_cli(
                self.argv(task, last),
                cwd=workdir,
                env=self._env(home),
                timeout_s=self.config.timeout_minutes * 60,
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)
        usage = _usage_from_jsonl(res.stdout)
        text = last.read_text() if last.exists() else _last_agent_message(res.stdout)
        changed = _cli.changed_paths(workdir)
        report = _cli.read_report(workdir)
        base = _cli.summarize(res, changed=changed, report=report, text=text)
        raw: dict[str, Any] = {
            "returncode": res.returncode,
            "usage": usage,
            "stderr_tail": res.stderr[-2000:],
            "changed_paths": changed,
        }
        return base.model_copy(update={"cost_usd": None, "turns": usage.get("turns"), "raw": raw})


def _events(stdout: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _usage_from_jsonl(stdout: str) -> dict[str, Any]:
    """Sum token usage across events; tolerate both `usage` and `info.usage` shapes."""
    totals: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    for ev in _events(stdout):
        usage = ev.get("usage") or (ev.get("info") or {}).get("usage") or {}
        if isinstance(usage, dict):
            totals["input_tokens"] += int(usage.get("input_tokens") or 0)
            totals["output_tokens"] += int(usage.get("output_tokens") or 0)
        if (
            ev.get("type") in {"turn.completed", "item.completed"}
            or ev.get("msg", {}).get("type") == "agent_message"
        ):
            totals["turns"] += 1
    return totals


def _last_agent_message(stdout: str) -> str:
    for ev in reversed(_events(stdout)):
        item = ev.get("item") or ev.get("msg") or {}
        if isinstance(item, dict) and item.get("type") in {"agent_message", "message"}:
            text = item.get("text") or item.get("message") or ""
            if text:
                return str(text)
    return ""
