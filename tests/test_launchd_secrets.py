"""No launchd plist may carry a credential.

com.ivy.result-updater held a live ODDS_API_KEY inline from 21 July to
9 September. ~/Library/LaunchAgents is mode 644, so it was readable by every
process on the machine for seven weeks. It also silently beat the key in .env,
because config.py loads with override=False, so that job ran on a different
credential from every other agent and nobody could see why.

It was inline for an understandable reason — result_updater read os.environ and
nothing else, so the plist was the only way it ever saw a key. That is fixed,
but the reason it went unnoticed for seven weeks is that nothing looked. This
looks, at both the templates in the repo and whatever is actually installed on
this machine, because the two drift apart exactly when it matters.
"""
from __future__ import annotations

import os
import plistlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "deploy" / "launchd"
INSTALLED = Path(os.path.expanduser("~/Library/LaunchAgents"))

# A plist legitimately sets PATH, HOME, PYTHONPATH and feature flags like
# ENABLE_IMESSAGE_POLLER=true. A name allowlist would have to grow every time
# one of those is added and would be edited away the first time it got in
# someone's way. Two narrower signals catch the real thing instead:
#
#   the NAME reads like a secret, and it has a value that is not a placeholder
#   the VALUE reads like a credential, whatever it is called
#
# ODDS_API_KEY=<32 hex> trips both. ENABLE_IMESSAGE_POLLER=true trips neither.
_SECRET_NAME = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|APIKEY|AUTH)", re.IGNORECASE
)

# Long enough and unstructured enough to be a real credential. Placeholders
# (__PROJECT_ROOT__, /usr/bin:/bin, your_api_key_here, true) do not match.
_SECRET_VALUE = re.compile(r"^[A-Za-z0-9+/_-]{20,}$")


def _looks_like_a_path(value: str) -> bool:
    """PYTHONPATH and PATH are long, unstructured and entirely innocent.

    The value pattern below allows "/" because base64 credentials contain it,
    which means an absolute path matches too — PYTHONPATH=/Users/lexi/openclaw
    -admin tripped this guard on its first run. A credential does not begin
    with a slash or a tilde, and does not arrive as a colon-separated list.
    """
    v = value.strip()
    return v.startswith("/") or v.startswith("~") or ":" in v


def _looks_like_a_placeholder(value: str) -> bool:
    v = value.strip()
    return (
        not v
        or v.startswith("__")
        or v.startswith("REDACTED")
        or "your_" in v.lower()
        or "_here" in v.lower()
    )


def _env_offenders(label: str, data: dict) -> list[str]:
    out = []
    env = data.get("EnvironmentVariables") or {}
    for key, value in env.items():
        value = str(value)
        if _looks_like_a_placeholder(value) or _looks_like_a_path(value):
            continue
        if _SECRET_NAME.search(key):
            out.append(f"{label}: EnvironmentVariables/{key} reads as a secret and has a value")
        elif _SECRET_VALUE.match(value.strip()):
            out.append(f"{label}: EnvironmentVariables/{key} holds a credential-shaped value")
    return out


def _value_offenders(label: str, raw: str) -> list[str]:
    """Catch a credential anywhere in the file, not only under EnvironmentVariables."""
    out = []
    for match in re.findall(r"<string>([^<]*)</string>", raw):
        value = match.strip()
        if "/" in value or "." in value or value.startswith("__"):
            continue  # paths, labels, module names, render placeholders
        if _SECRET_VALUE.match(value) and not _looks_like_a_placeholder(value):
            out.append(f"{label}: a <string> value looks like a credential")
    return out


def _templates() -> list[Path]:
    return sorted(TEMPLATES.glob("*.plist.template"))


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_no_template_carries_a_credential(path: Path) -> None:
    raw = path.read_text()
    data = plistlib.loads(raw.encode())
    offenders = _env_offenders(path.name, data) + _value_offenders(path.name, raw)
    assert not offenders, "\n".join(offenders)


def test_no_installed_ivy_plist_carries_a_credential() -> None:
    """The installed file is what actually leaks. A clean template proves
    nothing if someone hand-edits ~/Library/LaunchAgents, which is precisely
    how the key got there in the first place."""
    if not INSTALLED.is_dir():
        pytest.skip("no ~/Library/LaunchAgents on this machine")
    offenders = []
    for path in sorted(INSTALLED.glob("com.ivy.*.plist")):
        raw = path.read_text(errors="ignore")
        try:
            data = plistlib.loads(raw.encode())
        except Exception:
            continue
        offenders += _env_offenders(path.name, data) + _value_offenders(path.name, raw)
    assert not offenders, "\n".join(offenders)


def test_the_guard_actually_catches_one() -> None:
    """A guard that cannot fail is decoration. This is the shape of the plist
    that leaked, and it has to trip both detectors."""
    leaky = {"EnvironmentVariables": {"PATH": "/usr/bin", "ODDS_API_KEY": "a" * 32}}
    raw = plistlib.dumps(leaky).decode()
    assert _env_offenders("x", leaky), "a secret-named env var must be caught"
    assert _value_offenders("x", raw), "a credential-shaped value must be caught"

    # Caught on the value alone, even under an innocuous name.
    disguised = {"EnvironmentVariables": {"PATH": "/usr/bin", "SETTING": "b" * 32}}
    assert _env_offenders("x", disguised), "a credential under a bland name must be caught"


def test_the_guard_does_not_fire_on_a_clean_plist() -> None:
    """Over-eager and nobody keeps it."""
    clean = {"EnvironmentVariables": {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": "/Users/lexi",
        "ENABLE_IMESSAGE_POLLER": "true",   # a real flag this guard first tripped on
        "PYTHONPATH": "/Users/lexi/openclaw-admin",
    }}
    raw = plistlib.dumps(clean).decode()
    assert not _env_offenders("x", clean)
    assert not _value_offenders("x", raw)
