# Architecture

The long-form design, landscape survey, prior art and diagrams live in the design brief that
this repo was bootstrapped from; this page is the operational summary and will grow as the
pieces land.

## Runtime split

| Where | What | Why |
|---|---|---|
| **Lambda** (edges) | webhook router, triage, GitHub event handler, Slack interactions, daily digest | short, bursty, stateless |
| **Step Functions** | one execution per error fingerprint | execution history *is* the debugging UI; timeouts and retries built in |
| **ECS Fargate** | one task per fix / shepherd run: clone → agent → commit → push → callback | agent sessions run 5–40 min, need a writable disk and the client's toolchain; Lambda's 15-min cap and image limits rule it out |
| **DynamoDB** | `IssueState` by fingerprint; PR number → execution lookup; attempt and cost counters | one open issue per fingerprint, enforced with a conditional put |

## Flow

1. Source → `POST /in/<input>` (HTTP) or EventBridge rule → router Lambda. The input plugin
   verifies the signature and parses the payload into an `ErrorEvent` (pure, no network).
2. Triage Lambda: conditional put on the fingerprint (dedupe, cool-down), `enrich` (source API,
   scrubbed), severity + confidence, paging policy → Slack on-call channel, gated approval if
   configured, then start the state machine.
3. Fix task: checkout at the deployed SHA (or default branch), optional `prepare`, render the
   brief, run the engine with test runners denied, commit, push `grumpycat/<fingerprint>`,
   open a draft PR, link it back on the source issue, post the Slack thread, callback.
4. Wait for GitHub events. `status` / `check_run` failure → the `ci` plugin fetches the failing job log →
   shepherd task. Review/issue comments from allow-listed actors → shepherd task. Green and no
   open threads → ready for review. Attempts exhausted → `needs-human`.
5. Digest Lambda on a schedule: opened / merged / escalated / cost, per repo.

## Images

- `grumpycat-lambda` and `grumpycat-worker` are published to Amazon ECR Public. The Terraform
  module creates a credential-free pull-through cache so Lambda can use the same-account ECR
  copy it requires.
- Each deployment builds **one** runtime image `FROM grumpycat-worker` with every toolchain
  and CLI its repositories' skills need. Grumpycat's base contains only git, gh, node + claude,
  codex and jq.

## Loop breakers

Actor allow-list on GitHub events; the bot ignores its own comments; events tagged
`service:grumpycat` are dropped at ingress; one execution per fingerprint; gated mode requires
a Slack approval before any fix run.

## Lambda environment

| Variable | Set by | Used by |
|---|---|---|
| `GRUMPYCAT_CONFIG_PARAM` (or inline `GRUMPYCAT_CONFIG`) | module | all handlers |
| `GRUMPYCAT_SECRET_ARNS` — JSON `{ENV_VAR: arn}` (SSM or Secrets Manager) | module | all handlers |
| `GRUMPYCAT_TABLE` | module | all handlers |
| `GRUMPYCAT_STATE_MACHINE` | module | triage, Slack approve |
| `GRUMPYCAT_TRIAGE_FUNCTION` | module | router |
| `GRUMPYCAT_BOT_LOGIN` (default `grumpycat[bot]`) | module | github hook |

Handlers: `router` (`/in/{input}` + EventBridge), `triage`, `github_hook` (`/hooks/github`),
`slack_interactions` (`/slack/interactions`, approve/dismiss buttons), `lifecycle`
(`park` / `after_run` / `finalize`, called by the state machine), `digest` (EventBridge
schedule, `{"hours": 24}`; posts a per-repo summary to the Slack channel). The worker gets
`GRUMPYCAT_TASK` (a `WorkerTask` JSON, including the Step Functions task token) and
`GRUMPYCAT_BRIEF_MD` as container overrides.

## DynamoDB

Single table, `pk` hash key, two GSIs: `branch` (hash `branch` = `owner/name#branch`) and
`opened_on` (hash `opened_on` = `owner/name#YYYY-MM-DD`, keys-only). Items: `ISSUE#<fingerprint>`
(state document, `wait_token`, `pending_events`) and `PR#<owner/name>#<number>` → fingerprint.

## Engines and the worker

The worker clones the target repo (at the deployed SHA when the source reports one), runs an
optional per-repo `prepare` command, writes the brief to `.grumpycat/BRIEF.md` (excluded from
git), and runs the engine in that checkout:

| Engine | Invocation | Rule files | Guardrails |
|---|---|---|---|
| `claude` | `claude -p … --permission-mode dontAsk --max-turns N --allowedTools … --settings {deny: […]}` | `CLAUDE.md`, `.claude/rules`, skills, hooks, `.mcp.json` | allow-list + deny-list (test runners, commit, push, network), turn cap, timeout |
| `codex` | `codex exec --json --sandbox workspace-write …` | `AGENTS.md` | OS sandbox (no network by default), timeout, instructions |

Engines never commit. The worker stages everything except `.grumpycat/` and
`GRUMPYCAT_REPORT.md`, commits as `grumpycat[bot]`, pushes `grumpycat/<id>` and reports a
`FixOutcome` to Step Functions. A run that changes nothing is `declined` (with the report) and
ends the execution; the GitHub output opens the draft PR on the `PR_OPEN` transition.

## Outputs

Outputs react to state transitions (`OutputPlugin.on_transition(state, previous, brief)`):

| Transition | `github` | `slack` |
|---|---|---|
| `awaiting_approval` | — | message with **Open a fix PR / Dismiss** buttons (gated mode) |
| page (any status, once) | — | post to `oncall_channel` |
| `pr_open` (no PR yet) | open draft PR from the pushed branch, label | thread reply with the link |
| `shepherding` (new push) | comment what was pushed; reply to + resolve the review threads it addressed; attach the engine report | thread reply |
| `ready` | mark ready for review, request reviewers, comment | thread reply |
| `needs_human` | `needs-human` label + reason | thread reply |
| `rca_only` | — | summary with the reason |
| `merged` / `closed` | — | thread reply |

The input that reported the error gets a note with the PR link (`InputPlugin.annotate`)
the first time a PR exists.
