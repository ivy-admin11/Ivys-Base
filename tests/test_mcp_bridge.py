"""Tests for the MCP-stdio -> sync-Python-callable bridge.

mcp_bridge had no tests. It is the seam where a *third party's* JSON schema is
turned into Python source text and handed to exec(). Every tool name, parameter
name, description and default in that schema is written by whoever wrote the MCP
server, not by us, and a single awkward character in any of them used to be a
SyntaxError at exec() time -- which does not degrade one tool, it aborts
register_mcp_server and silently costs Ivy the *entire* server's toolbelt.

Nothing here spawns a subprocess or opens a socket: _ServerHandle is either
replaced outright by a recorder or driven through asyncio.run() with a fake
ClientSession.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import sys
import types
from typing import Any, Optional, get_args, get_origin

import pytest

# The real `mcp` package is not a test dependency (importing it is also the
# thing that drags in the stdio subprocess machinery), so stand in a stub before
# mcp_bridge is imported. Only if it is genuinely absent -- never shadow a real
# install.
if "mcp" not in sys.modules:
    try:
        import mcp  # noqa: F401
    except ImportError:
        _mcp = types.ModuleType("mcp")

        class _StubClientSession:  # pragma: no cover - never instantiated
            def __init__(self, read, write):
                self._read = read
                self._write = write

        class _StubStdioServerParameters:  # pragma: no cover - never instantiated
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        _mcp.ClientSession = _StubClientSession
        _mcp.StdioServerParameters = _StubStdioServerParameters
        _mcp_client = types.ModuleType("mcp.client")
        _mcp_stdio = types.ModuleType("mcp.client.stdio")

        def _stub_stdio_client(params):  # pragma: no cover - guard, not a path
            raise AssertionError("tests must never spawn a real MCP stdio server")

        _mcp_stdio.stdio_client = _stub_stdio_client
        _mcp_client.stdio = _mcp_stdio
        _mcp.client = _mcp_client
        sys.modules["mcp"] = _mcp
        sys.modules["mcp.client"] = _mcp_client
        sys.modules["mcp.client.stdio"] = _mcp_stdio

import mcp_bridge  # noqa: E402


class RecordingHandle:
    """Stands in for _ServerHandle so a forged wrapper can be called for real.

    The forged function's whole job is to marshal keyword arguments into the
    dict it hands to handle.call(), so recording that dict is the only way to
    test the marshalling without a live server.
    """

    def __init__(self, result: str = "ok"):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        return self._result


def tool(name: str = "do_thing", description: str = "Does a thing.", **schema) -> dict[str, Any]:
    """One entry as _ServerHandle.tools stores it."""
    return {
        "name": name,
        "description": description,
        "inputSchema": schema or {"type": "object", "properties": {}, "required": []},
    }


class TestSafeIdent:
    @pytest.mark.parametrize("name", ["search", "_private", "get_weather2", "A"])
    def test_accepts_plain_identifiers(self, name):
        assert mcp_bridge._safe_ident(name) == name

    @pytest.mark.parametrize("name", ["read-file", "2fast", "with space", "", "a.b", "tool!"])
    def test_rejects_anything_that_is_not_a_bare_identifier(self, name):
        # These become part of generated source; anything else is an injection
        # vector or an outright SyntaxError.
        with pytest.raises(ValueError):
            mcp_bridge._safe_ident(name)

    @pytest.mark.parametrize("name", ["class", "import", "lambda", "None"])
    def test_rejects_python_keywords(self, name):
        # `def class(...)` is a SyntaxError; `def None(...)` too.
        with pytest.raises(ValueError):
            mcp_bridge._safe_ident(name)


class TestForgedSignature:
    def test_required_params_come_first_so_the_signature_is_legal(self):
        # Python forbids a non-default parameter after a defaulted one; the
        # schema's property order gives no such guarantee.
        fn = mcp_bridge._forge_wrapper(
            RecordingHandle(),
            tool(
                type="object",
                properties={
                    "optional_a": {"type": "string"},
                    "required_z": {"type": "string"},
                    "optional_b": {"type": "string"},
                    "required_a": {"type": "string"},
                },
                required=["required_z", "required_a"],
            ),
        )
        names = list(inspect.signature(fn).parameters)
        assert names[:2] == ["required_a", "required_z"]
        assert set(names[2:]) == {"optional_a", "optional_b"}

    def annotations(self, fn):
        """mcp_bridge does `from __future__ import annotations`, and exec()
        inherits the caller's future flags -- so forged annotations are strings
        until they are evaluated against the forged function's own globals."""
        return {
            name: p.annotation
            for name, p in inspect.signature(fn, eval_str=True).parameters.items()
        }

    def test_json_types_map_to_python_annotations(self):
        fn = mcp_bridge._forge_wrapper(
            RecordingHandle(),
            tool(
                type="object",
                properties={
                    "s": {"type": "string"},
                    "i": {"type": "integer"},
                    "n": {"type": "number"},
                    "b": {"type": "boolean"},
                    "a": {"type": "array"},
                    "o": {"type": "object"},
                },
                required=["s", "i", "n", "b", "a", "o"],
            ),
        )
        ann = self.annotations(fn)
        assert [ann[k] for k in "sinbao"] == [str, int, float, bool, list, dict]

    def test_unknown_json_type_falls_back_to_str(self):
        fn = mcp_bridge._forge_wrapper(
            RecordingHandle(),
            tool(type="object", properties={"x": {"type": "widget"}}, required=["x"]),
        )
        assert self.annotations(fn)["x"] is str

    def test_optional_without_default_is_Optional_not_bare(self):
        fn = mcp_bridge._forge_wrapper(
            RecordingHandle(),
            tool(type="object", properties={"q": {"type": "string"}}, required=[]),
        )
        param = inspect.signature(fn).parameters["q"]
        assert param.default is None
        annotation = self.annotations(fn)["q"]
        assert get_origin(annotation) is not None
        assert set(get_args(annotation)) == {str, type(None)}

    def test_explicit_json_null_default_is_also_Optional(self):
        """REGRESSION. A schema may spell an optional parameter out as
        `{"type": "string", "default": null}`, which means exactly what omitting
        the default means. The code took the else-branch on it and emitted
        `q: str = None` -- the precise shape its own comment says pydantic
        (google-genai's introspector) rejects, which drops the tool from
        Gemini's toolbelt with no error anyone sees.
        """
        fn = mcp_bridge._forge_wrapper(
            RecordingHandle(),
            tool(type="object", properties={"q": {"type": "string", "default": None}}, required=[]),
        )
        param = inspect.signature(fn).parameters["q"]
        assert param.default is None
        annotation = self.annotations(fn)["q"]
        assert annotation is not str, "emitted `q: str = None`, the pydantic-hostile shape"
        assert set(get_args(annotation)) == {str, type(None)}

    def test_schema_default_is_preserved_and_used_when_the_arg_is_omitted(self):
        handle = RecordingHandle()
        fn = mcp_bridge._forge_wrapper(
            handle,
            tool(
                type="object",
                properties={"limit": {"type": "integer", "default": 25}},
                required=[],
            ),
        )
        assert inspect.signature(fn).parameters["limit"].default == 25
        fn()
        assert handle.calls == [("do_thing", {"limit": 25})]

    def test_string_default_containing_quotes_survives(self):
        handle = RecordingHandle()
        fn = mcp_bridge._forge_wrapper(
            handle,
            tool(
                type="object",
                properties={"sep": {"type": "string", "default": 'a"b\'c\\d'}},
                required=[],
            ),
        )
        fn()
        assert handle.calls[0][1]["sep"] == 'a"b\'c\\d'

    def test_non_finite_number_default_does_not_kill_the_whole_server(self):
        """REGRESSION. json.loads() turns a bare `NaN`/`Infinity` token into a
        Python float, whose repr is `nan`/`inf`. Splicing that repr into the
        generated `def` made it a NameError at exec() time, and the exception
        propagates out of register_mcp_server -- so one odd default cost every
        tool the server advertises, not just this one.
        """
        handle = RecordingHandle()
        fn = mcp_bridge._forge_wrapper(
            handle,
            tool(
                type="object",
                properties={"threshold": {"type": "number", "default": float("inf")}},
                required=[],
            ),
        )
        fn()
        assert math.isinf(handle.calls[0][1]["threshold"])


