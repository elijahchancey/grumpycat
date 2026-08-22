---
name: grumpycat-dev
description: Run the full local quality gate for grumpycat (ruff, mypy, pytest) and interpret failures. Use before declaring any change done, or when asked to "run the checks".
allowed-tools: Bash(uv run ruff*), Bash(uv run mypy*), Bash(uv run pytest*), Bash(uv sync*)
---

# grumpycat quality gate

Run all three, in this order, from the repo root:

```sh
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run pytest
```

- `ruff format --check` failing: run `uv run ruff format src tests` and re-check. Never hand-format.
- mypy is strict. Prefer fixing the type over `# type: ignore`; if you must ignore, give the
  error code and a reason on the same line.
- pytest: fixtures in `tests/fixtures/` are recorded webhook payloads. If a parser change
  breaks one, read the fixture — the payload is the spec.
- A change is not done until all three pass. Say which ones you ran in the PR text.
