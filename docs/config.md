# Configuration reference — `grumpycat.yaml`

Supplied to the Terraform module as `config_yaml`; validated by `grumpycat.core.config.Config`.
Unknown keys are rejected everywhere.

```yaml
client: acme                       # [a-z0-9-], used in names and metrics
inputs:                            # plugin name → that plugin's own config
  sentry: { org: acme, projects: [api] }
  datadog: { site: datadoghq.com }
engines: {}                        # optional per-engine config; engines are also pulled in by repos.*.engine
outputs:
  github: {}
  slack: { channel: "#grumpycat", oncall_channel: "#oncall" }
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
  max_attempts: 3                  # shepherd iterations before needs-human
  prs_per_day: 10
  cooldown_hours: 72               # after a closed-unmerged PR
  gated: true                      # Slack approval before every fix run
  freeze: false                    # triage only
reviewer_allowlist: [cursor[bot], some-human]
```

Secrets and the worker image are **module inputs**, not part of this file.
