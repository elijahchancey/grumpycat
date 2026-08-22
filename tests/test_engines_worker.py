from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest

from grumpycat.core import github_auth
from grumpycat.core.config import load_config
from grumpycat.core.models import (
    Brief,
    EngineResult,
    Evidence,
    RepoTarget,
    Severity,
    TaskKind,
    Triage,
    WorkerTask,
)
from grumpycat.engines import _cli
from grumpycat.engines.claude import ClaudeConfig, ClaudeEngine
from grumpycat.engines.codex import CodexConfig, CodexEngine, _usage_from_jsonl
from grumpycat.plugins import PluginKind, Registry, build
from grumpycat.plugins.spec import EnginePlugin
from grumpycat.worker import main as worker
from tests.conftest import make_event

SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "OPENAI_API_KEY": "sk-test",
    "SENTRY_AUTH_TOKEN": "s",
}


def sh(cmd: str, cwd: Path) -> str:
    return subprocess.run(
        cmd, shell=True, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def make_task(kind: TaskKind = TaskKind.FIX, engine: str = "claude", **target: Any) -> WorkerTask:
    ev = make_event(title="NoMethodError: undefined method `email' for nil")
    brief = Brief(
        event=ev,
        evidence=Evidence(),
        triage=Triage(severity=Severity.HIGH, confidence=0.7, page=False, rationale="t"),
        target=RepoTarget(full_name="acme/api", engine=engine, default_branch="main", **target),
    )
    return WorkerTask(kind=kind, brief=brief, branch="grumpycat/fake-abc", task_token="tok")


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH dir where tests drop fake `claude` / `codex` scripts."""
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return d


def script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one commit, used as the engine's working directory."""
    r = tmp_path / "work"
    r.mkdir()
    sh("git init -q -b main && git config user.email t@example.test && git config user.name t", r)
    (r / "app.rb").write_text("def deliver(u)\n  u.email\nend\n")
    sh("git add . && git commit -qm init", r)
    return r


# -- claude engine ------------------------------------------------------------------------


def test_claude_argv_encodes_guardrails() -> None:
    eng = ClaudeEngine(ClaudeConfig(model="claude-sonnet-5"), SECRETS)
    argv = eng.argv(make_task(model=None))
    assert argv[:3] == ["claude", "-p", pytest.approx(argv[2])]  # prompt is second arg
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--max-turns") + 1] == "60"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert "Bash(bundle exec rspec*)" in settings["permissions"]["deny"]
    assert "Bash(git push*)" in settings["permissions"]["deny"]
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Edit" in allowed and "Bash(git diff *)" in allowed and "Bash(rspec*)" not in allowed
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    # repo-level model wins over engine default
    assert ClaudeEngine(ClaudeConfig(model="x"), SECRETS).argv(make_task(model="y"))[-1] == "y"


def test_claude_preflight_and_auth_modes(fake_bin: Path) -> None:
    eng = ClaudeEngine(ClaudeConfig(binary="claude-not-installed"), {})
    problems = eng.preflight(Path("."))
    assert any("not on PATH" in p for p in problems) and any(
        "ANTHROPIC_API_KEY" in p for p in problems
    )
    script(fake_bin / "claude", "exit 0")
    assert ClaudeEngine(ClaudeConfig(), SECRETS).preflight(Path(".")) == []
    bed = ClaudeEngine(ClaudeConfig(auth="bedrock"), {"AWS_REGION": "us-east-1"})
    assert bed.preflight(Path(".")) == []
    env = bed._env()
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1" and "ANTHROPIC_API_KEY" not in env
    assert ClaudeEngine(ClaudeConfig(), SECRETS)._env()["SENTRY_AUTH_TOKEN"] == "s"  # passthrough


def test_claude_subscription_auth(fake_bin: Path) -> None:
    script(fake_bin / "claude", "exit 0")
    sub = ClaudeEngine(ClaudeConfig(auth="subscription"), {})
    assert any("CLAUDE_CODE_OAUTH_TOKEN" in p for p in sub.preflight(Path(".")))
    both = {"CLAUDE_CODE_OAUTH_TOKEN": "oat", "ANTHROPIC_API_KEY": "sk", "SENTRY_AUTH_TOKEN": "s"}
    sub = ClaudeEngine(ClaudeConfig(auth="subscription"), both)
    assert sub.preflight(Path(".")) == []
    env = sub._env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat" and "ANTHROPIC_API_KEY" not in env
    assert env["SENTRY_AUTH_TOKEN"] == "s"
    # and the reverse: api_key mode never forwards the OAuth token
    env = ClaudeEngine(ClaudeConfig(auth="api_key"), both)._env()
    assert env["ANTHROPIC_API_KEY"] == "sk" and "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_claude_run_parses_json_detects_changes_and_excludes_our_files(
    fake_bin: Path, repo: Path
) -> None:
    script(
        fake_bin / "claude",
        """
        test -f .grumpycat/BRIEF.md || { echo "no brief" >&2; exit 3; }
        grep -q "Production error" .grumpycat/BRIEF.md || exit 4
        printf 'def deliver(u)\\n  u&.email\\nend\\n' > app.rb
        mkdir -p spec && echo "it works" > spec/deliver_spec.rb
        echo "some noise line"
        printf '{"type":"result","result":"Added safe navigation and a regression test",'
        printf '"total_cost_usd":0.42,"num_turns":7,"session_id":"s1","is_error":false}\\n'
        """,
    )
    eng = ClaudeEngine(ClaudeConfig(), SECRETS)
    res = eng.run(make_task(), repo, "# Production error to fix\n")
    assert res.changed is True
    assert res.summary == "Added safe navigation and a regression test"
    assert res.cost_usd == 0.42 and res.turns == 7
    assert sorted(res.raw["changed_paths"]) == ["app.rb", "spec/deliver_spec.rb"]
    assert ".grumpycat/BRIEF.md" not in res.raw["changed_paths"]
    assert "GRUMPYCAT_REPORT.md" not in sh("git status --porcelain", repo)
    assert ".grumpycat/" in (repo / ".git/info/exclude").read_text()