class TestForgedDocstring:
    def test_description_becomes_the_docstring(self):
        fn = mcp_bridge._forge_wrapper(RecordingHandle(), tool(description="Reads a file."))
        assert fn.__doc__.startswith("Reads a file.")

    def test_missing_description_gets_a_placeholder(self):
        fn = mcp_bridge._forge_wrapper(RecordingHandle(), tool(name="zap", description=""))
        assert fn.__doc__ == "MCP tool zap."

    def test_parameter_descriptions_are_listed_under_Args(self):
        # google-genai reads the docstring for the per-parameter descriptions it
        # advertises to the model, so dropping them degrades tool selection.
        fn = mcp_bridge._forge_wrapper(
            RecordingHandle(),
            tool(
                type="object",
                properties={"path": {"type": "string", "description": "Absolute path."}},
                required=["path"],
            ),
        )
        assert "Args:" in fn.__doc__
        assert "path: Absolute path." in fn.__doc__

    def test_description_ending_in_a_double_quote(self):
        """REGRESSION. The description was spliced between triple quotes in
        generated source. A description ending in `"` closed the literal one
        character early and left a dangling quote -- SyntaxError out of exec(),
        which aborts register_mcp_server for the whole server.
        """
        fn = mcp_bridge._forge_wrapper(RecordingHandle(), tool(description='Search for "stuff"'))
        assert fn.__doc__ == 'Search for "stuff"'

    def test_description_containing_a_triple_quote(self):
        fn = mcp_bridge._forge_wrapper(RecordingHandle(), tool(description='Fence: """ here'))
        assert fn.__doc__ == 'Fence: """ here'

    @pytest.mark.parametrize(
        "description",
        [
            r"Match \x in the input",          # invalid \x escape -> SyntaxError
            r"Windows path C:\users\new",      # \u escape -> SyntaxError
            "Trailing backslash \\",           # escapes the closing delimiter
        ],
    )
    def test_backslashes_in_a_description_do_not_break_forging(self, description):
        """REGRESSION. Backslash sequences in a third-party description were
        interpreted as Python string escapes once spliced into source: `\\x` and
        `\\u` are hard SyntaxErrors, and a trailing backslash swallowed the
        function body into the docstring.
        """
        fn = mcp_bridge._forge_wrapper(RecordingHandle(), tool(description=description))
        assert fn.__doc__ == description
        assert callable(fn)

    def test_literal_backslash_n_is_not_turned_into_a_newline(self):
        # `\n` written literally in a description described the server's output
        # format; splicing turned it into an actual line break.
        fn = mcp_bridge._forge_wrapper(RecordingHandle(), tool(description=r"Joined with \n"))
        assert "\n" not in fn.__doc__
        assert fn.__doc__ == r"Joined with \n"


