"""Process-wide wiring for the Lambda handlers: config, secrets, registry, store, AWS clients.

Environment the Terraform module sets on every Lambda:

  GRUMPYCAT_CONFIG         inline grumpycat.yaml, or
  GRUMPYCAT_CONFIG_PARAM   SSM parameter name holding it (preferred; survives size limits)
  GRUMPYCAT_SECRET_ARNS    JSON {ENV_VAR: arn}               (see core.secrets)
  GRUMPYCAT_TABLE          DynamoDB table name
  GRUMPYCAT_STATE_MACHINE  Step Functions ARN                (triage, slack approve)
  GRUMPYCAT_TRIAGE_FUNCTION name of the triage Lambda         (router)
  GRUMPYCAT_BOT_LOGIN      GitHub login of our App, e.g. grumpycat[bot] (github hook)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from typing import Any

import boto3
from aws_lambda_powertools import Logger

from grumpycat.core.config import Config, load_config
from grumpycat.core.secrets import cached_secrets
from grumpycat.core.store import IssueStore
from grumpycat.plugins import Registry

logger = Logger(service="grumpycat")


@dataclass(frozen=True)
class Runtime:
    config: Config
    registry: Registry
    store: IssueStore
    sfn: Any
    lam: Any

    @property
    def state_machine_arn(self) -> str:
        return os.environ["GRUMPYCAT_STATE_MACHINE"]


def _config_text() -> str:
    if param := os.environ.get("GRUMPYCAT_CONFIG_PARAM"):
        return str(
            boto3.client("ssm").get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
        )
    return os.environ["GRUMPYCAT_CONFIG"]


@cache
def runtime() -> Runtime:
    config = load_config(_config_text())
    secrets = cached_secrets()
    registry = Registry(config, secrets)
    return Runtime(
        config=config,
        registry=registry,
        store=IssueStore(),
        sfn=boto3.client("stepfunctions"),
        lam=boto3.client("lambda"),
    )


def reset() -> None:
    """Tests only."""
    runtime.cache_clear()
    cached_secrets.cache_clear()
