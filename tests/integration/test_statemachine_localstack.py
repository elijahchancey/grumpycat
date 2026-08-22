"""Execute the real `statemachine/issue.asl.json` against LocalStack.

What is real: Step Functions (JSONata), DynamoDB, Lambda, the ASL file itself.
What is stubbed: the two Lambdas the ASL calls — the *worker* (stands in for the Fargate task,
returns a canned FixOutcome via SendTaskSuccess) and *lifecycle* (records every op in DynamoDB
and parks the task token there so the test can resume the execution like the GitHub hook
would). That isolates exactly the thing moto cannot test: the orchestration graph.

Run: LOCALSTACK_ENDPOINT=http://localhost:4566 uv run pytest tests/integration -m integration
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import boto3
import pytest

pytestmark = pytest.mark.integration

ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT")
if not ENDPOINT:
    pytest.skip("set LOCALSTACK_ENDPOINT to run", allow_module_level=True)

ASL = Path(__file__).parents[2] / "statemachine" / "issue.asl.json"
REGION = "us-east-1"
ROLE = "arn:aws:iam::000000000000:role/gc-test"

WORKER_STUB = r"""
import json, os, boto3
sfn = boto3.client("stepfunctions")
def handler(event, context):
    task = event["task"]; attempt = int(event.get("attempt", 0)); token = event["task_token"]
    title = task["brief"]["event"]["title"]
    if "decline" in title:
        out = {"status": "declined", "summary": "could not reproduce", "branch": task["branch"]}
    else:
        out = {"status": "pushed", "summary": f"push {attempt}", "branch": task["branch"],
               "pushed_sha": f"sha{attempt:02d}", "pr_number": task.get("pr_number"),
               "addressed_comment_ids": (task.get("trigger") or {}).get("comment_ids") or []}
    sfn.send_task_success(taskToken=token, output=json.dumps(out))
    return {"ok": True}
"""

LIFECYCLE_STUB = r"""
import json, os, time, boto3
ddb = boto3.resource("dynamodb").Table(os.environ["TABLE"])
def handler(event, context):
    op = event["op"]; fp = event["fingerprint"]
    ddb.put_item(Item={"pk": f"{fp}#{time.time_ns()}", "op": op, "payload": json.dumps(event)})
    if op == "park":
        ddb.put_item(Item={"pk": f"token#{fp}", "token": event["task_token"]})
    return {"ok": True, "op": op}
