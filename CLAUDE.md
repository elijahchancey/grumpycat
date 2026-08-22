@AGENTS.md

# Claude Code notes

Everything that matters is in `AGENTS.md` (included above) so Codex and OpenHands see the same
rules. Claude-only details:

- Skills are in `.agents/skills/`; `.claude/skills` is a symlink to it. Add new skills there.
- `.claude/settings.json` pre-approves `uv run ruff|mypy|pytest`, `uv sync`, and read-only git.
  Anything else prompts; do not widen it without a reason in the PR.
- This repo is also edited by Codex. If a rule would only make sense to Claude, it probably
  belongs in this file; otherwise put it in `AGENTS.md`.
