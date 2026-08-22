"""Slack output: one thread per issue, an approve/dismiss button in gated mode, and a page to
the on-call channel when triage says so.

Setup: a Slack app with `chat:write` (bot token → SLACK_BOT_TOKEN) and Interactivity
pointed at `<module output>/slack/interactions` (signing secret → SLACK_SIGNING_SECRET).
Invite the bot to the channels in config.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from grumpycat.core.models import Brief, IssueState, IssueStatus, Transition
from grumpycat.plugins.spec import OutputPlugin, PluginKind, PluginSpec

APPROVE = "grumpycat_approve"
DISMISS = "grumpycat_dismiss"


class SlackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str = Field(description="Channel id or #name for the per-issue threads")
    oncall_channel: str | None = Field(default=None, description="Where pages go")
    api_url: str = "https://slack.com/api"


class SlackOutput(OutputPlugin):
    spec = PluginSpec(
        name="slack",
        kind=PluginKind.OUTPUT,
        config_schema=SlackConfig,
        required_secrets=("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"),
    )
    config: SlackConfig
    transport: Any = None

    def _post(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        body: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts
        if blocks:
            body["blocks"] = blocks
        with httpx.Client(
            base_url=self.config.api_url, transport=self.transport, timeout=20.0
        ) as c:
            r = c.post(
                "/chat.postMessage",
                json=body,
                headers={"Authorization": f"Bearer {self.secrets['SLACK_BOT_TOKEN']}"},
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                msg = f"slack chat.postMessage failed: {data.get('error')}"
                raise RuntimeError(msg)
            return str(data["ts"])

    def on_transition(
        self, state: IssueState, previous: IssueState | None, brief: Brief | None
    ) -> IssueState:
        prev = previous.status if previous else None
        same_status = prev is state.status
        # Page once, as early as possible.
        if state.triage and state.triage.page and not state.paged and self.config.oncall_channel:
            self._post(self.config.oncall_channel, page_text(state))
            state = state.model_copy(update={"paged": True})

        if state.status is IssueStatus.AWAITING_APPROVAL and not same_status:
            ts = self._post(self.config.channel, summary_text(state), blocks=approval_blocks(state))
            return state.model_copy(update={"slack_thread_ts": ts})
        if state.status is IssueStatus.RCA_ONLY and not same_status:
            ts = self._post(
                self.config.channel,
                summary_text(state) + f"\n_No PR: {state.rationale}_",
                thread_ts=state.slack_thread_ts,
            )
            return state.model_copy(update={"slack_thread_ts": state.slack_thread_ts or ts})

        reply = transition_text(state, prev)
        if reply is None:
            return state
        if state.slack_thread_ts:
            self._post(self.config.channel, reply, thread_ts=state.slack_thread_ts)
            return state
        ts = self._post(self.config.channel, summary_text(state) + "\n" + reply)
        return state.model_copy(update={"slack_thread_ts": ts})


def summary_text(state: IssueState) -> str:
    e, t = state.event, state.triage
    head = "Regression" if e.transition is Transition.REGRESSION else "New error"
    parts = [f"*{head}:* {e.title}", f"`{e.service or '?'}` · `{e.env or '?'}` · {e.source}"]
    if t:
        parts.append(f"severity *{t.severity}* · confidence {t.confidence:.2f} · {t.rationale}")
    if e.url:
        parts.append(f"<{e.url}|open in {e.source}>")
    if state.target:
        parts.append(f"repo `{state.target.full_name}` · engine `{state.target.engine}`")
    return "\n".join(parts)


def page_text(state: IssueState) -> str:
    e = state.event
    return (
        f":rotating_light: *{e.title}* in `{e.service or '?'}` ({e.env or '?'}) — "
        f"{state.triage.rationale if state.triage else ''}\n{e.url or e.fingerprint}"
    )


def approval_blocks(state: IssueState) -> list[dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary_text(state)}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Open a fix PR"},
                    "action_id": APPROVE,
                    "value": state.fingerprint,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Dismiss"},
                    "action_id": DISMISS,
                    "value": state.fingerprint,
                },
            ],
        },
    ]


def transition_text(state: IssueState, prev: IssueStatus | None) -> str | None:
    s = state.status
    same = prev is s
    if s is IssueStatus.FIXING and not same:
        return ":hammer_and_wrench: starting a fix run"
    if s is IssueStatus.PR_OPEN and state.pr_url and (not same or prev is None):
        return f":git: draft PR opened: {state.pr_url}"
    if s is IssueStatus.SHEPHERDING:
        o = state.last_outcome
        sha = f" `{o.pushed_sha[:10]}`" if o and o.pushed_sha else ""
        return f":arrows_counterclockwise: pushed follow-up{sha} (attempt {state.attempts})"
    if s is IssueStatus.READY and not same:
        return f":white_check_mark: CI green, ready for review: {state.pr_url}"
    if s is IssueStatus.NEEDS_HUMAN and not same:
        return f":raised_hand: needs a human — {state.rationale}" + (
            f"\n{state.pr_url}" if state.pr_url else ""
        )
    if s is IssueStatus.MERGED and not same:
        return ":tada: merged"
    if s is IssueStatus.CLOSED and not same:
        return f":x: closed — {state.rationale or 'PR closed without merging'}"
    if same and s in {
        IssueStatus.PR_OPEN,
        IssueStatus.SHEPHERDING,
        IssueStatus.READY,
        IssueStatus.AWAITING_APPROVAL,
    }:
        when = state.event.occurred_at.isoformat(timespec="minutes")
        return f":eyes: seen again ({state.event.transition}) at {when}"
    return None