"""


def _zip(code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("handler.py")
        info.external_attr = 0o644 << 16  # readable by the Lambda runtime user
        z.writestr(info, code)
    return buf.getvalue()


def _client(name: str) -> Any:
    return boto3.client(
        name,
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture(scope="module")
def stack() -> dict[str, Any]:
    lam, sfn, ddb = _client("lambda"), _client("stepfunctions"), _client("dynamodb")
    suffix = str(time.time_ns())[-8:]
    table = f"gc-it-{suffix}"
    ddb.create_table(
        TableName=table,
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.get_waiter("table_exists").wait(TableName=table)

    def make_fn(name: str, code: str, env: dict[str, str]) -> str:
        r = lam.create_function(
            FunctionName=name,
            Runtime="python3.12",
            Role=ROLE,
            Handler="handler.handler",
            Code={"ZipFile": _zip(code)},
            Timeout=30,
            Environment={"Variables": env},
        )
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
        return str(r["FunctionArn"])

    worker = make_fn(f"gc-it-worker-{suffix}", WORKER_STUB, {})
    lifecycle = make_fn(f"gc-it-lifecycle-{suffix}", LIFECYCLE_STUB, {"TABLE": table})

    # Render the real definition, then swap the Fargate steps for the worker stub.
    text = ASL.read_text()
    for k, v in {
        "cluster_arn": "arn:aws:ecs:us-east-1:000000000000:cluster/x",
        "task_definition_arn": "arn:aws:ecs:us-east-1:000000000000:task-definition/x:1",
        "container_name": "worker",
        "subnets_json": '["subnet-1"]',
        "security_group_id": "sg-1",
        "assign_public_ip": "DISABLED",
        "lifecycle_lambda_arn": lifecycle,
    }.items():
        text = text.replace("${" + k + "}", v)
    asl = json.loads(text)
    for name in ("Fix", "Shepherd"):
        asl["States"][name]["Resource"] = "arn:aws:states:::lambda:invoke.waitForTaskToken"
        asl["States"][name]["Arguments"] = {
            "FunctionName": worker,
            "Payload": {
                "task": "{% $task %}",
                "attempt": "{% $attempt %}",
                "task_token": "{% $states.context.Task.Token %}",
            },
        }
        asl["States"][name].pop("HeartbeatSeconds", None)
    sm = sfn.create_state_machine(
        name=f"gc-it-{suffix}", definition=json.dumps(asl), roleArn=ROLE, type="STANDARD"
    )
    return {"sfn": sfn, "ddb": _client("dynamodb"), "table": table, "sm": sm["stateMachineArn"]}


def _records(st: dict[str, Any], fp: str) -> list[dict[str, Any]]:
    items = st["ddb"].scan(TableName=st["table"])["Items"]
    out = []
    for i in items:
        pk = i["pk"]["S"]
        if pk.startswith(f"{fp}#"):
            out.append({"pk": pk, "op": i["op"]["S"], "payload": json.loads(i["payload"]["S"])})
    return sorted(out, key=lambda r: int(r["pk"].rsplit("#", 1)[1]))


def _wait_token(
    st: dict[str, Any], fp: str, *, not_token: str | None = None, timeout: float = 60
) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = st["ddb"].get_item(TableName=st["table"], Key={"pk": {"S": f"token#{fp}"}})
        tok = r.get("Item", {}).get("token", {}).get("S")
        if tok and tok != not_token:
            return str(tok)
        time.sleep(1)
    msg = f"no park token for {fp}"
    raise AssertionError(msg)


def _status(st: dict[str, Any], arn: str) -> str:
    return str(st["sfn"].describe_execution(executionArn=arn)["status"])


def _wait_status(st: dict[str, Any], arn: str, wanted: set[str], timeout: float = 60) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _status(st, arn)
        if s in wanted:
            return s
        time.sleep(1)
    return _status(st, arn)


def _input(fp: str, title: str, max_attempts: int) -> str:
    brief = {
        "event": {
            "source": "sentry",
            "fingerprint": fp,
            "transition": "new",
            "service": "api",
            "env": "prod",
            "title": title,
            "occurred_at": "2026-08-22T00:00:00Z",
        },
        "evidence": {},
        "triage": {"severity": "high", "confidence": 0.7, "page": False, "rationale": "t"},
        "target": {"full_name": "acme/api", "engine": "claude"},
    }
    return json.dumps(
        {
            "fingerprint": fp,
            "max_attempts": max_attempts,
            "attempt": 0,
            "task": {"kind": "fix", "brief": brief, "branch": f"grumpycat/{fp}"},
            "brief_md": "# brief",
        }
    )


def _resume(st: dict[str, Any], token: str, kind: str, **extra: Any) -> None:
    ev = {
        "kind": kind,
        "received_at": "2026-08-22T00:00:00Z",
        "findings": [],
        "comment_ids": [],
        **extra,
    }
    st["sfn"].send_task_success(taskToken=token, output=json.dumps(ev))


def test_fix_then_ci_failure_then_shepherd_then_ready_then_merged(stack: dict[str, Any]) -> None:
    fp = f"it-happy-{time.time_ns()}"
    arn = stack["sfn"].start_execution(stateMachineArn=stack["sm"], input=_input(fp, "boom", 3))[
        "executionArn"
    ]

    t1 = _wait_token(stack, fp)
    recs = _records(stack, fp)
    assert [r["op"] for r in recs] == ["after_run", "park"]
    assert (
        recs[0]["payload"]["outcome"]["status"] == "pushed" and recs[0]["payload"]["attempt"] == 0
    )

    _resume(
        stack,
        t1,
        "ci_failure",
        ci_failure={"excerpt": "rspec failed", "truncated": False},
        comment_ids=[501],
    )
    t2 = _wait_token(stack, fp, not_token=t1)
    recs = _records(stack, fp)
    assert [r["op"] for r in recs] == ["after_run", "park", "after_run", "park"]
    shepherd = recs[2]["payload"]
    assert shepherd["attempt"] == 1 and shepherd["outcome"]["summary"] == "push 1"
    assert shepherd["outcome"]["addressed_comment_ids"] == [501]

    _resume(stack, t2, "ci_success")
    t3 = _wait_token(stack, fp, not_token=t2)  # FinalizeReady -> Park again
    recs = _records(stack, fp)
    assert recs[-2]["op"] == "finalize" and recs[-2]["payload"]["outcome"] == "ready"
    assert _status(stack, arn) == "RUNNING"

    _resume(stack, t3, "merged")
    assert _wait_status(stack, arn, {"SUCCEEDED", "FAILED"}) == "SUCCEEDED"
    finals = [r["payload"]["outcome"] for r in _records(stack, fp) if r["op"] == "finalize"]
    assert finals == ["ready", "merged"]


def test_attempt_budget_exhausted_ends_in_needs_human(stack: dict[str, Any]) -> None:
    fp = f"it-budget-{time.time_ns()}"
    arn = stack["sfn"].start_execution(stateMachineArn=stack["sm"], input=_input(fp, "boom", 1))[
        "executionArn"
    ]
    t1 = _wait_token(stack, fp)
    _resume(stack, t1, "ci_failure", ci_failure={"excerpt": "x"})
    t2 = _wait_token(stack, fp, not_token=t1)  # one shepherd run allowed
    _resume(stack, t2, "ci_failure", ci_failure={"excerpt": "y"})
    assert _wait_status(stack, arn, {"SUCCEEDED", "FAILED"}) == "SUCCEEDED"
    finals = [r["payload"] for r in _records(stack, fp) if r["op"] == "finalize"]
    assert len(finals) == 1 and finals[0]["outcome"] == "needs_human"
    assert re.search(r"attempt budget \(1\) exhausted", finals[0]["reason"])
    assert [r["op"] for r in _records(stack, fp)].count("after_run") == 2


def test_declined_fix_ends_without_parking(stack: dict[str, Any]) -> None:
    fp = f"it-decline-{time.time_ns()}"
    arn = stack["sfn"].start_execution(
        stateMachineArn=stack["sm"], input=_input(fp, "please decline", 3)
    )["executionArn"]
    assert _wait_status(stack, arn, {"SUCCEEDED", "FAILED"}) == "SUCCEEDED"
    recs = _records(stack, fp)
    assert [r["op"] for r in recs] == ["after_run"]
    assert recs[0]["payload"]["outcome"]["status"] == "declined"
