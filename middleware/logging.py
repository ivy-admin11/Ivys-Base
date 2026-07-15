"""Correlation-ID request logging middleware.

Assigns a UUID to every inbound request, logs the request in/out at INFO with
that ID, exposes it on ``request.state.correlation_id`` for handlers, and echoes
it back in the ``X-Request-ID`` response header so clients/logs can be
correlated across the boundary.
"""

from __future__ import annotations

import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("ivy.request")

CORRELATION_HEADER = "X-Request-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Honor an inbound correlation id if the client supplied one.
        correlation_id = request.headers.get(CORRELATION_HEADER) or uuid.uuid4().hex
        request.state.correlation_id = correlation_id

        client = request.client.host if request.client else "unknown"
        logger.info(
            "[%s] --> %s %s from %s",
            correlation_id,
            request.method,
            request.url.path,
            client,
        )
        try:
            response: Response = await call_next(request)
        except Exception:
            # Let the app's exception handlers format the body; just log + re-raise.
            logger.exception("[%s] <-- unhandled exception", correlation_id)
            raise

        response.headers[CORRELATION_HEADER] = correlation_id
        logger.info(
            "[%s] <-- %s %s %s",
            correlation_id,
            request.method,
            request.url.path,
            response.status_code,
        )
        return response
