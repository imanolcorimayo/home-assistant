import base64
import secrets

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class BasicAuthMiddleware(BaseHTTPMiddleware):
    # Excludes /webhook/* (Telegram must reach us without credentials) and
    # /health (probes). Disabled entirely when either credential is empty,
    # so local dev keeps working without prompting.

    def __init__(self, app, username: str, password: str) -> None:
        super().__init__(app)
        self._username = username
        self._password = password
        self._enabled = bool(username and password)

    async def dispatch(self, request: Request, call_next):
        if not self._enabled:
            return await call_next(request)

        path = request.url.path
        if path == "/health" or path.startswith("/webhook/"):
            return await call_next(request)

        if not self._check(request.headers.get("authorization", "")):
            return Response(
                status_code=401,
                content="Unauthorized",
                headers={"WWW-Authenticate": 'Basic realm="SovereignBox"'},
            )

        return await call_next(request)

    def _check(self, header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        user, sep, password = decoded.partition(":")
        if not sep:
            return False
        return (
            secrets.compare_digest(user, self._username)
            and secrets.compare_digest(password, self._password)
        )
