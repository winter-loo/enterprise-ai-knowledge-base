from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import TypedDict, cast


class ScopeClaims(TypedDict):
    kb_id: str
    project_id: str
    scope: str
    exp: int


def _key() -> bytes:
    value = os.getenv("AUTHZ_SIGNING_KEY")
    if value is None or len(value) < 32:
        raise RuntimeError("AUTHZ_SIGNING_KEY must contain at least 32 characters")
    return value.encode()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_scope(kb_id: str, project_id: str, scope: str, ttl_seconds: int = 300) -> str:
    payload = json.dumps(
        {"kb_id": kb_id, "project_id": project_id, "scope": scope, "exp": int(time.time()) + ttl_seconds},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = _encode(payload)
    signature = _encode(hmac.digest(_key(), encoded.encode(), hashlib.sha256))
    return f"{encoded}.{signature}"


def verify_scope(token: str, kb_id: str, project_id: str) -> str:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _encode(hmac.digest(_key(), encoded.encode(), hashlib.sha256))
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("invalid signature")
        claims = cast(ScopeClaims, json.loads(_decode(encoded)))
        if claims["kb_id"] != kb_id or claims["project_id"] != project_id:
            raise ValueError("scope target mismatch")
        if claims["exp"] < int(time.time()):
            raise ValueError("scope expired")
        return claims["scope"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid scope capability") from exc