def test_claude_decline_writes_report_and_no_changes(fake_bin: Path, repo: Path) -> None:
    script(
        fake_bin / "claude",
        """
        echo "Could not reproduce; see notes" > GRUMPYCAT_REPORT.md
        echo '{"result":"declined","total_cost_usd":0.05}'
        """,
    )
    res = ClaudeEngine(ClaudeConfig(), SECRETS).run(make_task(), repo, "brief")
    assert res.changed is False
    assert res.report is not None and "Could not reproduce" in res.report
    assert res.summary.startswith("Could not reproduce")
    assert _cli.changed_paths(repo) == []


def test_claude_timeout_and_garbage_output(fake_bin: Path, repo: Path) -> None:
    script(fake_bin / "claude", "sleep 5")
    eng = ClaudeEngine(ClaudeConfig(timeout_minutes=1), SECRETS)
    eng.config.__dict__["timeout_minutes"] = 1
    import grumpycat.engines.claude as mod

    orig = mod._cli.run_cli
    mod._cli.run_cli = lambda argv, **kw: orig(argv, **{**kw, "timeout_s": 1})  # type: ignore[assignment]
    try:
        res = eng.run(make_task(), repo, "brief")
    finally:
        mod._cli.run_cli = orig  # type: ignore[assignment]
    assert res.changed is False and res.summary == "engine timed out"
    script(fake_bin / "claude", "echo not-json; exit 1")
    res = ClaudeEngine(ClaudeConfig(), SECRETS).run(make_task(), repo, "brief")
    assert res.changed is False and res.cost_usd is None and res.raw["is_error"] is True


# -- codex engine -------------------------------------------------------------------------


