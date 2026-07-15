"""Bridge for calling async coroutines from synchronous contexts.

The background iMessage worker runs in a plain ``threading.Thread`` with no
event loop, but our external-service calls (DeepSeek, Readwise) are written as
``async`` functions using ``httpx.AsyncClient``. This helper lets the sync
worker / sync LangChain tools drive those coroutines safely.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


def run_async(coro: "Coroutine[Any, Any, T]") -> T:
    """Run ``coro`` to completion from a synchronous context.

    Raises ``RuntimeError`` if called from within a running event loop — in
    that case the caller should ``await`` the coroutine directly instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread: safe to spin up our own.
        return asyncio.run(coro)
    raise RuntimeError(
        "run_async() called from within a running event loop; await the "
        "coroutine directly instead of bridging."
    )
