"""config.py: fail-closed ADMIN_SECRET, canonical env vars, .env load order."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _isolated_config_dir(tmp_path):
    """Copy config.py into a directory with no .env file, so
    load_dotenv() has nothing to load — the real repo root always has a
    real .env with a real ADMIN_SECRET, which would defeat this test if we
    ran it from there instead."""
    shutil.copy(REPO_ROOT / "config.py", tmp_path / "config.py")
    return tmp_path


def test_admin_secret_fails_closed_when_unset(tmp_path):
    isolated_dir = _isolated_config_dir(tmp_path)
    env = {"PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=str(isolated_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "ADMIN_SECRET is not set" in result.stderr


def test_admin_secret_escape_hatch_allows_import(tmp_path):
    isolated_dir = _isolated_config_dir(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "ALLOW_INSECURE_ADMIN_SECRET": "true",  # pragma: allowlist secret
        "HENRY_PHONE": "+15555550100",
        "LEXI_PHONE": "+15555550101",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.ADMIN_SECRET)"],
        cwd=str(isolated_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "insecure-test-secret" in result.stdout


def test_canonical_env_vars_present():
    import config

    for name in (
        "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "ODDS_API_KEY",
        "READWISE_API_KEY", "ADMIN_SECRET", "HENRY_PHONE", "LEXI_PHONE",
        "ENABLE_IMESSAGE_POLLER", "ENABLE_CALENDAR_INTEGRATION",
        "ENABLE_REMINDERS_INTEGRATION", "ENABLE_READWISE_INTEGRATION",
        "ENABLE_SPORTS_PICKS", "IMESSAGE_FETCH_BATCH_SIZE",
        "IMESSAGE_QUEUE_MAXSIZE", "IMESSAGE_SLOW_QUEUE_MAXSIZE",
        "IMESSAGE_DEBOUNCE_SECONDS", "IMESSAGE_QUEUE_PUT_TIMEOUT_SECONDS",
        "IMESSAGE_STALE_QUEUE_SECONDS", "IMESSAGE_WORKER_JOIN_TIMEOUT_SECONDS",
        "IMESSAGE_SLOW_ACK_SECONDS",
        "IMESSAGE_SEND_TIMEOUT_SECONDS", "APPLE_CALENDAR_TIMEOUT_SECONDS",
        "APPLE_REMINDERS_TIMEOUT_SECONDS", "STATUS_COMMAND_TIMEOUT_SECONDS",
        "JOB_HEARTBEAT_SECONDS", "JOB_MAX_RUNTIME_SECONDS",
    ):
        assert hasattr(config, name), f"config.py missing canonical var {name}"


def test_optional_keys_do_not_raise_when_missing():
    """Missing optional provider keys must disable only the dependent
    capability, never crash the import."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "ADMIN_SECRET": "x",
        "HENRY_PHONE": "+15555550100",
        "LEXI_PHONE": "+15555550101",
        "GEMINI_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "XAI_API_KEY": "",
        "ODDS_API_KEY": "",
        "READWISE_API_KEY": "",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import config; print('ok')"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_production_dependencies_are_exactly_pinned():
    requirements = (REPO_ROOT / "requirements.txt").read_text().splitlines()
    direct_requirements = [
        line.strip()
        for line in requirements
        if line.strip() and not line.lstrip().startswith(("#", "-r "))
    ]

    assert direct_requirements
    assert all("==" in requirement for requirement in direct_requirements)
    assert "google-genai==1.2.0" in direct_requirements
    assert "httpx==0.26.0" in direct_requirements
    assert "pydantic==2.13.4" in direct_requirements
    assert "filelock==3.32.2" in direct_requirements


def test_env_example_documents_bounded_imessage_settings():
    env_example = (REPO_ROOT / ".env.example").read_text().splitlines()
    settings = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in env_example
        if line and not line.startswith("#") and "=" in line
    }

    expected_defaults = {
        "IMESSAGE_FETCH_BATCH_SIZE": "20",
        "IMESSAGE_QUEUE_MAXSIZE": "100",
        "IMESSAGE_SLOW_QUEUE_MAXSIZE": "50",
        "IMESSAGE_DEBOUNCE_SECONDS": "2.0",
        "IMESSAGE_QUEUE_PUT_TIMEOUT_SECONDS": "1.0",
        "IMESSAGE_STALE_QUEUE_SECONDS": "30",
        "IMESSAGE_WORKER_JOIN_TIMEOUT_SECONDS": "5",
        "IMESSAGE_SLOW_ACK_SECONDS": "5.0",
        "IMESSAGE_SEND_TIMEOUT_SECONDS": "10",
        "APPLE_CALENDAR_TIMEOUT_SECONDS": "8",
        "APPLE_REMINDERS_TIMEOUT_SECONDS": "8",
        "STATUS_COMMAND_TIMEOUT_SECONDS": "5",
        "IVY_JOB_HEARTBEAT_SECONDS": "15",
        "IVY_JOB_MAX_RUNTIME_SECONDS": "3600",
    }

    assert {name: settings.get(name) for name in expected_defaults} == expected_defaults
    assert settings["OPENAI_API_KEY"] == "your_openai_api_key_here"  # pragma: allowlist secret
