"""Resolve the secrets map into a `{ENV_VAR: value}` mapping, once per process.

Two sources, merged (values already in the environment win, so the ECS worker — which gets
secrets injected natively through the task definition — needs no API calls):

  1. `GRUMPYCAT_SECRET_ARNS`: JSON `{"ENV_VAR": "<arn>"}` set by the Terraform module on the
     Lambdas. ARNs may be SSM parameters (`arn:aws:ssm:…:parameter/…`) or Secrets Manager
     secrets (`arn:aws:secretsmanager:…:secret:…`, optionally `…:secret:name:JSONKEY::`).
  2. Environment variables named in that JSON that are already set.

Values are never logged. The resolved mapping is what the plugin registry receives.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from functools import cache
from typing import Any

import boto3

ENV_ARNS = "GRUMPYCAT_SECRET_ARNS"


def _ssm_name(arn: str) -> str:
    # arn:aws:ssm:us-east-1:123:parameter/grumpycat/x  -> /grumpycat/x
    return "/" + arn.split(":parameter/", 1)[1] if ":parameter/" in arn else arn


def _secretsmanager_parts(arn: str) -> tuple[str, str | None]:
    # arn:aws:secretsmanager:us-east-1:123:secret:name-AbCdEf[:jsonkey::]
    if arn.count(":") >= 8:
        head, _, tail = arn.rpartition(":secret:")
        rest = tail.split(":")
        return f"{head}:secret:{rest[0]}", (rest[1] if len(rest) > 1 and rest[1] else None)
    return arn, None


def fetch(arn: str, *, ssm: Any = None, sm: Any = None) -> str:
    if ":ssm:" in arn or arn.startswith("/"):
        client = ssm or boto3.client("ssm")
        return str(
            client.get_parameter(Name=_ssm_name(arn), WithDecryption=True)["Parameter"]["Value"]
        )
    if ":secretsmanager:" in arn:
        client = sm or boto3.client("secretsmanager")
        secret_id, key = _secretsmanager_parts(arn)
        value = str(client.get_secret_value(SecretId=secret_id)["SecretString"])
        if key:
            return str(json.loads(value)[key])
        return value
    msg = f"unsupported secret reference {arn!r}: expected an SSM or Secrets Manager ARN"
    raise ValueError(msg)


def load_secrets(
    env: Mapping[str, str] | None = None, *, ssm: Any = None, sm: Any = None
) -> dict[str, str]:
    env = os.environ if env is None else env
    arns: dict[str, str] = json.loads(env.get(ENV_ARNS) or "{}")
    out: dict[str, str] = {}
    for name, arn in arns.items():
        out[name] = env.get(name) or fetch(arn, ssm=ssm, sm=sm)
    return out


@cache
def cached_secrets() -> dict[str, str]:
    """Process-wide; Lambda keeps it warm across invocations."""
    return load_secrets()