def test_codex_argv_and_usage_parsing() -> None:
    eng = CodexEngine(CodexConfig(model="o4-mini", network=True), SECRETS)
    argv = eng.argv(make_task(engine="codex"), Path("/tmp/last.txt"))
    assert argv[:3] == ["codex", "exec", "--json"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in argv
    assert argv[argv.index("-m") + 1] == "o4-mini"
    assert argv[-1].startswith("Read `.grumpycat/BRIEF.md`")
    jsonl = "\n".join(
        [
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "done"},
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                }
            ),
            json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 5}}
            ),
            "not json",
        ]
    )
    assert _usage_from_jsonl(jsonl) == {"input_tokens": 150, "output_tokens": 25, "turns": 2}


def test_codex_auth_modes(fake_bin: Path, repo: Path) -> None:
    script(fake_bin / "codex", "exit 0")
    assert any("OPENAI_API_KEY" in p for p in CodexEngine(CodexConfig(), {}).preflight(Path(".")))
    chat = CodexEngine(CodexConfig(auth="chatgpt"), {"CODEX_AUTH_JSON": "not json"})
    problems = chat.preflight(Path("."))
    assert any("not valid JSON" in p for p in problems)
    auth = json.dumps({"tokens": {"access_token": "a", "refresh_token": "r"}})
    chat = CodexEngine(
        CodexConfig(auth="chatgpt"),
        {"CODEX_AUTH_JSON": auth, "OPENAI_API_KEY": "sk", "SENTRY_AUTH_TOKEN": "s"},
    )
    assert chat.preflight(Path(".")) == []
    home = chat._prepare_home(repo)
    assert (home / "auth.json").read_text() == auth
    assert oct((home / "auth.json").stat().st_mode & 0o777) == "0o600"
    env = chat._env(home)
    assert env["CODEX_HOME"] == str(home)
    assert "OPENAI_API_KEY" not in env and "CODEX_AUTH_JSON" not in env
    assert env["SENTRY_AUTH_TOKEN"] == "s"
    env = CodexEngine(CodexConfig(), SECRETS)._env(home)
    assert env["OPENAI_API_KEY"] == "sk-test"


def test_codex_chatgpt_run_cleans_up_home(fake_bin: Path, repo: Path) -> None:
    script(
        fake_bin / "codex",
        """
        test -f "$CODEX_HOME/auth.json" || { echo "no auth" >&2; exit 7; }
        test -z "$OPENAI_API_KEY" || { echo "key leaked" >&2; exit 8; }
        echo ok >> app.rb
        """,
    )
    eng = CodexEngine(
        CodexConfig(auth="chatgpt"), {"CODEX_AUTH_JSON": "{}", "OPENAI_API_KEY": "sk"}
    )
    res = eng.run(make_task(engine="codex"), repo, "brief")
    assert res.changed is True and res.raw["returncode"] == 0
    assert not (repo / ".grumpycat" / "codex-home").exists()


def test_codex_run(fake_bin: Path, repo: Path) -> None:
    script(
        fake_bin / "codex",
        """
        # find --output-last-message path
        while [ $# -gt 0 ]; do
          case "$1" in --output-last-message) shift; LAST="$1";; esac; shift
        done
        echo "fixed" >> app.rb
        echo "Added nil guard" > "$LAST"
        printf '{"type":"item.completed","item":{"type":"agent_message","text":"Added nil guard"},'
        printf '"usage":{"input_tokens":10,"output_tokens":2}}\n'
        """,
    )
    res = CodexEngine(CodexConfig(), SECRETS).run(make_task(engine="codex"), repo, "brief")
    assert res.changed is True and res.summary == "Added nil guard"
    assert res.cost_usd is None and res.turns == 1 and res.raw["usage"]["input_tokens"] == 10
    assert CodexEngine(CodexConfig(), SECRETS).preflight(Path(".")) == []


def test_engines_are_discoverable() -> None:
    assert isinstance(
        build(PluginKind.ENGINE, "claude", {}, SECRETS, cls=EnginePlugin), ClaudeEngine
    )
    assert isinstance(build(PluginKind.ENGINE, "codex", {}, SECRETS, cls=EnginePlugin), CodexEngine)


# -- github app auth ----------------------------------------------------------------------


