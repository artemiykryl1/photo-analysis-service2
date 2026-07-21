"""RED (Request/Error/Duration) HTTP metrics middleware.

tasks/TASK-002/20_design.md §8.3. Unlike `app.api.middleware.RequestIdMiddleware`
(pure ASGI, needed because it sets a `ContextVar` consumed by downstream
code), this middleware only measures timing and reads the already-resolved
route - it does not set anything that must be visible via a `ContextVar`,
so the simpler `@app.middleware("http")` (`BaseHTTPMiddleware`) style is
fine here, as explicitly allowed by the design.

`endpoint` is the matched route's **path template**
(e.g. `/v1/photos/{photo_id}`), never the raw request path - using the raw
path would blow up label cardinality with one series per `photo_id`
(design §8.3, §11 risk #7). Unmatched routes (404s for unknown paths) fall
back to the literal `"unmatched"` label.
"""

import time
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from app.integrations.metrics_api import http_request_duration_seconds, http_requests_total


async def metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.monotonic()
    response = await call_next(request)
    duration_seconds = time.monotonic() - start

    route = request.scope.get("route")
    endpoint = route.path if route is not None else "unmatched"

    http_requests_total.labels(
        method=request.method, endpoint=endpoint, status=str(response.status_code)
    ).inc()
    http_request_duration_seconds.labels(method=request.method, endpoint=endpoint).observe(
        duration_seconds
    )

    return response
