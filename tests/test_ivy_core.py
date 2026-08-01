"""ivy_core package: require_env, AppleScript escaping/argv, JSON fence stripping."""

import sys

import pytest

from ivy_core.env import MissingEnvironmentVariable, require_env
from ivy_core.llm import strip_json_fence
from utils.applescript import AppleScriptRunner, escape_applescript_string


def test_require_env_raises_not_exits():
    """Must raise, not sys.exit — reusable library code can't call sys.exit."""
    with pytest.raises(MissingEnvironmentVariable):
        require_env("DEFINITELY_NOT_SET_XYZ_ABC_123")


def test_escape_applescript_string_escapes_backslash_before_quote():
    raw = 'a "quoted\\backslash" string'
    escaped = escape_applescript_string(raw)
    # Unescaping in reverse order must round-trip to the original.
    unescaped = escaped.replace('\\"', '"')
    unescaped = unescaped.replace("\\\\", "\\")
    assert unescaped == raw


@pytest.mark.macos_integration
@pytest.mark.skipif(sys.platform != "darwin", reason="osascript only exists on macOS")
def test_argv_round_trip_with_tricky_characters_real_osascript():
    """Real, non-mocked osascript call — proves argv passing is immune to
    injection regardless of what characters are in the content. Uses a
    script that never touches Messages.app."""
    runner = AppleScriptRunner()
    script = """
on run argv
    return item 1 of argv & "|" & item 2 of argv
end run
"""
    tricky = 'a "quoted\\backslash" string with \'apostrophes\''
    result = runner.run_argv(script, ["recipient value", tricky])
    assert result == "recipient value|" + tricky


@pytest.mark.parametrize("raw,expected", [
    ('```json\n[{"a":1}]\n```', '[{"a":1}]'),
    ('```\n[{"a":1}]\n```', '[{"a":1}]'),
    ('[{"a":1}]', '[{"a":1}]'),
    ('  ```json\n{"a":1}\n```  ', '{"a":1}'),
])
def test_strip_json_fence(raw, expected):
    assert strip_json_fence(raw) == expected


def test_query_llm_accepts_temperature_kwarg():
    """Regression test: query_llm used to have no `temperature` param at
    all, so every caller passing temperature=X raised TypeError on every
    single call. Both providers being unconfigured (empty keys, set by
    conftest) must not raise — just fall through to the final message."""
    from ivy_core.llm import query_llm

    result = query_llm("hello", temperature=0.7)
    assert isinstance(result, str)
    assert "unexpected keyword" not in result.lower()
