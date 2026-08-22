# Working in this repository

This file is the single source of rules for coding agents. Claude Code reads it through
`CLAUDE.md`; Codex and OpenHands read it directly. Skills live in `.agents/skills/` and are
visible to all three (`.claude/skills` is a symlink). If something only applies to one agent,
say so explicitly here rather than in a second file.

## What this project is

Grumpycat turns production errors into draft pull requests and grooms them to green. Read
`README.md` first, then `docs/architecture.md`. The plugin contract in
`src/grumpycat/plugins/spec.py` is the most important file in the tree: inputs, engines and
outputs all implement it, and third parties build against it.

## Ground rules

- **Python 3.13+**, `uv` for everything (`uv sync`, `uv run ...`). Never `pip install` into
  the environment, never edit `uv.lock` by hand.
- **Type-checked and linted**: `uv run ruff check src tests`, `uv run ruff format src tests`,
  `uv run mypy`. All three must pass before you consider a change done. mypy runs in strict
  mode; do not add `# type: ignore` without a reason in the comment.
- **Pre-commit** hooks (`uv run pre-commit install`) run ruff and mypy on commit; never bypass
  them with `--no-verify`.
- **Tests**: `uv run pytest`. New behaviour needs a test. Webhook parsers are tested against
  recorded payloads in `tests/fixtures/` — scrub anything personal before committing a
  fixture (emails, IPs, user ids, cookies, auth headers).
- **Models cross boundaries, dicts don't.** Anything handed between Lambda, Step Functions
  and the worker is a pydantic model from `src/grumpycat/core/models.py`. Add fields there;
  don't pass loose dicts.
- **Plugins declare, the registry enforces.** A plugin lists `required_secrets` and
  `optional_tools` in its `PluginSpec`; it must not read `os.environ` for credentials or shell
  out to a CLI it did not declare. Built-in and contrib plugins must never add a CLI
  dependency to the worker base image — CLIs belong in the client's runtime image.
- **No client names.** This is a public repo deployed by several organisations. Do not
  commit company names, account ids, hostnames, monitor ids, Slack channel ids, or anything
  that identifies a deployment — not in code, tests, fixtures, comments, commit messages or
  PR text. Use `acme` / `example.test` in examples.
- **Grumpycat never merges and never runs a repo's test suite.** If you are tempted to add
  either, stop and open an issue instead.
- **Secrets are names, not values.** Configuration refers to SSM/Secrets Manager ARNs; values
  are injected at runtime by the Terraform module. Never log a secret, never write one to
  disk.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs the same three commands as the skill above
on Python 3.13 and 3.14. The `ok` job is the single required status. Keep the workflow free of
anything organisation-specific; this is a public repository.

## Layout

```
src/grumpycat/core       models, config loading, dedupe, severity, brief rendering
src/grumpycat/plugins    the contract (spec.py) and the registry
src/grumpycat/inputs     built-in inputs (sentry, datadog, ...)
src/grumpycat/engines    built-in engines (claude, codex, ...)
src/grumpycat/outputs    built-in outputs (github, slack, ...)
src/grumpycat/ci         built-in CI readers (buildkite, github_actions) used by the groomer
src/grumpycat/contrib    optional in-tree plugins shipped as extras
src/grumpycat/handlers    Lambda handlers (edges only; no long work here)
src/grumpycat/worker     Fargate entrypoint (clone → engine → commit → push → callback)
statemachine/            Step Functions definition
docker/                  Lambda, worker and example client Dockerfiles
docs/                    architecture, plugin authoring, config reference, runbook
```

## Conventions

- Commit messages: `type(scope): imperative summary` (`feat(inputs): add datadog log monitors`).
  Body explains *why* and what a reviewer can't see in the diff.
- PR descriptions: What / Why it matters / Tests / Not in scope. No process narration.
- Logging: `aws_lambda_powertools.Logger` in Lambdas, stdlib `logging` elsewhere, structured
  fields rather than f-strings with secrets.
- Errors meant for operators use `PluginError` / `RuntimeError` with a message that says what
  to change (`needs secret X in the secrets map`), not a stack trace.

## Things that look wrong but are deliberate

- `Exception_` in `core/models.py` has a trailing underscore to avoid shadowing the builtin.
- `load_config` accepts a path, a YAML string, or `$GRUMPYCAT_CONFIG` holding either — the
  module passes the document through SSM → environment.
- Entry-point groups are declared in `pyproject.toml` even when empty, so third-party plugin
  authors can see the group names.
