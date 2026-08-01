"""Tests for the deterministic operations-command handler and Tailscale status.

None of these tests invoke a real tailscale CLI, real subprocess calls, or any
LLM provider — every external call is patched.
"""

import json
import subprocess
from unittest.mock import MagicMock

import pytest

import main
from main import (
    _normalize_ops_text,
    handle_operations_command,
    get_tailscale_status,
)

SENDER = "+15555550100"


# ---------------------------------------------------------------------------
# _normalize_ops_text
# ---------------------------------------------------------------------------


def test_normalize_strips_whitespace():
    assert _normalize_ops_text("  status  ") == "status"


def test_normalize_collapses_internal_whitespace():
    assert _normalize_ops_text("health  check") == "health check"


def test_normalize_lowercases():
    assert _normalize_ops_text("TAILSCALE STATUS") == "tailscale status"


def test_normalize_strips_ivy_prefix():
    assert _normalize_ops_text("ivy status") == "status"


def test_normalize_strips_ivy_comma_prefix():
    assert _normalize_ops_text("ivy, tell me all of your skills") == "tell me all of your skills"


def test_normalize_strips_ivy_comma_no_space():
    assert _normalize_ops_text("ivy,status") == "status"


def test_normalize_no_prefix_unchanged():
    assert _normalize_ops_text("vpn status") == "vpn status"


# ---------------------------------------------------------------------------
# handle_operations_command — returns None for non-operations messages
# ---------------------------------------------------------------------------


def test_handle_returns_none_for_ordinary_message():
    result = handle_operations_command("hey what's the weather?", SENDER)
    assert result is None


def test_handle_returns_none_for_job_trigger():
    # "sharp picks" is a job command, not an operations command
    result = handle_operations_command("sharp picks", SENDER)
    assert result is None


# ---------------------------------------------------------------------------
# Tailscale routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", [
    "tailscale status",
    "Tailscale Status",
    "check tailscale",
    "is tailscale online",
    "tailscale",
    "vpn status",
    "network status",
])
def test_tailscale_phrases_routed_to_tailscale_handler(phrase, monkeypatch):
    monkeypatch.setattr(main, "get_tailscale_status", lambda: "🟢 Tailscale Status\nLocal device: test")
    result = handle_operations_command(phrase, SENDER)
    assert result is not None
    assert "Tailscale" in result


# ---------------------------------------------------------------------------
# Ivy / gateway status routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", [
    "ivy status",
    "ivy, ivy status",
    "status",
    "health check",
    "system health",
    "gateway status",
    "poller status",
    "is ivy online",
    "are you working",
])
def test_ivy_status_phrases_return_gateway_info(phrase):
    result = handle_operations_command(phrase, SENDER)
    assert result is not None
    assert "Ivy Gateway" in result or "Online" in result


# ---------------------------------------------------------------------------
# Capabilities / skills routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", [
    "skills",
    "ivy skills",
    "list skills",
    "tell me all your skills",
    "what can you do",
    "capabilities",
    "list capabilities",
    "what is turned on",
    "what things are turned on",
    "are all your skills active",
    "advise if all your skills are active",
    # With leading "ivy" / "ivy,"
    "ivy, tell me all of your skills",
    "ivy advise if all of your skills are active",
    "ivy, tell me what are all of the things that are turned on",
])
def test_capabilities_phrases_return_skills_list(phrase, monkeypatch):
    monkeypatch.setattr(main, "compute_tool_statuses", lambda: [
        {"tool_name": "check_apple_calendar", "status": "ready", "description": "Calendar"},
    ])
    result = handle_operations_command(phrase, SENDER)
    assert result is not None
    assert "Skills" in result or "Capabilities" in result or "Tools" in result


# ---------------------------------------------------------------------------
# Job status routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", [
    "sharp picks status",
    "last sharp picks",
    "last job",
    "recent jobs",
    "execution status",
])
def test_job_status_phrases_return_execution_history(phrase, monkeypatch):
    monkeypatch.setattr(main.receipts, "list_recent", lambda **_kw: [])
    result = handle_operations_command(phrase, SENDER)
    assert result is not None
    assert "📋" in result


def test_sharp_picks_status_filters_to_sharp_picks(monkeypatch):
    captured = {}

    def fake_list_recent(limit=50, job_name=None):  # noqa: ARG001
        captured["job_name"] = job_name
        return []

    monkeypatch.setattr(main.receipts, "list_recent", fake_list_recent)
    handle_operations_command("sharp picks status", SENDER)
    assert captured.get("job_name") == "sharp_picks"


def test_recent_jobs_does_not_filter_by_job(monkeypatch):
    captured = {}

    def fake_list_recent(limit=50, job_name=None):  # noqa: ARG001
        captured["job_name"] = job_name
        return []

    monkeypatch.setattr(main.receipts, "list_recent", fake_list_recent)
    handle_operations_command("recent jobs", SENDER)
    assert captured.get("job_name") is None


