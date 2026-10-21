"""Bearer-token authentication with scopes."""

from __future__ import annotations

import hmac
from collections.abc import Callable

from fastapi import HTTPException, Request


def require(scope: str) -> Callable[[Request], None]:
    """Dependency: 401 for a missing or unknown token, 403 for the wrong scope."""

    def check(request: Request) -> None:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(
                401, "missing token", headers={"WWW-Authenticate": "Bearer"}
            )
        granted = None
        for known, known_scope in request.app.state.config.tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                granted = known_scope
        if granted is None:
            raise HTTPException(
                401, "unknown token", headers={"WWW-Authenticate": "Bearer"}
            )
        if granted != scope:
            raise HTTPException(403, "token scope does not allow this route")

    return check
