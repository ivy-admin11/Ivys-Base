"""Bridge MCP stdio servers into sync Python callables for Gemini's tool API.

Gemini's google-genai library introspects regular Python functions to build the
tool schema it advertises to the model — `inspect.signature` for parameters,
the docstring for descriptions. MCP tools live behind an async stdio JSON-RPC
session, which doesn't fit that shape.

This module solves the impedance mismatch:
  * A single asyncio loop is hoisted onto a dedicated daemon thread.
  * Each registered MCP server stays open for the lifetime of the process
    (stdio_client + ClientSession are entered manually, never exited).
  * For every tool the server advertises, a fresh Python function is forged
    via `exec` whose name, signature, and docstring mirror the MCP tool's
    `inputSchema`. The forged function dispatches the call into the loop
    and returns the flattened text content as a string.

The forged callables are drop-in for Ivy's existing TOOLS_LIST.
"""

from __future__ import annotations

import asyncio
import json
import keyword
import re
import threading
from typing import Any, Callable, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


_loop: asyncio.AbstractEventLoop | None = None
_loop_ready = threading.Event()
_loop_lock = threading.Lock()


def _run_loop_forever() -> None:
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop_ready.set()
    _loop.run_forever()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            threading.Thread(target=_run_loop_forever, daemon=True, name="mcp-bridge-loop").start()
            if not _loop_ready.wait(timeout=5):
                raise RuntimeError("mcp_bridge: background asyncio loop failed to start")
    assert _loop is not None
    return _loop


def _run_sync(coro, timeout: float = 30.0):
    loop = _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


class _ServerHandle:
    """Holds the open stdio_client + ClientSession context managers indefinitely.

    We deliberately never `__aexit__` either — exiting would terminate the child
    process and cancel the receive task. The OS cleans them up when Ivy exits.
    """

    def __init__(self, command: str, args: list[str], cwd: str | None):
        self._command = command
        self._args = args
        self._cwd = cwd
        self._session: ClientSession | None = None
        self.tools: list[dict[str, Any]] = []

    async def _open(self) -> None:
        params = StdioServerParameters(command=self._command, args=self._args, cwd=self._cwd)
        self._stdio_cm = stdio_client(params)
        read, write = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()
        listed = await self._session.list_tools()
        self.tools = [
            {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema or {}}
            for t in listed.tools
        ]

    async def _call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise RuntimeError("mcp_bridge: session not opened")
        result = await self._session.call_tool(name, arguments)
        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
        if parts:
            return "\n".join(parts)
        # Fall back to a JSON dump if the server returned non-text blocks.
        try:
            return json.dumps(result.model_dump(), default=str)
        except Exception:
            return str(result)

    def open(self) -> None:
        _run_sync(self._open(), timeout=20.0)

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        return _run_sync(self._call(name, arguments), timeout=60.0)


_JSON_TO_PY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NO_DEFAULT = object()  # sentinel: schema declared an optional param with no explicit default


def _safe_ident(name: str) -> str:
    if not _IDENT_RE.match(name) or keyword.iskeyword(name):
        raise ValueError(f"mcp_bridge: unsafe identifier from MCP schema: {name!r}")
    return name


def _forge_wrapper(handle: _ServerHandle, tool: dict[str, Any]) -> Callable[..., str]:
    """Build a Python function whose signature mirrors the MCP tool's inputSchema."""
    name = _safe_ident(tool["name"])
    schema = tool.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    param_decls: list[str] = []
    arg_doc_lines: list[str] = []
    arg_dict_items: list[str] = []
    # Required first so Python signature is legal.
    ordered = sorted(properties.items(), key=lambda kv: (kv[0] not in required, kv[0]))
    for pname, pspec in ordered:
        pname_safe = _safe_ident(pname)
        ptype = _JSON_TO_PY.get((pspec or {}).get("type", "string"), "str")
        if pname in required:
            param_decls.append(f"{pname_safe}: {ptype}")
        else:
            # An MCP-optional param with no schema-declared default must NOT be
            # rendered as `name: str = None` — pydantic (used by google-genai to
            # introspect callables into tool schemas) rejects None as a non-Optional
            # default and the whole tool gets dropped from Gemini's toolbelt.
            # Use Optional[T] = None and filter None on dispatch.
            default = (pspec or {}).get("default", _NO_DEFAULT)
            if default is _NO_DEFAULT:
                param_decls.append(f"{pname_safe}: Optional[{ptype}] = None")
            else:
                param_decls.append(f"{pname_safe}: {ptype} = {default!r}")
        desc = (pspec or {}).get("description", "")
        arg_doc_lines.append(f"        {pname_safe}: {desc}".rstrip())
        arg_dict_items.append(f"{pname!r}: {pname_safe}")

    docstring = (tool.get("description") or f"MCP tool {name}.").strip()
    if arg_doc_lines:
        docstring += "\n\n    Args:\n" + "\n".join(arg_doc_lines)
    # Escape triple-quotes defensively before splicing into source.
    docstring_safe = docstring.replace('"""', '\\"\\"\\"')

    src = (
        f"def {name}({', '.join(param_decls)}) -> str:\n"
        f'    """{docstring_safe}"""\n'
        f"    _args = {{{', '.join(arg_dict_items)}}}\n"
        f"    _args = {{_k: _v for _k, _v in _args.items() if _v is not None}}\n"
        f"    return _handle.call({name!r}, _args)\n"
    )
    ns: dict[str, Any] = {"_handle": handle, "Optional": Optional}
    exec(src, ns)
    fn = ns[name]
    fn.__module__ = "mcp_bridge"
    fn._mcp_raw = tool
    return fn


def register_mcp_server(
    command: str,
    args: list[str],
    cwd: str | None = None,
) -> list[Callable[..., str]]:
    """Spawn an MCP stdio server, list its tools, return sync Python wrappers.

    The returned callables are suitable for Gemini's `tools=[...]` config and
    for Ivy's TOOL_REGISTRY name→callable map.
    """
    handle = _ServerHandle(command, args, cwd)
    handle.open()
    return [_forge_wrapper(handle, tool) for tool in handle.tools]
