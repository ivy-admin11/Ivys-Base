"""registry.py: one canonical tool schema, rendered correctly for both providers."""

from registry import DEEPSEEK_TOOL_SCHEMA, GEMINI_TOOL_DECLARATIONS, TOOL_SPECS


def test_both_schemas_declare_the_same_tool_names():
    gemini_names = {d["name"] for d in GEMINI_TOOL_DECLARATIONS}
    deepseek_names = {d["function"]["name"] for d in DEEPSEEK_TOOL_SCHEMA}
    assert gemini_names == deepseek_names


def test_deepseek_schema_includes_fetch_apple_reminders():
    """Regression test: the old hand-synced DEEPSEEK_TOOL_SCHEMA was
    missing fetch_apple_reminders entirely, so DeepSeek could never satisfy
    a 'read my reminders' request Gemini could handle."""
    names = {d["function"]["name"] for d in DEEPSEEK_TOOL_SCHEMA}
    assert "fetch_apple_reminders" in names


def test_run_job_tool_present_in_both_schemas():
    assert any(d["name"] == "run_job" for d in GEMINI_TOOL_DECLARATIONS)
    assert any(d["function"]["name"] == "run_job" for d in DEEPSEEK_TOOL_SCHEMA)


def test_gemini_declarations_use_uppercase_json_types():
    calendar = next(d for d in GEMINI_TOOL_DECLARATIONS if d["name"] == "check_apple_calendar")
    assert calendar["parameters"]["properties"]["timeframe"]["type"] == "STRING"


def test_deepseek_schema_uses_lowercase_json_types_and_function_wrapper():
    calendar = next(d for d in DEEPSEEK_TOOL_SCHEMA if d["function"]["name"] == "check_apple_calendar")
    assert calendar["type"] == "function"
    assert calendar["function"]["parameters"]["properties"]["timeframe"]["type"] == "string"


def test_required_params_rendered_correctly():
    add_reminder = next(spec for spec in TOOL_SPECS if spec.name == "add_apple_reminder")
    required_names = {p.name for p in add_reminder.params if p.required}
    assert required_names == {"title"}
    assert "list_name" not in required_names
