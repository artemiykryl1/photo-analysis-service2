"""Request-id propagation middleware.

Implemented as **pure ASGI** (`async def __call__(self, scope, receive,
send)`), NOT `starlette.middleware.base.BaseHTTPMiddleware`.

Reason (see tasks/TASK-001/20_design.md §7.1, §13 risks): `BaseHTTPMiddleware`
runs the downstream application in a separate anyio task, so a
`ContextVar` set in `dispatch()` before `call_next()` is not reliably
visible inside the endpoint/service code. A pure-ASGI middleware executes
in the same task as the rest of the request, so `trace_id_var` set here is
visible everywhere downstream (endpoint handlers, services, logging).
"""

import logging
from uuid import uuid4

from app.core.logging import trace_id_var

logger = logging.getLogger(__name__)


class RequestIdMiddleware:
    """Sets `trace_id_var` from `X-Request-ID` (or a generated uuid4) and
    echoes it back on the response as `X-Request-ID`.
    """

    def __init__(self, app) -> None:  # noqa: ANN001 - ASGI app callable
        self._app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_request_id = headers.get(b"x-request-id")
        request_id = raw_request_id.decode("latin-1") if raw_request_id else str(uuid4())

        token = trace_id_var.set(request_id)

        async def wrapped_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers") or [])
                response_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = response_headers
            await send(message)

        try:
            await self._app(scope, receive, wrapped_send)
        finally:
            trace_id_var.reset(token)
