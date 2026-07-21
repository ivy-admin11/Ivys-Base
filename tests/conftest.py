"""Shared test fixtures.

Sets safe, hermetic env defaults BEFORE any application module is imported
by a test — config.py reads these at import time, so this must run first.
Never touches the real .env, real API keys, or the real receipts DB.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("ALLOW_INSECURE_ADMIN_SECRET", "true")
os.environ.setdefault("ADMIN_SECRET", "test-admin-secret")
os.environ.setdefault("HENRY_PHONE", "+15555550100")
os.environ.setdefault("LEXI_PHONE", "+15555550101")
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("XAI_API_KEY", "test-xai-key-not-real")
os.environ.setdefault("ODDS_API_KEY", "")
os.environ.setdefault("READWISE_API_KEY", "")
os.environ.setdefault("ENABLE_IMESSAGE_POLLER", "false")

import pytest  # noqa: E402


def pytest_configure(config):
    """Configure pytest with custom markers and skip rules."""
    # macOS integration tests require explicit opt-in via PYTEST_MACOS_INTEGRATION=1
    # They are skipped by default in CI and local development.
    if os.environ.get("PYTEST_MACOS_INTEGRATION") != "1":
        config.option.markexpr = "not macos_integration"


@pytest.fixture(autouse=True)
def isolated_receipts_db(tmp_path, monkeypatch):
    """Every test gets its own scratch SQLite file — never the real
    logs/executions.db."""
    from ivy_core import receipts

    monkeypatch.setattr(receipts, "DB_PATH", tmp_path / "test_executions.db")
    yield


@pytest.fixture
def admin_api_key():
    return os.environ["ADMIN_SECRET"]
