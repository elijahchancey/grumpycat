from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from grumpycat.core.config import Config, load_config
from tests.conftest import MINIMAL_YAML


def test_load_from_string_and_path(tmp_path: Path) -> None:
    cfg = load_config(MINIMAL_YAML)
    assert cfg.client == "acme"
    p = tmp_path / "grumpycat.yaml"
    p.write_text(MINIMAL_YAML)
    assert load_config(p) == cfg
    assert load_config(str(p)) == cfg  # bare path string, no newline


def test_env_var_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRUMPYCAT_CONFIG", MINIMAL_YAML)
    assert load_config().client == "acme"
    monkeypatch.delenv("GRUMPYCAT_CONFIG")
    with pytest.raises(RuntimeError, match="no config"):
        load_config()


def test_policy_defaults_are_conservative() -> None:
    cfg = load_config(MINIMAL_YAML)
    assert cfg.policy.gated is True
    assert cfg.policy.freeze is False
    assert cfg.policy.max_attempts == 3
    assert cfg.policy.confidence_min == 0.6


def test_resolve_repo_by_service_then_by_name() -> None:
    cfg = load_config(MINIMAL_YAML)
    name, repo = cfg.resolve_repo("api-worker") or (None, None)
    assert name == "acme/api"
    assert repo is not None and repo.default_branch == "master"
    name, _ = cfg.resolve_repo("frontend") or (None, None)
    assert name == "acme/frontend"
    assert cfg.resolve_repo("unknown") is None
    assert cfg.resolve_repo(None) is None


def test_to_target_carries_everything() -> None:
    cfg = load_config(MINIMAL_YAML)
    t = cfg.repos["acme/api"].to_target("acme/api")
    assert t.full_name == "acme/api"
    assert t.engine == "fake_engine"
    assert t.ci_pipeline == "acme/api"
    assert t.labels == ["grumpycat"]


@pytest.mark.parametrize(
    "bad",
    [
        "client: ACME\n",  # uppercase
        "client: acme\nrepos:\n  api: {engine: x}\n",  # not owner/name
        "client: acme\npolicy: {confidence_min: 2}\n",
        "client: acme\nsurprise: 1\n",  # extra keys forbidden
    ],
)
def test_invalid_configs_are_rejected(bad: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        load_config(bad)


def test_top_level_must_be_mapping() -> None:
    with pytest.raises(ValueError, match="mapping"):
        load_config("- a\n- b\n")


def test_minimal_config_is_one_line() -> None:
    assert Config.model_validate({"client": "x1"}).repos == {}
