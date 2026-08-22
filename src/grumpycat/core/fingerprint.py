"""Final fingerprint = input's base fingerprint + (optionally) the enrichment signature.

Sentry issues are already grouped, so the base is enough. Datadog monitors and ECS exits
identify a *place*, not a *defect*; their inputs leave the signature to `enrich`, and we fold it
in here so the dedupe key means "this error", not "this monitor".
"""

from __future__ import annotations

import hashlib
import re

from grumpycat.core.models import ErrorEvent, Evidence

_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def final_fingerprint(event: ErrorEvent, evidence: Evidence) -> str:
    if evidence.signature and not event.fingerprint.startswith("sentry:"):
        digest = hashlib.sha256(evidence.signature.encode()).hexdigest()[:12]
        return f"{event.fingerprint}#{digest}"
    return event.fingerprint


def short_id(fingerprint: str) -> str:
    """Filesystem/branch/Step-Functions-name safe id derived from a fingerprint."""
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:10]


def branch_name(fingerprint: str) -> str:
    slug = _UNSAFE.sub("-", fingerprint.split(":", 1)[0])
    return f"grumpycat/{slug}-{short_id(fingerprint)}"
