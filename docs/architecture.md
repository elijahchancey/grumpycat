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