class TestForgedDispatch:
    def test_calls_the_handle_under_the_mcp_tool_name(self):
        handle = RecordingHandle(result="server said hi")
        fn = mcp_bridge._forge_wrapper(
            handle,
            tool(name="fetch", type="object", properties={"url": {"type": "string"}}, required=["url"]),
        )
        assert fn(url="http://x") == "server said hi"
        assert handle.calls == [("fetch", {"url": "http://x"})]

    def test_unset_optionals_are_dropped_from_the_wire_payload(self):
        # Sending `{"cursor": null}` to a server that validates its own schema is
        # not the same as omitting the key.
        handle = RecordingHandle()
        fn = mcp_bridge._forge_wrapper(
            handle,
            tool(
                type="object",
                properties={"q": {"type": "string"}, "cursor": {"type": "string"}},
                required=["q"],
            ),
        )
        fn(q="hello")
        assert handle.calls[0][1] == {"q": "hello"}

    @pytest.mark.parametrize("value", [False, 0, "", [], {}])
    def test_falsy_but_present_arguments_are_kept(self, value):
        # The filter is `is not None`, not truthiness -- `limit=0` and
        # `verbose=False` are real, meaningful arguments.
        handle = RecordingHandle()
        fn = mcp_bridge._forge_wrapper(
            handle,
            tool(type="object", properties={"v": {"type": "string"}}, required=[]),
        )
        fn(v=value)
        assert handle.calls[0][1] == {"v": value}

    def test_positional_call_works_too(self):
        handle = RecordingHandle()
        fn = mcp_bridge._forge_wrapper(
            handle,
            tool(type="object", properties={"a": {"type": "string"}}, required=["a"]),
        )
        fn("x")
        assert handle.calls[0][1] == {"a": "x"}

    def test_raw_tool_dict_is_attached_for_debugging(self):
        raw = tool()
        fn = mcp_bridge._forge_wrapper(RecordingHandle(), raw)
        assert fn._mcp_raw is raw
        assert fn.__module__ == "mcp_bridge"


class Block:
    """An MCP content block. Only text blocks carry a `.text`."""

    def __init__(self, text=None):
        if text is not None:
            self.text = text


class FakeResult:
    def __init__(self, content, dump=None):
        self.content = content
        self._dump = dump if dump is not None else {"content": "opaque"}

    def model_dump(self):
        return self._dump


class FakeSession:
    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._result


