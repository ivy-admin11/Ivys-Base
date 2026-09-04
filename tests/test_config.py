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
        "ALLOW_INSECURE_ADMIN_SECRET": "true",
        # Contacts are required with no default (see the contact tests below);
        # supplied here so this test isolates the ADMIN_SECRET escape hatch.
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


# ---------------------------------------------------------------------------
# Contact configuration (B5)
# ---------------------------------------------------------------------------

def _run_config(tmp_path, env_overrides):
    """Import config.py with ONLY the given env, isolated from the repo .env."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("HENRY_PHONE", "LEXI_PHONE", "Henry_PHONE")
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", "import config; print(config.HENRY_PHONE)"],
        cwd=str(_isolated_config_dir(tmp_path)),
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_missing_contact_fails_loudly_instead_of_using_a_default(tmp_path):
    """A real phone number used to be the default here, so an unset variable
    silently delivered to it. There must be no fallback at all."""
    res = _run_config(tmp_path, {"ADMIN_SECRET": "x", "LEXI_PHONE": "+15555550101"})
    assert res.returncode != 0
    assert "HENRY_PHONE is not set" in res.stderr


def test_mis_cased_contact_key_is_not_silently_accepted(tmp_path):
    """The actual bug: .env said 'Henry_PHONE' while the code reads
    'HENRY_PHONE', so the override was ignored and the hardcoded default won.
    Editing .env appeared to do nothing."""
    res = _run_config(tmp_path, {
        "ADMIN_SECRET": "x",
        "LEXI_PHONE": "+15555550101",
        "Henry_PHONE": "+15555559999",   # wrong case — must NOT satisfy it
    })
    assert res.returncode != 0
    assert "case-sensitive" in res.stderr.lower()


def test_contacts_come_from_the_environment_when_set(tmp_path):
    res = _run_config(tmp_path, {
        "ADMIN_SECRET": "x",
        "HENRY_PHONE": "+15555550100",
        "LEXI_PHONE": "+15555550101",
    })
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "+15555550100"
