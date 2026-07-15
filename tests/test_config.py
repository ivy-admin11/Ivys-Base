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
    env = {"PATH": os.environ.get("PATH", ""), "ALLOW_INSECURE_ADMIN_SECRET": "true"}
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
        "ENABLE_SPORTS_PICKS",
    ):
        assert hasattr(config, name), f"config.py missing canonical var {name}"


def test_optional_keys_do_not_raise_when_missing():
    """Missing optional provider keys must disable only the dependent
    capability, never crash the import."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "ADMIN_SECRET": "x",
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
