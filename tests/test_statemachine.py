"""Structural checks on statemachine/issue.asl.json. Execution is covered by the LocalStack
integration job; this keeps the definition valid JSON and the graph closed."""

from __future__ import annotations

import json
import re
from pathlib import Path

ASL = Path(__file__).parent.parent / "statemachine" / "issue.asl.json"

PLACEHOLDERS = {
    "cluster_arn": "arn:aws:ecs:us-east-1:123:cluster/gc",
    "task_definition_arn": "arn:aws:ecs:us-east-1:123:task-definition/gc:1",
    "container_name": "worker",
    "subnets_json": '["subnet-1", "subnet-2"]',
    "security_group_id": "sg-1",
    "assign_public_ip": "DISABLED",
    "lifecycle_lambda_arn": "arn:aws:lambda:us-east-1:123:function:gc-lifecycle",
}


def render() -> dict:
    text = ASL.read_text()
    for k, v in PLACEHOLDERS.items():
        text = text.replace("${" + k + "}", v)
    assert "${" not in text, "unreplaced placeholder"
    return json.loads(text)


def test_definition_is_valid_json_after_substitution() -> None:
    d = render()
    assert d["QueryLanguage"] == "JSONata"
    assert d["StartAt"] == "Init"


def test_every_transition_targets_an_existing_state() -> None:
    d = render()
    states = d["States"]
    targets: set[str] = set()
    for s in states.values():
        for key in ("Next", "Default"):
            if key in s:
                targets.add(s[key])
        for c in s.get("Choices", []):
            targets.add(c["Next"])
        for c in s.get("Catch", []):
            targets.add(c["Next"])
    missing = targets - set(states)
    assert not missing, missing
    terminal = [n for n, s in states.items() if s.get("End") or s["Type"] == "Succeed"]
    assert "Done" in terminal and "FinalizeMerged" in terminal


def test_worker_tasks_carry_token_and_budget_flow() -> None:
    d = render()
    for name in ("Fix", "Groom"):
        s = d["States"][name]
        assert s["Resource"] == "arn:aws:states:::ecs:runTask.waitForTaskToken"
        env = s["Arguments"]["Overrides"]["ContainerOverrides"][0]["Environment"]
        assert {e["Name"] for e in env} == {"GRUMPYCAT_TASK", "GRUMPYCAT_BRIEF_MD"}
        assert "$states.context.Task.Token" in env[0]["Value"]
        assert s["Catch"][0]["Next"] == "FinalizeNeedsHuman"
    assert d["States"]["Park"]["TimeoutSeconds"] == 14 * 24 * 3600
    assert d["States"]["CheckBudget"]["Choices"][0]["Condition"] == "{% $attempt < $max_attempts %}"
    assert d["States"]["BumpAttempt"]["Assign"]["attempt"] == "{% $attempt + 1 %}"
    # FinalizeReady keeps listening rather than ending the execution
    assert d["States"]["FinalizeReady"]["Next"] == "Park"


def test_placeholders_are_documented() -> None:
    found = set(re.findall(r"\$\{(\w+)\}", ASL.read_text()))
    assert found == set(PLACEHOLDERS)
