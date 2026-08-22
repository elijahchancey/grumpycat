# Grumpycat

Production errors in, draft pull requests out.

Grumpycat listens for errors from your observability stack (Sentry, Datadog, AWS events),
decides whether each one looks like a code defect, and — if it does — runs a coding agent
(Claude Code or Codex) in a checkout of the right repository. The agent follows that repo's
own `AGENTS.md` / `CLAUDE.md` / rules / skills, writes a minimal fix and a regression test,
and opens a **draft** PR. Grumpycat then **grooms** the PR — like a cat, it keeps working at
it until it is clean: it watches CI, reads review-bot
and human comments, pushes follow-up commits until the build is green and no review thread
is open, and hands off to a human when it is stuck. It never merges.

It runs in **your** AWS account from a single Terraform module, and it is built from
components that are free for personal and commercial use.

> Status: pre-alpha. The plugin contract is stable enough to build against; the rest is
> being assembled in the open. See the issues for what is and isn't there yet.

## How it works

```
 Sentry / Datadog / EventBridge ──▶ API Gateway + Lambda ──▶ triage (dedupe, severity,
                                                              confidence, paging policy)
                                                                  │
                                                   Step Functions execution per error
                                                                  │
                                   ECS Fargate task: clone ▸ agent ▸ commit ▸ push ▸ draft PR
                                                                  │
                           GitHub events (CI status, reviews, comments) ──▶ groom runs
                                                                  │
                                          green + 0 open threads ──▶ ready for review
                                          attempt budget spent   ──▶ needs-human + Slack
```

Grumpycat does **not** run your test suite. It commits, pushes, and follows your CI
(Buildkite or GitHub Actions, through a `ci` plugin) for the verdict. It also does not query your observability tools from
inside the fix: the agent uses the skills and MCP servers already in your repo, with the
CLIs your runtime image provides.

## Plugins

Four axes, all discovered through Python entry points:

| Axis | Built in | Planned |
|---|---|---|
| Inputs | `sentry`, `datadog` (Error Tracking issues, metric/APM monitors, log monitors) | `ecs_task` (EventBridge), generic webhook, Keep |
| Engines | `claude`, `codex` | `openhands` |
| Outputs | `github`, `slack` | `gitlab`, Datadog On-Call |
| CI (reads the target repos' build logs) | `buildkite`, `github_actions` | — |

Writing one is a class with a `spec: PluginSpec` and three methods. See
[`docs/plugins.md`](docs/plugins.md).

## Deploying

Use the Terraform module:
[`elijahchancey/terraform-aws-grumpycat`](https://github.com/elijahchancey/terraform-aws-grumpycat).
You supply a `grumpycat.yaml` (which repos, which engine per repo, severity policy), a map of
secrets (SSM or Secrets Manager ARNs), and one runtime image built `FROM` the published
`grumpycat-worker` image with the toolchains your repos need.

Images are published to Amazon ECR Public:

```
public.ecr.aws/<alias>/grumpycat-lambda:vX.Y.Z
public.ecr.aws/<alias>/grumpycat-worker:vX.Y.Z
```

See [`docs/config.md`](docs/config.md) for the configuration reference and
[`docs/architecture.md`](docs/architecture.md) for the long version.

## Developing

```sh
uv sync
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run pytest
```

Python 3.13+ (3.14 in the published images). See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
