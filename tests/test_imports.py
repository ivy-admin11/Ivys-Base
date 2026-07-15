"""Every module here must import cleanly without optional secrets — a
fresh clone with conftest's hermetic env (no real API keys) must be able
to import all of these without crashing."""

import pytest


@pytest.mark.parametrize("module_name", [
    "main",
    "config",
    "registry",
    "job_runner",
    "ivy_core",
    "ivy_core.env",
    "ivy_core.llm",
    "ivy_core.messaging",
    "ivy_core.receipts",
    "utils.applescript",
    "proactive_agents.sports_bettor",
    "proactive_agents.happy_hour_scout",
    "proactive_agents.Familia_meal_planner",
])
def test_module_imports_cleanly(module_name):
    __import__(module_name)


def test_tools_agent_services_packages_no_longer_exist():
    """Regression test: tools/, agent/, services/ were dead, unimported,
    mutually-referencing packages superseded by registry.py + job_runner.py.
    If one of these comes back, something is duplicating the registry
    again."""
    import importlib

    for dead_package in ("tools", "agent", "services"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(dead_package)
