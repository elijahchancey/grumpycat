# Configuration reference — `grumpycat.yaml`

Supplied to the Terraform module as `config_yaml`; validated by `grumpycat.core.config.Config`.
Unknown keys are rejected everywhere.

```yaml
client: acme                       # [a-z0-9-], used in names and metrics
inputs:                            # plugin name → that plugin's own config
  sentry: { org: acme, projects: [api] }
  datadog: { site: datadoghq.com }
engines:                           # optional per-engine config; engines are also pulled in by repos.*.engine
  claude: { auth: api_key }        # api_key | bedrock | subscription
  codex: { auth: api_key }         # api_key | chatgpt
outputs:
  github: { draft: true, needs_human_label: needs-human, request_reviewers_on_ready: [] }
  slack: { channel: C0123456, oncall_channel: C0654321 }   # channel ids; bot must be a member
ci:                                # how to read the target repos' CI (one provider per deployment)
  provider: buildkite              # buildkite | github_actions | <any installed ci plugin>
  options: { org: acme }
repos:
  acme/api:                        # owner/name
    engine: claude                 # claude | codex | <any installed engine>
    model: claude-sonnet-5
    default_branch: master
    ci_pipeline: acme/api          # pipeline id in the CI provider, for log fetching
    prepare: "rbenv local 3.4.2"   # optional, runs before the engine
    services: [api, api-worker]    # alert service names that map here (default: repo name)
    labels: [grumpycat]
policy:
  page_when: { env: prod, level_fatal: true, users_15m: 50 }
  confidence_min: 0.6              # below this: RCA to Slack, no PR
  max_attempts: 3                  # groom iterations before needs-human
  prs_per_day: 10
  cooldown_hours: 72               # after a closed-unmerged PR
  gated: true                      # Slack approval before every fix run
  freeze: false                    # triage only
reviewer_allowlist: [cursor[bot], some-human]
```

Secrets and the worker image are **module inputs**, not part of this file.

## Secrets the built-in plugins need

| Plugin | Secret map keys |
|---|---|
| `sentry` | `SENTRY_WEBHOOK_SECRET`, `SENTRY_AUTH_TOKEN` |
| `datadog` | `DD_WEBHOOK_TOKEN`, `DD_API_KEY`, `DD_APP_KEY` |
| `claude` | `auth: api_key` → `ANTHROPIC_API_KEY`; `auth: bedrock` → task-role creds; `auth: subscription` → `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) |
| `codex` | `auth: api_key` → `OPENAI_API_KEY`; `auth: chatgpt` → `CODEX_AUTH_JSON` (contents of `~/.codex/auth.json` after `codex login`) |
| `github` (and the worker) | `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`; the hook also needs `GITHUB_WEBHOOK_SECRET` |
| `slack` | `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` |
| `buildkite` (ci) | `BUILDKITE_API_TOKEN` |

Anything else in the map is passed through to the engine run as environment variables, for
the repository's own skills and MCP servers.

### Subscription vs API billing

Both engines can run on a personal plan instead of per-token billing: Claude Code via a
long-lived OAuth token (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`), Codex via the
ChatGPT login file (`codex login` → `~/.codex/auth.json` → `CODEX_AUTH_JSON`). Both are tied
to **one person's** account and rate limits, and the audit trail shows that person, not the
deployment — fine for a solo deployment, wrong for an organisation whose spend should land on
its own key or AWS account. Refresh tokens rotate; if runs start failing with auth errors,
re-export the secret.
