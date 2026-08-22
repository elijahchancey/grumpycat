"""GitHub App authentication: a short-lived installation token per run.

Secrets map keys: GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY (PEM).
Tokens last an hour; the worker mints one at start, the GitHub output mints per call.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx
import jwt

REQUIRED = ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY")


def app_jwt(app_id: str, private_key_pem: str, *, now: float | None = None) -> str:
    t = int(now if now is not None else time.time())
    payload = {"iat": t - 60, "exp": t + 9 * 60, "iss": app_id}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def installation_token(
    secrets: Mapping[str, str],
    *,
    api_url: str = "https://api.github.com",
    transport: Any = None,
) -> str:
    missing = [k for k in REQUIRED if not secrets.get(k)]
    if missing:
        msg = f"GitHub App auth needs secrets {missing} in the secrets map"
        raise RuntimeError(msg)
    token = app_jwt(secrets["GITHUB_APP_ID"], secrets["GITHUB_APP_PRIVATE_KEY"])
    with httpx.Client(base_url=api_url, transport=transport, timeout=20.0) as c:
        r = c.post(
            f"/app/installations/{secrets['GITHUB_APP_INSTALLATION_ID']}/access_tokens",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        r.raise_for_status()
        return str(r.json()["token"])


def clone_url(repo_full_name: str, token: str, *, host: str = "github.com") -> str:
    return f"https://x-access-token:{token}@{host}/{repo_full_name}.git"
