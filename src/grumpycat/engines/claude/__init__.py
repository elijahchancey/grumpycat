"""Claude Code engine: `claude -p` in the checkout, non-bare so the repo's CLAUDE.md, rules,
skills, hooks and .mcp.json apply exactly as they do for a human.

Guardrails are enforced by Claude Code itself, not by prompt alone:
  --permission-mode dontAsk       anything not allowed is denied, never prompted
  --allowedTools <list>           read/edit/search plus a read-only git subset
  --settings {"permissions": {"deny": [...]}}   test runners, commit, push, network tools
  --max-turns N                   hard cap on iterations

Auth: ANTHROPIC_API_KEY in the secrets map, or `auth: bedrock` (task-role credentials via
CLAUDE_CODE_USE_BEDROCK=1). Cost comes back in the JSON result (`total_cost_usd`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from grumpycat.core.models import EngineResult, WorkerTask
from grumpycat.engines import _cli
from grumpycat.plugins.spec import EnginePlugin, PluginKind, PluginSpec

DEFAULT_ALLOWED = [
    "Read",
    "Edit",
    "Write",
    "MultiEdit",
    "Glob",
    "Grep",
    "LS",
    "Bash(git diff *)",
    "Bash(git log *)",
    "Bash(git status *)",
    "Bash(git show *)",
    "Bash(git blame *)",
    "Bash(ls *)",
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(rg *)",
    "Bash(grep *)",
    "Bash(find *)",
    "Bash(wc *)",
]
DEFAULT_DENIED = [
    "Bash(git commit*)",
    "Bash(git push*)",
    "Bash(git reset*)",
    "Bash(git checkout*)",
    "Bash(git switch*)",
    "Bash(bundle exec rspec*)",
    "Bash(rspec*)",
    "Bash(bin/rails test*)",
    "Bash(bundle exec rails test*)",
    "Bash(pytest*)",
    "Bash(python -m pytest*)",
    "Bash(uv run pytest*)",
    "Bash(npm test*)",
    "Bash(yarn test*)",
    "Bash(pnpm test*)",
    "Bash(npx jest*)",
    "Bash(npx vitest*)",
    "Bash(go test*)",
    "Bash(cargo test*)",
    "Bash(mix test*)",
    "Bash(make test*)",
    "Bash(curl *)",
    "Bash(wget *)",
    "WebFetch",
    "WebSearch",
]


class ClaudeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, description="Default model; repos may override")
    auth: Literal["api_key", "bedrock"] = "api_key"
    max_turns: int = Field(default=60, ge=1, le=500)
    timeout_minutes: int = Field(default=40, ge=1, le=180)
    allowed_tools: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED))
    denied_tools: list[str] = Field(default_factory=lambda: list(DEFAULT_DENIED))
    extra_args: list[str] = Field(default_factory=list)
    binary: str = "claude"


PROMPT = (
    f"Read `{_cli.BRIEF_FILE}` in this repository and do exactly what it says. "
    "Start by reading the repository's agent guidelines. Work in the working tree only; "
    "do not commit."
)


class ClaudeEngine(EnginePlugin):
    spec = PluginSpec(
        name="claude",
        kind=PluginKind.ENGINE,
        config_schema=ClaudeConfig,
        required_secrets=(),  # api key OR bedrock; checked in preflight
        optional_tools=("claude",),
    )
    config: ClaudeConfig

    def preflight(self, workdir: Path) -> list[str]:
        problems = []
        if _cli.which(self.config.binary) is None:
            problems.append(f"`{self.config.binary}` not on PATH in the worker image")
        if self.config.auth == "api_key" and not self.secrets.get("ANTHROPIC_API_KEY"):
            problems.append("ANTHROPIC_API_KEY missing from the secrets map (auth: api_key)")
        return problems

    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        if self.config.auth == "bedrock":
            env["CLAUDE_CODE_USE_BEDROCK"] = "1"
            for k in ("AWS_REGION", "AWS_DEFAULT_REGION"):
                if k in self.secrets:
                    env[k] = self.secrets[k]
        else:
            env["ANTHROPIC_API_KEY"] = self.secrets["ANTHROPIC_API_KEY"]
        # Pass through everything else the deployment mapped (tokens for the repo's own skills
        # and MCP servers); the engine never sees AWS task-role creds unless mapped.
        for k, v in self.secrets.items():
            env.setdefault(k, v)
        return env

    def argv(self, task: WorkerTask) -> list[str]:
        model = task.brief.target.model or self.config.model
        settings = json.dumps({"permissions": {"deny": self.config.denied_tools}})
        argv = [
            self.config.binary,
            "-p",
            PROMPT,
            "--permission-mode",
            "dontAsk",
            "--max-turns",
            str(self.config.max_turns),
            "--output-format",
            "json",
            "--allowedTools",
            ",".join(self.config.allowed_tools),
            "--settings",
            settings,
        ]
        if model:
            argv += ["--model", model]
        return argv + list(self.config.extra_args)

    def run(self, task: WorkerTask, workdir: Path, brief_md: str) -> EngineResult:
        _cli.write_brief(workdir, brief_md)
        res = _cli.run_cli(
            self.argv(task),
            cwd=workdir,
            env=self._env(),
            timeout_s=self.config.timeout_minutes * 60,
        )
        payload = _parse_json(res.stdout)
        changed = _cli.changed_paths(workdir)
        report = _cli.read_report(workdir)
        text = str(payload.get("result") or "") if payload else res.stdout[-2000:]
        base = _cli.summarize(res, changed=changed, report=report, text=text)
        cost = payload.get("total_cost_usd") if payload else None
        turns = payload.get("num_turns") if payload else None
        raw: dict[str, Any] = {
            "returncode": res.returncode,
            "is_error": bool(payload.get("is_error")) if payload else res.returncode != 0,
            "session_id": payload.get("session_id") if payload else None,
            "stderr_tail": res.stderr[-2000:],
            "changed_paths": changed,
        }
        return base.model_copy(
            update={
                "cost_usd": float(cost) if isinstance(cost, int | float) else None,
                "turns": int(turns) if isinstance(turns, int) else None,
                "raw": raw,
            }
        )


def _parse_json(stdout: str) -> dict[str, Any] | None:
    """`--output-format json` prints one object; tolerate leading noise."""
    text = stdout.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        start = text.rfind("\n{")
        if start >= 0:
            try:
                obj = json.loads(text[start + 1 :])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
        return None
