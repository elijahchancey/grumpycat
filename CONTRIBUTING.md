# Contributing to Grumpycat

Thanks for looking. This document covers how to get a change in, how the plugin system works
for people writing plugins, and what we will and won't accept.

## Getting set up

```sh
git clone https://github.com/elijahchancey/grumpycat
cd grumpycat
uv sync
uv run pytest
```

You need Python 3.13+ and [`uv`](https://docs.astral.sh/uv/). Nothing else is required for
unit tests. Integration tests (marked `integration`) need real credentials and are skipped by
default.

## The quality gate

Every PR must pass, in CI and locally:

```sh
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run pytest
```

mypy runs strict. ruff enforces import order, annotations and a security ruleset. If you use
Claude Code or Codex to work on this repo, they read `AGENTS.md` and will run this for you;
still say in the PR which checks you ran.

## Changes we take

- Bug fixes with a regression test.
- New input / engine / output plugins that follow the contract below and carry tests against
  recorded payloads.
- Documentation that makes a real deployment easier.
- Refactors that are motivated by a concrete next change, not taste.

## Changes we don't take

- Anything that lets Grumpycat **merge** a PR or **run a repository's test suite** locally.
  Both are deliberate non-goals: CI is the judge, humans merge.
- CLI dependencies added to the worker base image on behalf of a plugin. CLIs belong in the
  client's runtime image (`docker/client.example.Dockerfile`).
- Organisation-specific anything: names, ids, hostnames, channel ids. Use `acme` in examples
  and scrub fixtures.
- Credentials read from `os.environ` inside a plugin. Declare `required_secrets` instead.

## Writing a plugin

A plugin is a Python class with a `spec: PluginSpec` and a constructor `(config, secrets)`.
The registry discovers it through an entry point, validates its config section against
`spec.config_schema`, checks every `required_secrets` name exists in the secrets map, warns
about any `optional_tools` missing from `PATH`, and only then constructs it. Read
`src/grumpycat/plugins/spec.py`; it is short and it is the contract.

### The four kinds

| Kind | Base class | Must implement | Entry-point group |
|---|---|---|---|
| Input | `InputPlugin` | `parse(payload) -> ErrorEvent \| None`, `enrich(event) -> Evidence`; HTTP inputs also `verify(headers, body)` | `grumpycat.inputs` |
| Engine | `EnginePlugin` | `run(task, workdir, brief_md) -> EngineResult` | `grumpycat.engines` |
| Output | `OutputPlugin` | `on_transition(state, previous, brief) -> IssueState` | `grumpycat.outputs` |
| CI | `CIPlugin` | `fetch_failure(repo, sha, context, target_url) -> CIFailure` | `grumpycat.ci` |

Inputs declare how they are triggered: `Trigger.HTTP` (a `POST /in/<name>` route the Terraform
module exposes; you verify the signature) or `Trigger.EVENTBRIDGE` (you supply an
`event_pattern`; the module creates the rule). An input's `parse` must be pure — no network —
so it can run in the router Lambda; `enrich` may call the source API and must scrub PII.

### Registering it

In your package's `pyproject.toml`:

```toml
[project.entry-points."grumpycat.inputs"]
pagerduty = "grumpycat_pagerduty:PagerDutyInput"
```

The entry-point name must equal `spec.name`, and `spec.api_version` must match
`grumpycat.PLUGIN_API_VERSION` (currently 1). The registry rejects mismatches with a message
that says so.

### Where it lives — three tiers

1. **Built-in** (`src/grumpycat/{inputs,engines,outputs}/`): sources and targets most
   deployments have. Added by core maintainers; always in the published images.
2. **Contrib** (`src/grumpycat/contrib/<name>/`): optional, in-tree, shipped as an extra
   (`pip install grumpycat[<name>]`) and included in the published images. Good home for a
   community plugin that passes CI. Must not require CLIs in the base image.
3. **Out-of-tree**: your own pip package. Users build `FROM grumpycat-worker` /
   `grumpycat-lambda`, `pip install` it, and point the Terraform module at their image. No
   changes to Grumpycat needed.

Open an issue before starting a built-in or contrib plugin so we can agree on scope; go
straight to a PR for bug fixes.

### Testing a plugin

- Record a real payload, scrub it, put it in `tests/fixtures/<plugin>/`. That fixture is the
  spec for `parse`.
- Test `verify` with a good signature *and* a bad one.
- Test that `required_secrets` / `optional_tools` are declared (the registry tests cover the
  enforcement; yours cover the declaration).
- For engines, test `run` against a fake CLI on `PATH` (see `tests/fakes.py` for the pattern),
  never against the real agent.

## Pull requests

- One change per PR; keep it reviewable.
- Title: `type(scope): what it does` (`feat(inputs): datadog log monitors`).
- Body: **What**, **Why it matters**, **Tests** (name them), **Not in scope**. Skip headings
  you have nothing real to put under. No narration of how you got here.
- CI is GitHub Actions (`.github/workflows/ci.yml`): lint, types and tests on Python 3.13 and
  3.14. A green `ok` check and one maintainer approval merges it; maintainers merge,
  bots don't.

## Releases

Semver tags (`vX.Y.Z`) build and publish the Lambda and worker images to ECR Public and are
recorded in `CHANGELOG.md` with image digests. The Terraform module is versioned separately
and pins a Grumpycat version it was tested with.

## Code of conduct

Be kind, be specific, assume good faith. Disagreements about design go in the issue, not the
PR review; disagreements about correctness go in the PR with a failing test.