# ---------------------------------------------------------------------------
# get_tailscale_status — CLI missing
# ---------------------------------------------------------------------------


def test_tailscale_status_cli_not_found(monkeypatch):
    monkeypatch.setattr(main._shutil, "which", lambda _: None)
    result = get_tailscale_status()
    assert "unavailable" in result.lower()
    assert "tailscale CLI" in result


# ---------------------------------------------------------------------------
# get_tailscale_status — CLI timeout
# ---------------------------------------------------------------------------


def test_tailscale_status_timeout(monkeypatch):
    monkeypatch.setattr(main._shutil, "which", lambda _: "/usr/local/bin/tailscale")

    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["tailscale"], timeout=5)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    result = get_tailscale_status()
    assert "could not be read" in result.lower()


# ---------------------------------------------------------------------------
# get_tailscale_status — nonzero returncode
# ---------------------------------------------------------------------------


def test_tailscale_status_nonzero_returncode(monkeypatch):
    monkeypatch.setattr(main._shutil, "which", lambda _: "/usr/local/bin/tailscale")

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "error"
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: mock_result)

    result = get_tailscale_status()
    assert "could not be read" in result.lower()


# ---------------------------------------------------------------------------
# get_tailscale_status — valid JSON (running, peers)
# ---------------------------------------------------------------------------


def _make_tailscale_json(
    backend_state="Running",
    hostname="alexiss-imac",
    ip="100.113.29.14",
    peers=None,
    exit_node_id="",
):
    return json.dumps({
        "BackendState": backend_state,
        "Self": {
            "HostName": hostname,
            "TailscaleIPs": [ip],
        },
        "Peer": peers or {},
        "ExitNodeStatus": {"ID": exit_node_id},
    })


def test_tailscale_status_running_with_peers(monkeypatch):
    monkeypatch.setattr(main._shutil, "which", lambda _: "/usr/local/bin/tailscale")

    peers = {
        "abc123": {"HostName": "iphone171", "Online": True},
        "def456": {"HostName": "hillas", "Online": True},
        "ghi789": {"HostName": "offline-device", "Online": False},
    }
    payload = _make_tailscale_json(peers=peers)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = payload
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: mock_result)

    result = get_tailscale_status()
    assert "🟢" in result
    assert "alexiss-imac" in result
    assert "100.113.29.14" in result
    assert "Running" in result
    assert "Exit node: None" in result
    assert "Online peers: 2" in result
    assert "• iphone171" in result
    assert "• hillas" in result
    # offline device must not appear
    assert "offline-device" not in result


def test_tailscale_status_not_running_shows_orange(monkeypatch):
    monkeypatch.setattr(main._shutil, "which", lambda _: "/usr/local/bin/tailscale")

    payload = _make_tailscale_json(backend_state="Stopped")
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = payload
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: mock_result)

    result = get_tailscale_status()
    assert "🟠" in result


def test_tailscale_status_exit_node_resolved(monkeypatch):
    monkeypatch.setattr(main._shutil, "which", lambda _: "/usr/local/bin/tailscale")

    peers = {
        "node-exit-1": {"ID": "exit-node-id-1", "HostName": "exit-server", "Online": True},
    }
    payload = json.dumps({
        "BackendState": "Running",
        "Self": {"HostName": "myhost", "TailscaleIPs": ["100.1.2.3"]},
        "Peer": peers,
        "ExitNodeStatus": {"ID": "exit-node-id-1"},
    })
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = payload
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: mock_result)

    result = get_tailscale_status()
    assert "exit-server" in result


def test_tailscale_status_no_auth_keys_exposed(monkeypatch):
    """Auth keys, machine keys, node keys must not appear in the output."""
    monkeypatch.setattr(main._shutil, "which", lambda _: "/usr/local/bin/tailscale")

    payload = json.dumps({
        "BackendState": "Running",
        "Self": {
            "HostName": "myhost",
            "TailscaleIPs": ["100.1.2.3"],
            "NodeKey": "nodekey:super-secret-key-abc123",
            "MachineKey": "mkey:machine-secret-abc123",
            "AuthKey": "tskey-auth-super-secret",
        },
        "Peer": {},
        "ExitNodeStatus": {"ID": ""},
    })
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = payload
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: mock_result)

    result = get_tailscale_status()
    assert "nodekey:" not in result
    assert "mkey:" not in result
    assert "tskey-auth" not in result
    assert "super-secret" not in result


def test_tailscale_status_uses_no_shell_true(monkeypatch):
    """subprocess.run must be called without shell=True."""
    monkeypatch.setattr(main._shutil, "which", lambda _: "/usr/local/bin/tailscale")

    calls = []

    def mock_run(*_args, **kwargs):
        calls.append(kwargs)
        r = MagicMock()
        r.returncode = 1
        r.stdout = ""
        return r

    monkeypatch.setattr(subprocess, "run", mock_run)
    get_tailscale_status()
    assert calls
    assert not calls[0].get("shell", False)
