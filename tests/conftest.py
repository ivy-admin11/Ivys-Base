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

os.environ["ALLOW_INSECURE_ADMIN_SECRET"] = "true"  # pragma: allowlist secret
os.environ["ADMIN_SECRET"] = "test-admin-secret"  # pragma: allowlist secret
os.environ["HENRY_PHONE"] = "+15555550100"
os.environ["LEXI_PHONE"] = "+15555550101"
os.environ["GEMINI_API_KEY"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["XAI_API_KEY"] = "test-xai-key-not-real"  # pragma: allowlist secret
os.environ["ODDS_API_KEY"] = ""
os.environ["READWISE_API_KEY"] = ""
os.environ["ENABLE_IMESSAGE_POLLER"] = "false"

import pytest  # noqa: E402


def pytest_configure(config):
    """Configure pytest with custom markers and skip rules."""
    # Real macOS integration tests require explicit opt-in.  The normal Linux
    # suite skips them; the dedicated hosted-macOS safety job opts in without
    # exercising Messages.app or any live delivery path.
    if os.environ.get("PYTEST_MACOS_INTEGRATION") != "1":
        config.option.markexpr = "not macos_integration"


@pytest.fixture(autouse=True)
def isolated_receipts_db(tmp_path, monkeypatch):
    """Every test gets its own scratch SQLite file — never the real
    logs/executions.db."""
    from ivy_core import receipts

    monkeypatch.setattr(receipts, "DB_PATH", tmp_path / "test_executions.db")
    yield


@pytest.fixture(autouse=True)
def isolated_picks_db(tmp_path, monkeypatch):
    """Every test gets its own scratch SQLite file — never the real
    data/picks.db."""
    from ivy_core import picks_tracker

    monkeypatch.setattr(picks_tracker, "PICKS_DB", tmp_path / "test_picks.db")
    yield


@pytest.fixture(autouse=True)
def isolated_imessage_worker_db(tmp_path, monkeypatch):
    """Tests never create or update the production collector cursor journal."""
    from ivy_core.imessage_state import InboxStateStore

    if "main" in sys.modules:
        monkeypatch.setattr(
            sys.modules["main"],
            "_IMESSAGE_STATE",
            InboxStateStore(tmp_path / "test_imessage_worker.db"),
        )
    yield


@pytest.fixture
def admin_api_key():
    return os.environ["ADMIN_SECRET"]