class TestServerHandleCall:
    """_call is driven directly through asyncio.run -- no background loop, no
    child process, no stdio."""

    def handle(self, result):
        h = mcp_bridge._ServerHandle("never-run", [], None)
        h._session = FakeSession(result)
        return h

    def test_text_blocks_are_joined_with_newlines(self):
        h = self.handle(FakeResult([Block("line one"), Block("line two")]))
        assert asyncio.run(h._call("t", {"a": 1})) == "line one\nline two"
        assert h._session.calls == [("t", {"a": 1})]

    def test_non_text_blocks_are_skipped(self):
        h = self.handle(FakeResult([Block(), Block("kept"), Block()]))
        assert asyncio.run(h._call("t", {})) == "kept"

    def test_all_non_text_falls_back_to_a_json_dump(self):
        # Gemini needs a *string* back; returning "" would look like a
        # successful empty answer.
        h = self.handle(FakeResult([Block()], dump={"images": 1}))
        assert asyncio.run(h._call("t", {})) == '{"images": 1}'

    def test_unserialisable_dump_falls_back_to_str(self):
        class Exploding(FakeResult):
            def model_dump(self):
                raise RuntimeError("nope")

        h = self.handle(Exploding([Block()]))
        assert isinstance(asyncio.run(h._call("t", {})), str)

    def test_empty_string_block_is_preserved_not_treated_as_absent(self):
        h = self.handle(FakeResult([Block("")]))
        assert asyncio.run(h._call("t", {})) == ""

    def test_calling_before_open_raises_instead_of_returning_nothing(self):
        h = mcp_bridge._ServerHandle("never-run", [], None)
        with pytest.raises(RuntimeError, match="session not opened"):
            asyncio.run(h._call("t", {}))


class TestRegisterMcpServer:
    def test_returns_one_callable_per_advertised_tool(self, monkeypatch):
        opened: list[tuple[str, list, Optional[str]]] = []

        def fake_open(self):
            opened.append((self._command, self._args, self._cwd))
            self.tools = [
                tool(name="alpha", description="A."),
                tool(name="beta", description="B."),
            ]

        monkeypatch.setattr(mcp_bridge._ServerHandle, "open", fake_open)
        fns = mcp_bridge.register_mcp_server("node", ["server.js"], cwd="/srv")

        assert opened == [("node", ["server.js"], "/srv")]
        assert [f.__name__ for f in fns] == ["alpha", "beta"]
        assert all(callable(f) for f in fns)

    def test_a_server_with_no_tools_yields_an_empty_list_not_an_error(self, monkeypatch):
        monkeypatch.setattr(mcp_bridge._ServerHandle, "open", lambda self: None)
        assert mcp_bridge.register_mcp_server("node", []) == []

    def test_every_wrapper_shares_the_one_handle(self, monkeypatch):
        # Two handles would mean two child processes for one server.
        handles: list[Any] = []

        def fake_open(self):
            handles.append(self)
            self.tools = [tool(name="a"), tool(name="b")]

        monkeypatch.setattr(mcp_bridge._ServerHandle, "open", fake_open)
        fns = mcp_bridge.register_mcp_server("node", [])
        assert len(handles) == 1
        for fn in fns:
            assert fn.__globals__["_handle"] is handles[0]

    def test_a_hyphenated_tool_name_aborts_the_whole_server(self, monkeypatch):
        """Documents a real limitation rather than a fix: `read-file` is a legal
        MCP tool name but not a Python identifier, and _safe_ident raises, so
        register_mcp_server loses every tool on that server. Loud, at least --
        but if this ever needs to be relaxed, note that the forged body dispatches
        under the *sanitised* name, so the original wire name would have to be
        carried through separately.
        """
        def fake_open(self):
            self.tools = [tool(name="ok"), tool(name="read-file")]

        monkeypatch.setattr(mcp_bridge._ServerHandle, "open", fake_open)
        with pytest.raises(ValueError, match="unsafe identifier"):
            mcp_bridge.register_mcp_server("node", [])


class TestBackgroundLoop:
    def test_run_sync_drives_a_coroutine_to_completion(self):
        async def add():
            await asyncio.sleep(0)
            return 41 + 1

        assert mcp_bridge._run_sync(add(), timeout=5.0) == 42

    def test_the_loop_is_created_once_and_reused(self):
        async def noop():
            return None

        mcp_bridge._run_sync(noop(), timeout=5.0)
        first = mcp_bridge._ensure_loop()
        mcp_bridge._run_sync(noop(), timeout=5.0)
        assert mcp_bridge._ensure_loop() is first
        assert first.is_running()

    def test_exceptions_propagate_across_the_thread_boundary(self):
        # A tool fault must surface, not come back as a silent None.
        async def boom():
            raise ValueError("server exploded")

        with pytest.raises(ValueError, match="server exploded"):
            mcp_bridge._run_sync(boom(), timeout=5.0)