def test_installation_token_flow() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["auth"] = req.headers["Authorization"]
        return httpx.Response(201, json={"token": "ghs_abc"})

    secrets = {
        "GITHUB_APP_ID": "12",
        "GITHUB_APP_INSTALLATION_ID": "34",
        "GITHUB_APP_PRIVATE_KEY": pem,
    }
    tok = github_auth.installation_token(secrets, transport=httpx.MockTransport(handler))
    assert tok == "ghs_abc" and seen["path"] == "/app/installations/34/access_tokens"
    assert seen["auth"].startswith("Bearer ey")
    assert (
        github_auth.clone_url("acme/api", tok)
        == "https://x-access-token:ghs_abc@github.com/acme/api.git"
    )
    with pytest.raises(RuntimeError, match="needs secrets"):
        github_auth.installation_token({})


# -- worker end to end --------------------------------------------------------------------


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare 'origin' with main + one commit, like a real remote."""
    seed = tmp_path / "seed"
    seed.mkdir()
    sh(
        "git init -q -b main && git config user.email t@example.test && git config user.name t",
        seed,
    )
    (seed / "app.rb").write_text("def deliver(u)\n  u.email\nend\n")
    (seed / "AGENTS.md").write_text("# rules\n")
    sh("git add . && git commit -qm init", seed)
    bare = tmp_path / "origin.git"
    sh(f"git clone -q --bare {seed} {bare}", tmp_path)
    return bare


def registry_with(engine: str, fake_entry_points: Any, binary: str | None = None) -> Registry:
    from tests import fakes

    fake_entry_points(
        PluginKind.ENGINE,
        {"claude": ClaudeEngine, "codex": CodexEngine, "fake_engine": fakes.FakeEngine},
    )
    engines = f"engines:\n  {engine}: {{binary: {binary}}}\n" if binary else ""
    cfg = load_config(f"client: acme\n{engines}repos:\n  acme/api: {{engine: {engine}}}\n")
    return Registry(cfg, SECRETS)


def test_worker_fix_run_pushes_branch_and_reports(
    fake_bin: Path, origin: Path, tmp_path: Path, fake_entry_points: Any
) -> None:
    script(
        fake_bin / "claude",
        """
        printf 'def deliver(u)\\n  u&.email\\nend\\n' > app.rb
        echo '{"result":"Safe navigation","total_cost_usd":0.3,"num_turns":3}'
        """,
    )
    task = make_task(prepare="echo prepared > .grumpycat/prepared.txt")
    out = worker.run(
        task,
        "# Production error to fix\n",
        secrets=SECRETS,
        registry=registry_with("claude", fake_entry_points),
        clone_url=str(origin),
        root=tmp_path / "w",
    )
    assert out.status == "pushed" and out.branch == "grumpycat/fake-abc" and out.cost_usd == 0.3
    log = sh("git log --format=%s%n%b grumpycat/fake-abc -1", origin)
    assert log.startswith("fix: NoMethodError: undefined method `email' for nil")
    assert "Fingerprint: fake:1" in log and "Safe navigation" in log
    files = sh("git ls-tree --name-only -r grumpycat/fake-abc", origin).split()
    assert (
        "app.rb" in files
        and ".grumpycat/BRIEF.md" not in files
        and "GRUMPYCAT_REPORT.md" not in files
    )
    assert "u&.email" in sh("git show grumpycat/fake-abc:app.rb", origin)


def test_worker_declines_without_pushing(
    fake_bin: Path, origin: Path, tmp_path: Path, fake_entry_points: Any
) -> None:
    script(fake_bin / "claude", 'echo "nope" > GRUMPYCAT_REPORT.md; echo \'{"result":"declined"}\'')
    out = worker.run(
        make_task(),
        "brief",
        secrets=SECRETS,
        registry=registry_with("claude", fake_entry_points),
        clone_url=str(origin),
        root=tmp_path / "w",
    )
    assert out.status == "declined" and "nope" in out.summary
    assert "grumpycat/fake-abc" not in sh("git branch", origin)


def test_worker_groom_run_continues_existing_branch(
    fake_bin: Path, origin: Path, tmp_path: Path, fake_entry_points: Any
) -> None:
    # first push
    script(fake_bin / "claude", 'echo v1 > fix.txt; echo \'{"result":"v1"}\'')
    worker.run(
        make_task(),
        "b",
        secrets=SECRETS,
        registry=registry_with("claude", fake_entry_points),
        clone_url=str(origin),
        root=tmp_path / "w1",
    )
    # groom iteration sees v1 and adds to it
    script(
        fake_bin / "claude",
        'test -f fix.txt || exit 9; echo v2 >> fix.txt; echo \'{"result":"v2"}\'',
    )
    task = make_task(kind=TaskKind.GROOM).model_copy(update={"attempt": 2, "pr_number": 7})
    out = worker.run(
        task,
        "b",
        secrets=SECRETS,
        registry=registry_with("claude", fake_entry_points),
        clone_url=str(origin),
        root=tmp_path / "w2",
    )
    assert out.status == "pushed" and out.pr_number == 7
    assert sh("git rev-list --count main..grumpycat/fake-abc", origin).strip() == "2"
    assert sh("git log -1 --format=%s grumpycat/fake-abc", origin).startswith(
        "fix: address CI/review feedback (attempt 2)"
    )


def test_worker_fails_cleanly_on_preflight_or_prepare(
    fake_bin: Path, origin: Path, tmp_path: Path, fake_entry_points: Any
) -> None:
    with pytest.raises(worker.WorkerError, match="preflight"):
        worker.run(
            make_task(),
            "b",
            secrets=SECRETS,
            registry=registry_with("claude", fake_entry_points, binary="claude-not-installed"),
            clone_url=str(origin),
            root=tmp_path / "w1",
        )
    script(fake_bin / "claude", "exit 0")
    with pytest.raises(worker.WorkerError, match="prepare command failed"):
        worker.run(
            make_task(prepare="false"),
            "b",
            secrets=SECRETS,
            registry=registry_with("claude", fake_entry_points),
            clone_url=str(origin),
            root=tmp_path / "w2",
        )


def test_worker_main_sends_task_success(
    fake_bin: Path,
    origin: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_entry_points: Any,
) -> None:
    from unittest.mock import MagicMock

    script(fake_bin / "claude", 'echo x > y.txt; echo \'{"result":"ok","total_cost_usd":0.1}\'')
    task = make_task()
    monkeypatch.setenv("GRUMPYCAT_TASK", task.model_dump_json())
    monkeypatch.setenv("GRUMPYCAT_BRIEF_MD", "# brief")
    monkeypatch.setenv("GRUMPYCAT_CONFIG", "client: acme\nrepos:\n  acme/api: {engine: claude}\n")
    monkeypatch.setenv("GRUMPYCAT_SECRET_ARNS", "{}")
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(worker, "load_secrets", lambda: SECRETS)
    monkeypatch.setattr(github_auth, "installation_token", lambda s: "tok")
    monkeypatch.setattr(github_auth, "clone_url", lambda repo, tok: str(origin))
    sfn = MagicMock()
    monkeypatch.setattr(worker.boto3, "client", lambda name: sfn)
    registry_with("claude", fake_entry_points)  # registers the entry point fakes
    assert worker.main() == 0
    out = json.loads(sfn.send_task_success.call_args.kwargs["output"])
    assert (
        out["status"] == "pushed" and sfn.send_task_success.call_args.kwargs["taskToken"] == "tok"
    )

    script(fake_bin / "claude", "exit 0")
    monkeypatch.setenv("GRUMPYCAT_TASK", make_task(prepare="false").model_dump_json())
    assert worker.main() == 1
    assert "prepare command failed" in sfn.send_task_failure.call_args.kwargs["cause"]


def test_commit_message_shape() -> None:
    res = EngineResult(changed=True, summary="did things")
    msg = worker.commit_message(make_task(), res)
    assert msg.splitlines()[0] == "fix: NoMethodError: undefined method `email' for nil"
    assert "Generated by grumpycat" in msg
