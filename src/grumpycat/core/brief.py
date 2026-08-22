"""Render a `Brief` to the markdown an engine reads. Kept boring on purpose: the engine
follows the repo's own rules for *how* to work; this tells it *what* happened and *where*."""

from __future__ import annotations

from grumpycat.core.models import Brief, CIFailure, Transition

INSTRUCTIONS_FIX = """\
## Instructions

Follow this repository's agent guidelines (AGENTS.md / CLAUDE.md and their rules and skills)
before anything else. Then:

1. Find the root cause of the error above. Use the repository's own skills and tools to
   query observability data if you need more than what is here.
2. Fix it minimally. Do not refactor unrelated code. Do not change behaviour beyond the fix.
3. Add a test that fails without the fix and passes with it. Do not weaken or delete existing
   tests.
4. **Do not run the full test suite.** CI runs it after you push. You may run a single
   focused test file if the repository's guidelines allow it.
5. Do not commit. The orchestrator commits and pushes what you leave in the working tree.
6. If you cannot reproduce or fix this safely, make **no code changes** and write
   `GRUMPYCAT_REPORT.md` at the repository root explaining what you found and what a human
   should look at.
"""

INSTRUCTIONS_SHEPHERD = """\
## Instructions

This branch already contains a fix for the error above and is under review. Address the
feedback below, following the repository's agent guidelines:

- Make CI pass without weakening the regression test or any existing test. If the failure
  is unrelated to this change (flaky, infrastructure), say so in `GRUMPYCAT_REPORT.md` and
  change nothing.
- For review comments: apply the ones you agree with; for any you decline, explain why in
  `GRUMPYCAT_REPORT.md` under the comment's text so the orchestrator can reply.
- Do not commit; the orchestrator commits and pushes.
"""


def _code(text: str | None, limit: int = 6000) -> str:
    if not text:
        return "_none_"
    t = text if len(text) <= limit else text[:limit] + "\n…(truncated)"
    return f"```\n{t}\n```"


def render_brief(
    brief: Brief, *, ci_failure: CIFailure | None = None, findings: list[str] | None = None
) -> str:
    e, ev, t = brief.event, brief.evidence, brief.triage
    kind = "Regression" if e.transition is Transition.REGRESSION else "Production error"
    lines = [
        f"# {kind} to fix — {e.title}",
        "",
        f"- Source: **{e.source}**  ·  Fingerprint: `{e.fingerprint}`",
        f"- Service: `{e.service or '?'}`  ·  Env: `{e.env or '?'}`  ·  "
        f"Occurred: {e.occurred_at.isoformat()}",
        f"- Severity: **{t.severity}**  ·  Confidence it's a code defect: "
        f"{t.confidence:.2f} ({t.rationale})",
    ]
    if ev.first_seen or ev.event_count or ev.user_count:
        lines.append(
            f"- First seen: {ev.first_seen.isoformat() if ev.first_seen else '?'}  ·  "
            f"Events: {ev.event_count or '?'}  ·  Users: {ev.user_count or '?'}"
        )
    if ev.deployed_sha:
        lines.append(f"- Deployed SHA when it happened: `{ev.deployed_sha}` (this checkout)")
    if brief.previous_pr_url:
        lines.append(f"- Previous fix for this issue: {brief.previous_pr_url} — read it first")
    if e.url:
        lines.append(f"- Link: {e.url}")
    for name, url in ev.links.items():
        lines.append(f"- {name}: {url}")
    if ev.routing_hints:
        lines += ["", "## Where to look", *[f"- {h}" for h in ev.routing_hints]]
    if ev.exception:
        frames = "\n".join(
            f"  {'*' if f.in_app else ' '} {f.filename}:{f.lineno or '?'} in {f.function or '?'}"
            for f in ev.exception.frames
        )
        lines += [
            "",
            "## Exception",
            _code(f"{ev.exception.type}: {ev.exception.value}\n{frames}".rstrip()),
        ]
    if ev.message:
        lines += ["", "## Alert message (verbatim)", _code(ev.message, 3000)]
    if ev.sample_logs:
        lines += ["", "## Sample logs / stack", _code("\n".join(ev.sample_logs))]
    if ev.request:
        lines += ["", "## Request (scrubbed)", _code(str(ev.request), 2000)]
    if ci_failure is not None or findings:
        lines += ["", "## Feedback to address"]
        if ci_failure is not None:
            head = f"### CI failure{f' — {ci_failure.job_name}' if ci_failure.job_name else ''}"
            if ci_failure.build_url:
                head += f" ({ci_failure.build_url})"
            lines += [head, _code(ci_failure.excerpt, 12000)]
        for i, f in enumerate(findings or [], 1):
            lines += [f"### Review comment {i}", _code(f, 3000)]
        lines += ["", INSTRUCTIONS_SHEPHERD]
    else:
        lines += ["", INSTRUCTIONS_FIX]
    return "\n".join(lines) + "\n"
