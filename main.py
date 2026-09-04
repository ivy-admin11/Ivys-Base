"""
Ivy Local Admin API Gateway v2.2 — Voice Assistant Edition

Architecture:
- Phase 1: Critical fixes (duplicates, auth, f-string bugs)
- Phase 2: Config consolidation (tool schemas, timeouts, feature flags)
- Phase 3: Gemini SDK refactor (use google.generativeai official library)
- Phase 4: Prompt caching for 80-90% token cost reduction ✅ IMPLEMENTED
- Phase 5: Voice assistant with session management and cache optimization ✅ IMPLEMENTED

All hardcoded values are extracted to config.py for centralized tuning.
Environment-specific secrets go in .env (see .env.example).

Security:
- All FastAPI endpoints require X-API-Key header matching ADMIN_SECRET
- Database reads use SQLite read-only mode to prevent accidental mutations
- iMessage poller validates sender against favorites.json whitelist

Voice Assistant Features:
- Session-based conversation management with automatic cleanup
- Cache-optimized queries for 80-90% token savings on repeated interactions
- DeepSeek primary, with Gemini backup/failover for reliability
- Real-time cache statistics and session monitoring

Cost Optimization:
- Prompt caching enabled: 80-90% reduction on repeated input tokens
- Voice queries benefit from cached system instructions and context
- Expected monthly cost: $8-12 (down from $230+)
"""

import os
import socket
import sys
import time
import sqlite3
import threading
import logging
import json
import requests
import subprocess
import google.generativeai as genai
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import re as _re
from typing import List, Optional, Dict, Any, Callable, Tuple
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel

# Import centralized configuration
from config import (
    POLLING_INTERVAL,
    DB_TIMEOUT,
    DB_RETRY_ATTEMPTS,
    DB_RETRY_BACKOFF,
    CHAT_DB_PATH,
    EXTERNAL_API_TIMEOUT,
    ENABLE_IMESSAGE_POLLER,
    ENABLE_CALENDAR_INTEGRATION,
    ENABLE_REMINDERS_INTEGRATION,
    ENABLE_READWISE_INTEGRATION,
    PLAYWRIGHT_ENABLED,
    ADMIN_SECRET,
    GEMINI_SYSTEM_INSTRUCTION,
    DEEPSEEK_SYSTEM_INSTRUCTION_TEMPLATE,
    READWISE_API_ENDPOINT,
    READWISE_HIGHLIGHTS_LIMIT,
    READWISE_TOKEN_OPTIMIZATION_MAX_CHARS,
    STORE_CONFIG_PATH,
    STORE_CONFIG_FALLBACKS,
    LOG_LEVEL,
    LOG_FORMAT,
    ENABLE_PROMPT_CACHING,
    ENABLE_CACHE_METRICS_LOGGING,
)

# Canonical tool schema — single source of truth for both providers
from registry import GEMINI_TOOL_DECLARATIONS, DEEPSEEK_TOOL_SCHEMA
from ivy_core import receipts
from ivy_core import outbox as _outbox
from ivy_core import attachment_verify
from ivy_core.messaging import send_imessage_attachment
from ivy_core.report_fallback import split_imessage_content
from utils.applescript import AppleScriptRunner

# Import prompt caching manager
try:
    from cache_manager import cache_manager
    CACHING_AVAILABLE = True
except ImportError:
    CACHING_AVAILABLE = False
    logger_temp = logging.getLogger("ivy.gateway")
    logger_temp.warning("cache_manager not found; prompt caching disabled")

# Import voice assistant module
try:
    from voice_assistant import voice_session_manager, VoiceProcessor
    VOICE_ASSISTANT_AVAILABLE = True
    # Initialize with cache manager if available
    voice_processor = VoiceProcessor(cache_manager=cache_manager if CACHING_AVAILABLE else None)
except ImportError:
    VOICE_ASSISTANT_AVAILABLE = False
    voice_processor = None
    logger_temp = logging.getLogger("ivy.gateway")
    logger_temp.warning("voice_assistant not found; voice features disabled")

# Import job runner for ad-hoc job execution
try:
    from job_runner import job_runner, JobStatus
    JOB_RUNNER_AVAILABLE = True
except ImportError:
    JOB_RUNNER_AVAILABLE = False
    job_runner = None
    logger_temp = logging.getLogger("ivy.gateway")
    logger_temp.warning("job_runner not found; job execution disabled")

# ============================================================================
# LOGGING SETUP
# ============================================================================
# .env is loaded by config.py (imported above) before any settings are read.

logging.basicConfig(
    level=LOG_LEVEL,
    format=LOG_FORMAT,
)
logger = logging.getLogger("ivy.gateway")

# 🛡️ Guarded Playwright import (grocery staging removed)
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = PLAYWRIGHT_ENABLED
except ImportError:
    async_playwright = None
    PLAYWRIGHT_AVAILABLE = False
    logger.info("Playwright not available (optional — grocery staging removed)")

# ============================================================================
# GEMINI SDK CONFIGURATION
# ============================================================================

genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
gemini_model = genai.GenerativeModel("gemini-2.5-flash")

# ============================================================================
# PYDANTIC MODELS (Voice Assistant)
# ============================================================================

class VoiceQueryRequest(BaseModel):
    """Voice query request with optional session ID."""
    query: str
    user_id: str
    session_id: Optional[str] = None

class VoiceQueryResponse(BaseModel):
    """Voice query response with session and cache info."""
    session_id: str
    response: str
    cached_tokens: int = 0
    total_queries: int = 0
    cache_hit_rate: float = 0.0

class VoiceSessionResponse(BaseModel):
    """Voice session information."""
    session_id: str
    user_id: str
    state: str
    message_count: int
    cache_hit_rate: float

# ============================================================================
# TOOL REGISTRY (Powers /capabilities, /health, startup banner)
# ============================================================================

TOOLS_LIST = [
    {
        "name": "imessage_send",
        "description": "Sends an outbound iMessage via local AppleScript routing.",
        "required_env": [],
    },
    {
        "name": "check_apple_calendar",
        "description": "Scans the local Mac iCloud 'Hilla' Calendar for upcoming events.",
        "required_env": [],
        "enabled": ENABLE_CALENDAR_INTEGRATION,
    },
    {
        "name": "fetch_apple_reminders",
        "description": "Reads uncompleted tasks/groceries from a Mac Reminders list.",
        "required_env": [],
        "enabled": ENABLE_REMINDERS_INTEGRATION,
    },
    {
        "name": "add_apple_reminder",
        "description": "Adds a task or grocery entry into an Apple Reminders list.",
        "required_env": [],
        "enabled": ENABLE_REMINDERS_INTEGRATION,
    },
    {
        "name": "fetch_readwise_highlights",
        "description": "Retrieves saved articles and highlights from the Readwise API.",
        "required_env": [["READWISE_API_KEY"]],
        "enabled": ENABLE_READWISE_INTEGRATION,
    },
    {
        "name": "deepseek",
        "description": "Primary AI conversation/reasoning engine via the DeepSeek API.",
        "required_env": [["DEEPSEEK_API_KEY"]],
        "role": "primary",
    },
    {
        "name": "gemini",
        "description": "Failover/backup AI engine via Google Gemini (prompt caching enabled).",
        "required_env": [["GEMINI_API_KEY"]],
        "role": "failover",
    },
    {
        "name": "voice_assistant",
        "description": "Voice conversation with session management and cache-optimized queries.",
        "required_env": [],
        "enabled": True,
    },
    {
        "name": "get_capabilities",
        "description": "Lists every Ivy tool and whether it is configured/ready (no external calls).",
        "required_env": [],
    },
]


def _env_group_satisfied(group: List[str]) -> bool:
    """A group is satisfied if ANY env var in it is present and non-empty."""
    return any(os.environ.get(var, "").strip() for var in group)


def compute_tool_statuses() -> List[Dict[str, Any]]:
    """Compute tool readiness (fast, no external calls)."""
    statuses = []
    for tool in TOOLS_LIST:
        # Skip if feature flag is disabled
        if not tool.get("enabled", True):
            statuses.append({
                "tool_name": tool["name"],
                "description": tool["description"],
                "status": "disabled",
                "reason": "Feature flag disabled",
            })
            continue

        missing_groups = [
            g for g in tool.get("required_env", [])
            if not _env_group_satisfied(g)
        ]

        if missing_groups:
            reasons = ["Missing " + " or ".join(g) + " environment variable" for g in missing_groups]
            status, reason = "unavailable", "; ".join(reasons)
        else:
            status, reason = "ready", None

        statuses.append({
            "tool_name": tool["name"],
            "description": tool["description"],
            "status": status,
            "reason": reason,
        })
    return statuses


# ---------------------------------------------------------------------------
# Provider auth probes — distinguish "configured" from "authenticated"
# ---------------------------------------------------------------------------

_PROVIDER_PROBE_CACHE: Dict[str, Any] = {}
_PROVIDER_PROBE_LOCK = threading.Lock()
_PROVIDER_PROBE_TTL = 60  # seconds — avoid hammering APIs on every /health poll


def _probe_deepseek() -> Dict[str, Any]:
    """Make a minimal (~1-token) HTTP call to DeepSeek and map the result to
    {configured, authenticated, reachable, role, status, reason}."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return {
            "configured": False, "authenticated": False, "reachable": False,
            "role": "primary", "status": "unconfigured", "reason": "DEEPSEEK_API_KEY not set",
        }
    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
            timeout=8,
        )
        if resp.status_code == 200:
            return {
                "configured": True, "authenticated": True, "reachable": True,
                "role": "primary", "status": "ready", "reason": None,
            }
        if resp.status_code in (401, 403):
            return {
                "configured": True, "authenticated": False, "reachable": True,
                "role": "primary", "status": "degraded",
                "reason": f"Provider returned HTTP {resp.status_code}",
            }
        return {
            "configured": True, "authenticated": False, "reachable": True,
            "role": "primary", "status": "error",
            "reason": f"Unexpected HTTP {resp.status_code}",
        }
    except requests.exceptions.Timeout:
        return {
            "configured": True, "authenticated": False, "reachable": False,
            "role": "primary", "status": "unreachable", "reason": "Request timed out",
        }
    except requests.exceptions.ConnectionError as exc:
        return {
            "configured": True, "authenticated": False, "reachable": False,
            "role": "primary", "status": "unreachable", "reason": str(exc)[:120],
        }
    except Exception as exc:
        return {
            "configured": True, "authenticated": False, "reachable": True,
            "role": "primary", "status": "error", "reason": str(exc)[:120],
        }


def _probe_gemini() -> Dict[str, Any]:
    """Make a minimal (~1-token) call to Gemini and map the result."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return {
            "configured": False, "authenticated": False, "reachable": False,
            "role": "failover", "status": "unconfigured", "reason": "GEMINI_API_KEY not set",
        }
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")
        model.generate_content("hi", generation_config={"max_output_tokens": 1})
        return {
            "configured": True, "authenticated": True, "reachable": True,
            "role": "failover", "status": "ready", "reason": None,
        }
    except Exception as exc:
        msg = str(exc)
        if any(code in msg for code in ("401", "403", "API_KEY_INVALID", "PERMISSION_DENIED")):
            return {
                "configured": True, "authenticated": False, "reachable": True,
                "role": "failover", "status": "degraded",
                "reason": "Provider returned auth error",
            }
        return {
            "configured": True, "authenticated": False, "reachable": True,
            "role": "failover", "status": "error", "reason": msg[:120],
        }


def _provider_cache_snapshot() -> Tuple[Dict[str, Any], float]:
    with _PROVIDER_PROBE_LOCK:
        cached_at = _PROVIDER_PROBE_CACHE.get("_ts", 0.0)
        return {k: v for k, v in _PROVIDER_PROBE_CACHE.items() if k != "_ts"}, cached_at


def probe_providers(*, force: bool = False) -> Dict[str, Any]:
    """Return per-provider auth status, making live (~1-token) calls when the
    cache is older than _PROVIDER_PROBE_TTL seconds. BLOCKS for up to ~16 s
    on the network — never call this from /health or /ready; they use
    cached_provider_status() and refresh in the background.

    Pass force=True to bypass the cache (e.g., after a key rotation)."""
    snapshot, cached_at = _provider_cache_snapshot()
    if not force and snapshot and (time.monotonic() - cached_at) < _PROVIDER_PROBE_TTL:
        return snapshot

    # Network calls happen OUTSIDE the lock so a slow provider can never
    # stall a reader that only wants the cached value.
    result: Dict[str, Any] = {
        "deepseek": _probe_deepseek(),
        "gemini": _probe_gemini(),
    }
    with _PROVIDER_PROBE_LOCK:
        _PROVIDER_PROBE_CACHE.clear()
        _PROVIDER_PROBE_CACHE.update(result)
        _PROVIDER_PROBE_CACHE["_ts"] = time.monotonic()
    return result


_PROVIDER_PROBE_THREAD: Optional[threading.Thread] = None


def _refresh_provider_probe_async() -> Optional[threading.Thread]:
    """Start a background probe unless one is already running."""
    global _PROVIDER_PROBE_THREAD
    if _PROVIDER_PROBE_THREAD is not None and _PROVIDER_PROBE_THREAD.is_alive():
        return _PROVIDER_PROBE_THREAD
    thread = threading.Thread(
        target=probe_providers, kwargs={"force": True},
        daemon=True, name="provider-probe",
    )
    _PROVIDER_PROBE_THREAD = thread
    thread.start()
    return thread


def _pending_provider_status() -> Dict[str, Any]:
    out = {}
    for name, env_var, role in (("deepseek", "DEEPSEEK_API_KEY", "primary"),
                                ("gemini", "GEMINI_API_KEY", "failover")):
        configured = bool(os.environ.get(env_var, "").strip())
        out[name] = {
            "configured": configured, "authenticated": False, "reachable": False,
            "role": role, "status": "pending" if configured else "unconfigured",
            "reason": "auth probe not finished yet" if configured else f"{env_var} not set",
        }
    return out


def cached_provider_status(*, wait_for_first: float = 0.0) -> Dict[str, Any]:
    """Provider status WITHOUT touching the network on the caller's thread.

    Returns the last probe result, kicking off a background refresh when it
    is stale. Before the very first probe completes it returns a "pending"
    placeholder — optionally waiting up to ``wait_for_first`` seconds for the
    in-flight probe so /ready is accurate seconds after startup.

    History: /health used to call probe_providers() inline. Every 60 s the
    cache expired and the next /health blocked on two live LLM calls (8 s
    timeout each); the monitor's 5 s request timeout expired three times in
    a row and texted Henry "gateway DOWN" while the process was fine.
    """
    snapshot, cached_at = _provider_cache_snapshot()
    if not snapshot or (time.monotonic() - cached_at) >= _PROVIDER_PROBE_TTL:
        thread = _refresh_provider_probe_async()
        if not snapshot and wait_for_first > 0 and thread is not None:
            thread.join(timeout=wait_for_first)
            snapshot, _ = _provider_cache_snapshot()
    return snapshot or _pending_provider_status()



# The interpreter the gateway's Full Disk Access / Automation grants were
# issued against. Empty string disables the check (e.g. on another machine).
TCC_GRANTED_INTERPRETER = os.environ.get(
    "TCC_GRANTED_INTERPRETER",
    "/Users/lexi/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12",
)


def _interpreter_matches_tcc_grant() -> bool:
    """True when the running interpreter is still the TCC-granted binary."""
    if not TCC_GRANTED_INTERPRETER:
        return True
    running = os.path.realpath(getattr(sys, "_base_executable", sys.executable))
    return running == os.path.realpath(TCC_GRANTED_INTERPRETER)


def print_startup_banner() -> None:
    """Colorful ANSI banner of every tool's health."""
    GREEN, RED, YELLOW, BOLD, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[0m"
    lines = [f"{BOLD}🚀 Ivy Gateway v2.2 — Voice Assistant Edition{RESET}"]

    # Add feature statuses
    if ENABLE_PROMPT_CACHING and CACHING_AVAILABLE:
        lines.append(f"{GREEN}💾 Prompt Caching:     ENABLED (80-90% token savings){RESET}")
    else:
        lines.append(f"{YELLOW}⊘ Prompt Caching:     DISABLED{RESET}")

    if VOICE_ASSISTANT_AVAILABLE:
        lines.append(f"{GREEN}🎙️  Voice Assistant:    ENABLED (session-based, cache-optimized){RESET}")
    else:
        lines.append(f"{YELLOW}⊘ Voice Assistant:    DISABLED{RESET}")

    for s in compute_tool_statuses():
        if s["status"] == "ready":
            lines.append(f"{GREEN}✅ {s['tool_name']:<22} Ready{RESET}")
        elif s["status"] == "disabled":
            lines.append(f"{YELLOW}⊘ {s['tool_name']:<22} Disabled (feature flag){RESET}")
        else:
            lines.append(f"{RED}❌ {s['tool_name']:<22} {s['reason']}{RESET}")
    print("\n".join(lines), flush=True)


# ============================================================================
# LIFESPAN: Startup & Shutdown
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Print banner, start iMessage poller, initialize voice assistant."""
    print_startup_banner()

    # Initialize voice session manager if available
    if VOICE_ASSISTANT_AVAILABLE:
        logger.info("Voice session manager initialized and ready.")

    # Warm the provider auth cache off the request path so /health and
    # /ready never wait on the network.
    _refresh_provider_probe_async()

    # Start iMessage poller if enabled
    if ENABLE_IMESSAGE_POLLER:
        worker_thread = threading.Thread(target=background_imessage_worker, daemon=True)
        worker_thread.start()
        logger.info("Background iMessage polling thread started.")

    try:
        yield
    finally:
        logger.info("Gateway shutdown complete.")


app = FastAPI(title="Ivy Local Admin API Gateway v2.2 — Voice Assistant", lifespan=lifespan)

PROCESS_STARTED_AT = datetime.now()
PROJECT_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================================
# SECURITY: Authentication Middleware
# ============================================================================


def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    """Verify the X-API-Key header against ADMIN_SECRET."""
    if not x_api_key or x_api_key != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")
    return True


# ============================================================================
# HELPER FUNCTIONS: Token Optimization
# ============================================================================


def optimize_token_payload(raw_text: str, max_chars: int = 3500) -> str:
    """Enforce token constraints by chunking and sizing text payload."""
    if not raw_text:
        return "Empty payload dataset."
    lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
    cleaned_text = "\n".join(lines)
    if len(cleaned_text) <= max_chars:
        return cleaned_text
    logger.warning(
        "Payload size alert (%d chars). Token optimization chunking engaged.",
        len(cleaned_text),
    )
    return (
        cleaned_text[:max_chars]
        + "\n\n[...System Truncation applied for strict Token optimization...]"
    )


# ============================================================================
# READWISE INTEGRATION
# ============================================================================


def fetch_readwise_highlights() -> str:
    """Fetch saved articles and highlights from Readwise API."""
    active_token = os.environ.get("READWISE_API_KEY", "")
    if not active_token:
        return "❌ Readwise pipeline offline: READWISE_API_KEY missing from environment."

    headers = {"Authorization": f"Token {active_token}"}

    try:
        response = requests.get(
            READWISE_API_ENDPOINT,
            headers=headers,
            timeout=EXTERNAL_API_TIMEOUT,
        )
        if response.status_code != 200:
            return (
                f"❌ Readwise API connection issue. Status Code: {response.status_code}"
            )

        data = response.json()
        results = data.get("results", [])
        if not results:
            return "Your Readwise repository is currently clear of saved elements."

        compiled_items = []
        for item in results[: READWISE_HIGHLIGHTS_LIMIT]:
            text = item.get("text", "")
            note = item.get("note", "")
            title = item.get("title", "Saved Article")
            block = f"- From '{title}': \"{text}\""
            if note:
                block += f" (Note: {note})"
            compiled_items.append(block)

        raw_output = "\n".join(compiled_items)
        return optimize_token_payload(raw_output, max_chars=READWISE_TOKEN_OPTIMIZATION_MAX_CHARS)
    except Exception as e:
        return f"❌ Readwise Integration Pipeline Error: {str(e)}"


# ============================================================================
# APPLESCRIPT RUNNER (shared by every osascript caller below)
# ============================================================================
#
# Every AppleScript invocation in this module goes through this runner so that
# untrusted content is passed as process argv rather than interpolated into
# script source, and so that no call can block forever — these run on the
# poller thread, and a bare subprocess.run() with no timeout wedges it.

_GATEWAY_APPLESCRIPT = AppleScriptRunner()


# ============================================================================
# APPLE CALENDAR INTEGRATION
# ============================================================================


def check_apple_calendar(timeframe: str) -> str:
    """Scan local Mac Hilla Calendar for upcoming events."""
    script_lines = [
        "set totalEvents to \"\"",
        "set midnightToday to (current date)",
        "set hours of midnightToday to 0",
        "set minutes of midnightToday to 0",
        "set seconds of midnightToday to 0",
        "tell application \"Calendar\"",
        "    try",
        "        set familyCal to calendar \"Hilla\"",
        "        set upcomingEvents to (every event of familyCal whose start date is greater than or equal to midnightToday)",
        "        repeat with e in upcomingEvents",
        "            set d to start date of e",
        "            set totalEvents to totalEvents & (summary of e) & \":::\" & (day of d as text) & \" \" & (month of d as text) & \" \" & (year of d as text) & \" at \" & (time string of d) & \"\\n\"",
        "        end repeat",
        "    on error err",
        "        return \"Error: \" & err",
        "    end try",
        "end tell",
        "return totalEvents",
    ]
    script = "\n".join(script_lines)
    raw_output = _GATEWAY_APPLESCRIPT.run(script)

    # The script's own failure path returns "Error: ..."; the runner returns
    # "ERROR: ..." for a timeout or a non-zero osascript exit. Catch both.
    if raw_output.startswith("ERROR:") or "Error:" in raw_output:
        return f"❌ AppleScript Database Error: {raw_output}"
    if not raw_output:
        return "Your Hilla calendar has no upcoming events listed."

    now = datetime.now()
    months_map = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
    }

    parsed_events = []
    for line in raw_output.split("\n"):
        if ":::" not in line:
            continue
        summary, date_string = line.split(":::", 1)
        parts = date_string.split()
        if len(parts) >= 3:
            try:
                ev_day = int(parts[0])
                ev_month = months_map.get(parts[1].lower(), 1)
                ev_year = int(parts[2])
                ev_time = " ".join(parts[4:]) if "at" in parts else parts[3]

                event_dt = datetime(ev_year, ev_month, ev_day)
                parsed_events.append({
                    "date": event_dt,
                    "display": f"- {event_dt.strftime('%A, %b %d')}: {summary} ({ev_time})"
                })
            except Exception:
                continue

    parsed_events.sort(key=lambda x: x["date"])

    if timeframe.lower() in ["all", "full", "everything"]:
        formatted_list = "Complete Upcoming Schedule:\n" + "\n".join(
            [e["display"] for e in parsed_events]
        )
        return optimize_token_payload(formatted_list, max_chars=3500)

    target = now + timedelta(days=1) if timeframe.lower() == "tomorrow" else now
    day_matches = [
        e["display"] for e in parsed_events if e["date"].date() == target.date()
    ]

    if day_matches:
        return f"Schedule for {timeframe}:\n" + "\n".join(day_matches)

    next_up = parsed_events[0]["display"] if parsed_events else "None listed"
    return f"Your Hilla calendar is clear for {timeframe}. Next upcoming agenda item:\n{next_up}"


# ============================================================================
# APPLE REMINDERS INTEGRATION
# ============================================================================


# The only Reminders lists Ivy is allowed to touch. Both LLM system prompts and
# registry.py tell the model "must strictly be 'Household'", but that is prose
# aimed at a model, not a check: the Gemini paths clamped list_name while the
# DeepSeek path — the primary brain — passed the model's raw value straight
# through, so an inbound text could name any list and the add script would
# create it on demand. Enforced here so every provider path shares one rule.
_ALLOWED_REMINDER_LISTS = ("Household", "Meal Plan")


def _clamp_reminder_list(list_name: str) -> str:
    return list_name if list_name in _ALLOWED_REMINDER_LISTS else "Household"


def fetch_apple_reminders(list_name: str = "Household") -> str:
    """Read uncompleted tasks from Apple Reminders.

    ``list_name`` is passed as a process argument, never interpolated into
    AppleScript source (see utils.applescript).
    """
    list_name = _clamp_reminder_list(list_name)
    result = _GATEWAY_APPLESCRIPT.fetch_reminders_argv(list_name)

    # An empty read and a failed read used to be indistinguishable: the old
    # code answered "No active reminders found." whenever stdout was empty,
    # including when osascript had errored outright.
    if result.startswith("ERROR:"):
        logger.error("Reminders read failed for list %r: %s", list_name, result[:200])
        return f"❌ Couldn't read your '{list_name}' list: {result[len('ERROR:'):].strip()}"
    return result or "No active reminders found."


def add_apple_reminder(title: str, list_name: str = "Household") -> str:
    """Add a task to Apple Reminders."""
    # Auto-categorize based on keywords
    if any(word in list_name.lower() for word in ["meal", "food", "dinner", "recipe", "taco"]):
        list_name = "Meal Plan"
    elif any(word in list_name.lower() for word in ["house", "chore", "clean", "task"]):
        list_name = "Household"

    # After the keyword auto-categorisation above, so "meal"/"chore" routing
    # still works — but an arbitrary or dash-leading name cannot get through.
    list_name = _clamp_reminder_list(list_name)
    result = _GATEWAY_APPLESCRIPT.add_reminder_argv(list_name, title)

    # Exact match, not a substring test: the old `"SUCCESS" in raw_output`
    # would also fire on an error message that happened to quote a title
    # containing the word.
    if result == "SUCCESS":
        return f"✅ Added to your '{list_name}' list: {title}"
    logger.error("Reminder add failed for list %r: %s", list_name, result[:200])
    return f"❌ Reminders Integration Error: {result}"


def run_job(job_name: str) -> str:
    """Execute a background job by name (sharp_picks, happy_hour, meals, etc.).

    Every call here is an explicit, on-demand request (via iMessage, voice,
    the CLI, or /run-job) — never the scheduled invocation, which launchd
    triggers directly. force=True so an ad-hoc "run picks now" always
    delivers even if the underlying agent has its own duplicate-suppression
    gate (sharp_picks) or 48h cadence (familia_meal_planner).
    """
    if not JOB_RUNNER_AVAILABLE:
        return "❌ Job execution system unavailable."

    status, message = job_runner.run_job(job_name, force=True)

    if status == JobStatus.SUCCESS:
        return message
    elif status == JobStatus.ALREADY_RUNNING:
        return f"⏳ {message}"
    elif status == JobStatus.NOT_FOUND:
        return f"❓ {message}"
    elif status == JobStatus.UNAVAILABLE:
        return f"🚫 {message}"
    else:
        return f"❌ {message}"


# ============================================================================
# TOOL DISPATCH (single registry — replaces per-provider globals()/if-elif dispatch)
# ============================================================================

TOOL_HANDLERS: Dict[str, Callable[..., str]] = {
    "check_apple_calendar": check_apple_calendar,
    "fetch_readwise_highlights": fetch_readwise_highlights,
    "fetch_apple_reminders": fetch_apple_reminders,
    "add_apple_reminder": add_apple_reminder,
    "run_job": run_job,
}


# A message that points BACKWARD at something Ivy already sent. Deliberately
# separate from the report-command matcher: this one is the last line of
# defence for phrasings the matcher misses, and it only ever suppresses a job.
_BACKWARD_REFERENCE_RE = _re.compile(
    r"\b(you\s+sent|sent\s+me|you\s+just\s+sent|earlier|last\s+one|last\s+report|"
    r"that\s+report|those\s+picks|the\s+other\s+picks|from\s+(?:this\s+)?(?:morning|afternoon))\b",
    _re.IGNORECASE,
)

# ... unless the message also asks for a NEW run.
_RUN_INTENT_RE = _re.compile(
    r"\b(run|rerun|re-run|start|refresh|new|again|latest|update)\b", _re.IGNORECASE
)


def _rerun_would_be_wrong(tool_name: str, tool_args: Dict[str, Any], inbound_text: str) -> Optional[str]:
    """Return a reply instead of running a job, when the message was asking
    about a report Ivy already sent.

    On 2026-09-03 "More of the 3pm picks you sent me" made DeepSeek call
    run_job(sharp_picks): a full X sweep, live-odds spend, and a second report
    that answered a question about the first one. The matcher catches that
    exact phrasing now; this catches the ones it doesn't.
    """
    if tool_name != "run_job" or not inbound_text:
        return None
    if not _BACKWARD_REFERENCE_RE.search(inbound_text):
        return None
    if _RUN_INTENT_RE.search(inbound_text):
        return None  # they really do want a fresh run

    job_name = _RESEND_ALIASES.get(
        str(tool_args.get("job_name", "")).strip().lower()
    ) or str(tool_args.get("job_name", "")).strip()
    reports = _outbox.list_reports(job_name if job_name in _RESEND_REPORT_NAMES else None)
    if not reports:
        return None

    report_id = reports[0]["report_id"]
    name = _RESEND_REPORT_NAMES.get(reports[0].get("job_name", ""), "report")
    logger.info(
        "Suppressed run_job(%s): message refers back to %s", job_name or "?", report_id
    )
    return (
        f"That was my {name} from earlier ({report_id}) — I won't re-run it. "
        f"Reply MORE for the rest of it, WHY <n> for the reasoning on one, or "
        f"PDF for the file. Say \"run picks\" if you do want a fresh sweep."
    )


def _execute_tool_call(
    tool_name: str,
    tool_args: Dict[str, Any],
    inbound_text: str = "",
) -> str:
    """Execute a registered tool by name. Both the Gemini and DeepSeek paths
    call through here, so neither can dispatch to anything but a real,
    registered tool, and DeepSeek gets the same run_job access Gemini has.

    ``inbound_text`` is the message that produced the tool call; it is used
    only to refuse a job re-run that is really a question about the last one.
    """
    guard = _rerun_would_be_wrong(tool_name, tool_args, inbound_text)
    if guard is not None:
        return guard

    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Error: Function {tool_name} is undefined."
    try:
        return handler(**tool_args)
    except Exception as exec_err:
        return f"Error: {exec_err}"


# ============================================================================
# IMESSAGE ROUTING
# ============================================================================


def run_local_applescript_send(target: str, body: str) -> str:
    """Send an iMessage reply. Returns "SUCCESS" or an "ERROR: ..." string.

    Routed through the argv-based runner: the body is passed as a process
    argument, never interpolated into AppleScript source. The old f-string
    version broke on any reply containing a double quote (a recipe calling
    for "00" flour never reached Henry on 2026-08-28) and had no timeout, so
    a hung Messages.app could wedge the poller thread indefinitely.
    """
    result = _GATEWAY_APPLESCRIPT.send_imessage_argv(target, body)
    if result != "SUCCESS":
        logger.error("iMessage reply to %s was NOT sent: %s", target, result[:200])
    return result


# ============================================================================
# DEEPSEEK FAILOVER ENGINE
# ============================================================================


def execute_deepseek_call(
    text_content: str,
    system_instruction: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Execute call via DeepSeek API with tool calling support.

    ``history`` is the sender's recent turns (see conversation_history) so a
    follow-up like "yes, the full recipe" is read against what Ivy just
    offered instead of as a standalone command.
    """
    active_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not active_key:
        logger.warning("DeepSeek call attempted with no DEEPSEEK_API_KEY configured.")
        return (
            "DeepSeek is not configured. Please set the DEEPSEEK_API_KEY environment variable."
        )

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {active_key}", "Content-Type": "application/json"}

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_instruction}]
    for turn in history or []:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": text_content})

    payload = {
        "model": "deepseek-v4-flash",
        "messages": messages,
        "tools": DEEPSEEK_TOOL_SCHEMA,
        "temperature": 0.1,
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=EXTERNAL_API_TIMEOUT)
        if response.status_code != 200:
            return f"❌ DeepSeek Engine Communication Fault. Status: {response.status_code}"

        res_data = response.json()
        message_node = res_data["choices"][0]["message"]

        # Check if DeepSeek triggered tool execution — dispatched through the
        # same TOOL_HANDLERS registry Gemini uses, so DeepSeek can execute
        # every registered tool (including run_job, which it previously
        # could request via its schema but never actually got dispatched).
        if "tool_calls" in message_node and message_node["tool_calls"]:
            call = message_node["tool_calls"][0]
            func_name = call["function"]["name"]
            args = (
                json.loads(call["function"].get("arguments", "{}"))
                if call["function"].get("arguments")
                else {}
            )

            logger.info(
                "DeepSeek Core triggered native tool: %s with arguments: %s",
                func_name,
                args,
            )

            return _execute_tool_call(func_name, args, inbound_text=text_content)

        return message_node.get("content", "").strip()
    except Exception as e:
        return f"❌ DeepSeek Execution Layer Exception: {str(e)}"


# ============================================================================
# GEMINI BACKUP ENGINE (only reached when DeepSeek is unavailable/empty)
# ============================================================================


def _gemini_backup_reply(text: str, history: Optional[List[Dict[str, str]]] = None) -> Optional[str]:
    """Gemini backup: prompt-cached generate_content call with real tool
    execution and a real follow-up round-trip. Raises on provider failure
    (caller treats that as "no backup available" and gives up); returns None
    if Gemini responded but had nothing usable to say.

    Recent conversation turns are folded into the user message as plain text
    (the cached-request helper only takes a single user string).
    """
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise ValueError("GEMINI_API_KEY not configured in environment")

    # Keep the raw message: `text` is about to absorb the conversation history,
    # and the re-run guard must judge what was said NOW, not what was said
    # three turns ago.
    inbound_text = text

    if history:
        text = format_history_for_prompt(history) + "\n\nCurrent message: " + text

    # ✅ USE CACHED PROMPTS IF ENABLED
    use_caching = ENABLE_PROMPT_CACHING and CACHING_AVAILABLE
    if use_caching:
        messages = cache_manager.create_cached_gemini_request(
            user_message=text,
            system_instruction=GEMINI_SYSTEM_INSTRUCTION,
            tool_declarations=GEMINI_TOOL_DECLARATIONS,
        )
        if messages is None:
            logger.warning("Caching failed, falling back to non-cached request")
            messages = [genai.types.ContentDict(role="user", parts=[genai.types.PartDict(text=text)])]
            use_caching = False
    else:
        messages = [genai.types.ContentDict(role="user", parts=[genai.types.PartDict(text=text)])]

    # ⚠️ IMPORTANT: When using cached messages, don't pass system_instruction again
    # The cache_manager already includes it in the message stream
    if use_caching:
        response = gemini_model.generate_content(
            messages,
            tools=[genai.types.Tool(function_declarations=GEMINI_TOOL_DECLARATIONS)],
        )
    else:
        response = gemini_model.generate_content(
            messages,
            tools=[genai.types.Tool(function_declarations=GEMINI_TOOL_DECLARATIONS)],
            system_instruction=GEMINI_SYSTEM_INSTRUCTION,
        )

    # 💾 LOG CACHE METRICS
    if ENABLE_CACHE_METRICS_LOGGING and CACHING_AVAILABLE:
        cache_manager.log_cache_efficiency(
            response, endpoint="background_imessage_worker", model="gemini-2.5-flash"
        )

    if not (response.candidates and response.candidates[0].content):
        return None

    parts = response.candidates[0].content.parts
    text_reply = ""
    tool_calls = []
    for part in parts:
        if hasattr(part, "text") and part.text:
            text_reply += part.text
        # part.function_call is always a present attribute (protobuf oneof
        # field) even on text-only parts — checking truthiness, not hasattr,
        # is what actually detects a real tool call.
        if getattr(part, "function_call", None):
            tool_calls.append(part.function_call)

    if not tool_calls:
        return text_reply.strip() or None

    logger.info("🛠️ Gemini returned %d tool operations", len(tool_calls))
    tool_results = []
    for call in tool_calls:
        tool_name = call.name
        tool_args = call.args
        # Enforce Household list for reminders
        if tool_name in ["add_apple_reminder", "fetch_apple_reminders"]:
            tool_args["list_name"] = "Household"
        logger.info("🛠️ Executing Tool: %s with arguments %s", tool_name, tool_args)
        tool_result = _execute_tool_call(tool_name, tool_args, inbound_text=inbound_text)
        logger.info("📤 Tool Output: %s", tool_result)
        tool_results.append((tool_name, tool_result))

    # Follow-up call with the *real* tool results (previously always sent
    # back an empty {} regardless of what the tool actually returned).
    follow_up_kwargs = {"tools": [genai.types.Tool(function_declarations=GEMINI_TOOL_DECLARATIONS)]}
    if not use_caching:
        follow_up_kwargs["system_instruction"] = GEMINI_SYSTEM_INSTRUCTION

    follow_up_response = gemini_model.generate_content(
        [
            *messages,
            {"role": "model", "parts": parts},
            {
                "role": "function",
                "parts": [
                    {"function_response": {"name": name, "response": {"result": result}}}
                    for name, result in tool_results
                ],
            },
        ],
        **follow_up_kwargs,
    )
    if follow_up_response.candidates:
        follow_up_parts = follow_up_response.candidates[0].content.parts
        return "".join(p.text for p in follow_up_parts if hasattr(p, "text")).strip() or None
    return None


def query_llm_with_tools(prompt_text: str) -> str:
    """One-shot DeepSeek-primary/Gemini-backup query with real tool execution.

    Used by the `ivy` CLI's query mode. Unlike the iMessage poller and
    /voice/query, this has no session state — just a single question, a
    single dual-brain answer.
    """
    reply = None
    try:
        reply = execute_deepseek_call(
            prompt_text,
            DEEPSEEK_SYSTEM_INSTRUCTION_TEMPLATE.format(
                current_date_str=datetime.now().strftime("%A, %B %d, %Y")
            ),
        )
    except Exception as exc:
        logger.error("CLI query: DeepSeek primary layer fault: %s", exc)
        reply = None

    if not reply:
        try:
            reply = _gemini_backup_reply(prompt_text)
        except Exception as exc:
            logger.error("CLI query: Gemini backup layer fault: %s", exc)
            reply = None

    return reply or "No response."


# ============================================================================
# DATABASE OPERATIONS: Safe SQLite Read-Only Access
# ============================================================================


def safe_fetch_last_message(last_id: int) -> Optional[tuple]:
    """Fetch next message from chat.db with retry logic and read-only mode.

    Raises the last sqlite3 error if every attempt fails."""
    last_error: Exception = sqlite3.OperationalError("chat.db read failed")
    for attempt in range(DB_RETRY_ATTEMPTS):
        try:
            # Use read-only mode to prevent accidental mutations
            conn = sqlite3.connect(
                f"file:{CHAT_DB_PATH}?mode=ro&uri=true",
                uri=True,
                timeout=DB_TIMEOUT,
            )
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT m.ROWID, m.text, COALESCE(h.id, 'Me')
                FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.ROWID > ? AND m.is_from_me = 0 AND m.text IS NOT NULL
                ORDER BY m.ROWID ASC LIMIT 1
                """,
                (last_id,),
            )
            row = cursor.fetchone()
            conn.close()
            return row
        except sqlite3.OperationalError as e:
            last_error = e
            backoff = DB_RETRY_BACKOFF * (2 ** attempt)
            logger.warning(
                "Database read attempt %d failed: %s. Retrying in %.1f seconds...",
                attempt + 1,
                e,
                backoff,
            )
            time.sleep(backoff)
    # Returning None here would be indistinguishable from "no new message"
    # and would let the poller mark a failed read as a healthy cycle.
    raise last_error


def get_last_message_id() -> Optional[int]:
    """Get the highest ROWID from the message table."""
    try:
        conn = sqlite3.connect(
            f"file:{CHAT_DB_PATH}?mode=ro&uri=true",
            uri=True,
            timeout=DB_TIMEOUT,
        )
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(ROWID) FROM message")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] else 0
    except Exception:
        return None


# ============================================================================
# STORE CONFIG LOADING
# ============================================================================


def load_store_configs() -> Dict[str, Dict[str, str]]:
    """Load store selectors from store_configs.json, with fallbacks."""
    if os.path.exists(STORE_CONFIG_PATH):
        try:
            with open(STORE_CONFIG_PATH, "r") as f:
                data = json.load(f)
            merged = {}
            for store, fallback in STORE_CONFIG_FALLBACKS.items():
                cfg = dict(fallback)
                cfg.update(data.get(store, {}))
                merged[store] = cfg
            # Allow stores defined only in the file
            for store, cfg in data.items():
                if store not in merged:
                    merged[store] = cfg
            return merged
        except Exception as cfg_err:
            logger.warning(
                "Failed to parse store_configs.json (%s) — using hardcoded fallbacks.",
                cfg_err,
            )
    return {k: dict(v) for k, v in STORE_CONFIG_FALLBACKS.items()}


# ============================================================================
# RESEND COMMAND HANDLER
# ============================================================================


_REPORT_ID_RE = r"[A-Za-z]{2}-\d{8}-\d{4}(?::\d{2})?"

# Command verbs, matched at the START of the message only.
#
# These are deliberately loose about what FOLLOWS the verb, because real
# replies are not typed like commands. On 2026-09-03 Henry replied "More",
# got a "what would you like more of?" from DeepSeek, then wrote "More of the
# 3pm picks you sent me" — which DeepSeek answered by re-running the entire
# sweep and texting a brand-new report. Both should have been served from the
# outbox without a model call.
_MORE_VERB = _re.compile(
    r"^\s*(?:more|the\s+rest|rest|others?|what\s+else|show\s+me\s+(?:the\s+)?(?:rest|more|others?))\b",
    _re.IGNORECASE,
)
_WHY_VERB = _re.compile(r"^\s*why\s+(?:is\s+|on\s+)?#?(\d{1,2})\b", _re.IGNORECASE)
_PDF_VERB = _re.compile(
    r"^\s*(?:resend|pdf|send\s+(?:me\s+)?(?:the\s+)?pdf|the\s+pdf)\b",
    _re.IGNORECASE,
)

# What the tail of a loose command may refer to before we treat it as a report
# command rather than conversation. "more picks" / "more of the 3pm ones you
# sent" qualify; "more info about tomorrow" does not.
_REPORT_NOUN = _re.compile(
    r"\b(pick|picks|report|list|play|plays|board|special|specials|meal|meals|"
    r"recipe|recipes|spot|spots|happy\s*hour|them|those|these|ones?|it|that|"
    r"you\s+sent|sent\s+me|earlier|last\s+one)\b",
    _re.IGNORECASE,
)

# "3pm", "3 pm", "9:00 AM" — a clock reference identifying WHICH report.
_CLOCK_RE = _re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", _re.IGNORECASE)
# Coarser references, mapped to the hour a report would carry.
_PERIOD_HOURS = {
    "this morning": 9, "morning": 9,
    "this afternoon": 15, "afternoon": 15,
    "this evening": 20, "evening": 20, "tonight": 20,
    "last night": 21, "yesterday": 21,
}

# Politeness that carries no meaning; stripped before deciding whether a tail
# refers to a report, so "pdf please" reads the same as "pdf".
_FILLER_RE = _re.compile(
    r"\b(please|pls|plz|thanks|thank\s+you|thx|now|again|for\s+me)\b", _re.IGNORECASE
)

# Words that make a message a REQUEST rather than a reference to something
# already sent. "more chicken in the meal plan next week" is a preference for a
# future run, not a command to re-read the last one.
_FUTURE_INTENT_RE = _re.compile(
    r"\b(next\s+(week|time|run|month)|tomorrow|future|from\s+now\s+on|going\s+forward)\b",
    _re.IGNORECASE,
)

# A loose command has to stay short — a long message is conversation.
# "More of the 3pm picks you sent me" is 8 words, and that is the ceiling.
_LOOSE_MAX_WORDS = 8

# How far a clock reference may sit from a report's own timestamp, in hours.
_CLOCK_TOLERANCE_H = 2.0

_RESEND_ALIASES: Dict[str, str] = {
    "picks": "sharp_picks",
    "sharp picks": "sharp_picks",
    "happy hour": "happy_hour",
    "meal plan": "familia_meal_planner",
}

_RESEND_COMMANDS: Dict[str, str] = {
    "sharp_picks": "RESEND PICKS",
    "happy_hour": "RESEND HAPPY HOUR",
    "familia_meal_planner": "RESEND MEAL PLAN",
}

_RESEND_REPORT_NAMES: Dict[str, str] = {
    "sharp_picks": "Sharp Picks",
    "happy_hour": "Happy Hour Scout",
    "familia_meal_planner": "Familia Meal Plan",
    "bravo_scout": "Bravo Scout",
}

_HELP_HINT = "Try MORE, WHY 2, or PDF."


def _target_hour(tail: str) -> Optional[int]:
    """Hour (0-23) a message points at, or None.

    Report IDs already carry local HHMM, so "the 3pm picks" resolves by
    comparing against that rather than re-deriving timestamps.
    """
    m = _CLOCK_RE.search(tail)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3).lower() == "p":
            hour += 12
        return hour
    low = tail.lower()
    for phrase, hour in _PERIOD_HOURS.items():
        if phrase in low:
            return hour
    return None


def _job_from_tail(tail: str) -> Optional[str]:
    """Job named in a command tail, or None."""
    low = tail.lower()
    for alias, job in _RESEND_ALIASES.items():
        if alias in low:
            return job
    return None


def _resolve_report(tail: Optional[str]) -> tuple:
    """Map a command's tail to ``(report_id, job_name, error)``.

    Resolution order: an explicit report ID, then a named job, then a clock
    reference ("the 3pm picks"), then simply the most recent report Ivy sent —
    so a bare "MORE" right after a picks text means those picks.
    """
    tail = (tail or "").strip()

    m = _re.search(_REPORT_ID_RE, tail, _re.IGNORECASE)
    if m:
        report_id = m.group(0).upper()
        job_name = _outbox.job_name_for_report_id(report_id)
        if not job_name:
            return None, None, f"I don't recognise that report ID. {_HELP_HINT}"
        return report_id, job_name, None

    job_name = _job_from_tail(tail)
    hour = _target_hour(tail)

    candidates = _outbox.list_reports(job_name)
    if not candidates:
        if job_name:
            name = _RESEND_REPORT_NAMES.get(job_name, job_name)
            return None, None, f"I don't have a recent {name} report to work from."
        return None, None, "I haven't sent a report recently — nothing to pull up."

    if hour is not None:
        def _distance(meta):
            rid = meta.get("report_id", "")
            try:
                hhmm = rid.split("-")[2]
                report_hour = int(hhmm[:2]) + int(hhmm[2:4]) / 60.0
            except (IndexError, ValueError):
                return 99.0
            return abs(report_hour - hour)

        best = min(candidates, key=_distance)
        if _distance(best) <= _CLOCK_TOLERANCE_H:
            return best["report_id"], best.get("job_name"), None
        # A clock reference that matches nothing is worth saying out loud,
        # rather than silently serving a different report.
        when = f"{hour % 12 or 12}{'pm' if hour >= 12 else 'am'}"
        return None, None, (
            f"I don't have a report from around {when}. The most recent one is "
            f"{candidates[0]['report_id']} — reply MORE {candidates[0]['report_id']} for that one."
        )

    return candidates[0]["report_id"], candidates[0].get("job_name"), None


def _match_command(text: str) -> Optional[tuple]:
    """Classify a message as a report command.

    Returns ``(verb, tail, number)`` — verb in {"more", "why", "pdf"} — or None
    when the message is ordinary conversation and belongs to the LLM.

    A bare verb always counts. A verb with a tail counts only when the message
    is short AND the tail refers to a report, which is what keeps "why did you
    pick the Yankees?" and "more info about tomorrow" out of here.
    """
    stripped = text.strip().rstrip("?!.").strip()
    if not stripped:
        return None

    for verb, pattern in (("why", _WHY_VERB), ("more", _MORE_VERB), ("pdf", _PDF_VERB)):
        m = pattern.match(stripped)
        if not m:
            continue
        tail = stripped[m.end():].strip(" ,.:;-")
        number = int(m.group(1)) if verb == "why" else None

        if _FUTURE_INTENT_RE.search(tail):
            return None  # a preference for the next run, not a lookup

        tail = _FILLER_RE.sub(" ", tail).strip(" ,.:;-")
        if not tail:
            return verb, "", number
        if len(stripped.split()) > _LOOSE_MAX_WORDS:
            return None
        if _REPORT_NOUN.search(tail) or _target_hour(tail) is not None or _re.search(
            _REPORT_ID_RE, tail, _re.IGNORECASE
        ):
            return verb, tail, number
        return None
    return None


def handle_more_command(text: str, sender: str) -> Optional[List[str]]:
    """MORE — send the items the concise report held back."""
    match = _match_command(text)
    if not match or match[0] != "more":
        return None
    _, tail, _ = match

    report_id, job_name, error = _resolve_report(tail)
    if error:
        return [error]

    detail = _outbox.load_detail(report_id)
    if not detail:
        name = _RESEND_REPORT_NAMES.get(job_name, job_name)
        return [f"I don't have the full {name} list any more (ref {report_id}). Run the job again for a fresh one."]

    items = detail.get("items") or []
    shown = int(detail.get("shown") or 0)
    rest = items[shown:]
    if not rest:
        return [f"That was the whole list — all {len(items)} of them are in the report above."]

    intro = detail.get("more_intro") or "The rest of the list:"
    body = "\n\n".join([intro] + [i["headline"] for i in rest if i.get("headline")])
    body += "\n\n\u2014\nReply WHY <n> for the reasoning \u00b7 PDF for the file"
    return split_imessage_content(body)


def handle_why_command(text: str, sender: str) -> Optional[List[str]]:
    """WHY <n> — the reasoning behind one numbered item."""
    match = _match_command(text)
    if not match or match[0] != "why":
        return None
    _, tail, n = match

    report_id, job_name, error = _resolve_report(tail)
    if error:
        return [error]

    detail = _outbox.load_detail(report_id)
    if not detail:
        name = _RESEND_REPORT_NAMES.get(job_name, job_name)
        return [f"I don't have the detail for that {name} report any more (ref {report_id})."]

    items = detail.get("items") or []
    if not 1 <= n <= len(items):
        return [f"There's no #{n} in that report — it has {len(items)} item(s)."]

    return split_imessage_content(items[n - 1].get("detail") or "No extra detail on that one.")


def handle_pdf_command(text: str, sender: str) -> Optional[List[str]]:
    """PDF / RESEND — send the archived attachment for a report.

    Reports are delivered as text now, so this is the only path that pushes a
    PDF, and it only runs because Henry asked for it.
    """
    match = _match_command(text)
    if not match or match[0] != "pdf":
        return None
    _, tail, _ = match

    report_id, job_name, error = _resolve_report(tail)
    if error:
        return [error]

    report_name = _RESEND_REPORT_NAMES.get(job_name, job_name)
    pdf_path = _outbox.get_outbox_pdf_path(report_id)
    if not pdf_path:
        return [f"There's no PDF stored for {report_id} — the {report_name} content was texted to you. Reply MORE for the full list."]

    meta = _outbox.load_report_meta(report_id) or {}
    logger.info("PDF request: sending %s → %s", report_id, sender)
    receipt = send_imessage_attachment(sender, str(pdf_path), report_id=report_id)
    attempts = (meta.get("send_attempts") or 0) + receipt.attempts
    _outbox.update_report_status(report_id, f"pdf_{receipt.status}", attempts=attempts)

    if receipt.status == "verified_delivered":
        return [f"\u2705 {report_name} PDF sent (ref: {report_id})."]
    if receipt.status == "submitted_unverified":
        # Honest wording: Messages accepted it but chat.db never confirmed the
        # upload. Saying "sent" here is what hid a month of missing reports.
        return [(
            f"I handed the {report_name} PDF to Messages but couldn't confirm it "
            f"landed (ref: {report_id}). If it didn't show up, reply PDF to try "
            f"again \u2014 the text version above has the same content."
        )]
    return [(
        f"The {report_name} PDF wouldn't send (ref: {report_id}). "
        f"The text version above has everything in it \u2014 reply MORE for the full list."
    )]


def handle_report_command(text: str, sender: str) -> Optional[List[str]]:
    """Deterministic report commands — never calls an LLM, never runs a job.

    Returns the message(s) to send back, or None when the text is not a report
    command (the caller then proceeds to the LLM).

    Supported, in strict or conversational form:
      MORE / more picks / more of the 3pm picks you sent me / MORE <REPORT_ID>
      WHY 3 / why 3 picks / why #3 of the 9am report
      PDF / send me the pdf / RESEND PICKS / RESEND <REPORT_ID>
    """
    for handler in (handle_more_command, handle_why_command, handle_pdf_command):
        reply = handler(text, sender)
        if reply is not None:
            return reply
    return None


def handle_resend_command(text: str, sender: str) -> Optional[str]:
    """Backwards-compatible single-string wrapper around handle_report_command."""
    reply = handle_report_command(text, sender)
    if reply is None:
        return None
    return "\n\n".join(reply)


# ============================================================================
# BACKGROUND IMESSAGE WORKER: DeepSeek Primary + Gemini Backup with CACHING
# ============================================================================


# ============================================================================
# IMESSAGE POLLER SELF-HEALING
# ============================================================================
#
# History: on 2026-08-24 the poller hit five consecutive "authorization
# denied" errors reading chat.db (Full Disk Access is decided at process
# launch, and this process had been started before the grant), exhausted its
# 2/4/8/16s backoff in about 30 seconds, and exited the worker thread for
# good. The process stayed alive and /health kept returning 200, so Ivy went
# on texting out while deaf to every incoming message — for three days,
# silently. Two things were wrong: giving up permanently, and doing so
# invisibly.
#
# Now: retry indefinitely with capped backoff, and if the failure outlasts
# POLLER_ESCALATE_AFTER_SECONDS, exit the process so launchd (KeepAlive=true)
# relaunches it. A TCC denial cannot be cleared in-process — only a fresh
# process can — so exiting IS the recovery, not a crash. POLLER_STATE is
# surfaced through /ready so a wedged poller is visible even when chat.db is
# perfectly readable.

POLLER_MAX_BACKOFF_SECONDS = 60
POLLER_ESCALATE_AFTER_SECONDS = int(os.environ.get("POLLER_ESCALATE_AFTER_SECONDS", "300"))
POLLER_STALE_AFTER_SECONDS = int(os.environ.get("POLLER_STALE_AFTER_SECONDS", "300"))
# macOS TCC "authorization denied" on chat.db is never transient for a
# running process — observed 2026-07-16, 2026-08-24 and 2026-09-01, each time
# only a fresh process got access back. So it is escalated after this many
# consecutive failures (~10 s) instead of after POLLER_ESCALATE_AFTER_SECONDS,
# which left Ivy deaf for five minutes and gave the monitor time to page.
POLLER_AUTH_DENIED_MAX_FAILURES = int(os.environ.get("POLLER_AUTH_DENIED_MAX_FAILURES", "3"))

# Per-sender short-term memory so follow-ups ("yes", "the full recipe") are
# answered in context. Without it, "Yes, I want the full recipe" arrived as a
# standalone message and DeepSeek launched the Familia Meal Planner job
# (2026-08-28) instead of giving the pizza-dough recipe it had just offered.
CONVERSATION_MAX_MESSAGES = int(os.environ.get("CONVERSATION_MAX_MESSAGES", "8"))
CONVERSATION_TTL_SECONDS = int(os.environ.get("CONVERSATION_TTL_SECONDS", "2700"))
_CONVERSATIONS: Dict[str, List[Dict[str, Any]]] = {}
_CONVERSATIONS_LOCK = threading.Lock()


def conversation_history(sender: str) -> List[Dict[str, str]]:
    """Recent turns for ``sender`` as [{role, content}], oldest first.
    Entries older than CONVERSATION_TTL_SECONDS are dropped."""
    cutoff = time.time() - CONVERSATION_TTL_SECONDS
    with _CONVERSATIONS_LOCK:
        turns = [t for t in _CONVERSATIONS.get(sender, []) if t["ts"] >= cutoff]
        _CONVERSATIONS[sender] = turns
        return [{"role": t["role"], "content": t["content"]} for t in turns]


def remember_turn(sender: str, user_text: str, reply_text: Optional[str]) -> None:
    now = time.time()
    with _CONVERSATIONS_LOCK:
        turns = _CONVERSATIONS.setdefault(sender, [])
        turns.append({"role": "user", "content": user_text, "ts": now})
        if reply_text:
            turns.append({"role": "assistant", "content": str(reply_text), "ts": now})
        del turns[:-CONVERSATION_MAX_MESSAGES]


def format_history_for_prompt(history: List[Dict[str, str]]) -> str:
    lines = ["Recent conversation (oldest first):"]
    for turn in history:
        speaker = "User" if turn.get("role") == "user" else "Ivy"
        lines.append(f"{speaker}: {turn.get('content', '')}")
    return "\n".join(lines)


def _is_tcc_denial(exc: BaseException) -> bool:
    return "authorization denied" in str(exc).lower()
# Escape hatch: a developer running `uvicorn main:app` by hand does not want a
# broken chat.db to kill their foreground server after five minutes.
POLLER_EXIT_ON_UNRECOVERABLE = os.environ.get("POLLER_EXIT_ON_UNRECOVERABLE", "true").lower() == "true"

POLLER_STATE: Dict[str, Any] = {
    "enabled": ENABLE_IMESSAGE_POLLER,
    "running": False,
    "started_at": None,
    "last_success_ts": None,
    "consecutive_failures": 0,
    "last_error": None,
    "escalations": 0,
}


def _escalate_poller_restart(reason: str) -> None:
    """Exit the process so launchd relaunches it with a fresh TCC decision."""
    POLLER_STATE["escalations"] += 1
    POLLER_STATE["running"] = False
    POLLER_STATE["last_error"] = reason
    if not POLLER_EXIT_ON_UNRECOVERABLE:
        logger.error(
            "🔁 iMessage poller unrecoverable (%s), but POLLER_EXIT_ON_UNRECOVERABLE=false — "
            "staying up and continuing to retry. /ready will report the poller unhealthy.",
            reason,
        )
        return
    logger.error(
        "🔁 iMessage poller unrecoverable (%s). Exiting process so launchd relaunches it.",
        reason,
    )
    for handler in list(logger.handlers) + list(logging.getLogger().handlers):
        try:
            handler.flush()
        except Exception:
            pass
    os._exit(1)


def poller_healthy() -> bool:
    """True when the poller is either deliberately off or demonstrably alive.

    "Alive" means a poll cycle completed recently — not merely that the thread
    object exists, which is what made the 2026-08-24 outage invisible.
    """
    if not POLLER_STATE["enabled"]:
        return True
    if not POLLER_STATE["running"]:
        return False
    reference = POLLER_STATE["last_success_ts"] or POLLER_STATE["started_at"]
    if reference is None:
        return False
    # monotonic, not wall-clock: on macOS it does not advance while the Mac
    # sleeps, so the first /ready after wake doesn't see a "stale" heartbeat
    # that is really just the nap the whole machine took.
    return (time.monotonic() - reference) < POLLER_STALE_AFTER_SECONDS


def background_imessage_worker() -> None:
    """Poll iMessage database and respond via DeepSeek → Gemini failover chain.
    
    🆕 Now with prompt caching enabled for 80-90% token savings!
    """
    logger.info("🤖 Ivy Polling Thread Engaged (DeepSeek Primary + Gemini Backup Core)")
    logger.info(f"💾 Prompt Caching: {'ENABLED' if (ENABLE_PROMPT_CACHING and CACHING_AVAILABLE) else 'DISABLED'}")
    
    POLLER_STATE.update({
        "running": True,
        "started_at": time.monotonic(),
        "consecutive_failures": 0,
        "last_error": None,
    })

    # get_last_message_id() returns 0 for an empty database and None only on a
    # real access error, so None unambiguously means "cannot read chat.db".
    last_id = get_last_message_id()
    startup_attempt = 0
    startup_began = time.time()
    while last_id is None:
        startup_attempt += 1
        POLLER_STATE["consecutive_failures"] = startup_attempt
        POLLER_STATE["last_error"] = "chat.db unreadable at startup"
        logger.error(
            "❌ Cannot access chat.db (attempt %d). Verify Full Disk Access for the "
            "interpreter launchd starts. Retrying...",
            startup_attempt,
        )
        if (time.time() - startup_began) >= POLLER_ESCALATE_AFTER_SECONDS:
            _escalate_poller_restart("chat.db unreadable throughout poller startup")
            return
        if startup_attempt >= POLLER_AUTH_DENIED_MAX_FAILURES and not (
            os.path.exists(CHAT_DB_PATH) and os.access(CHAT_DB_PATH, os.R_OK)
        ):
            _escalate_poller_restart(
                f"chat.db access denied at startup ({startup_attempt} attempts) — "
                "Full Disk Access is missing for the interpreter launchd starts"
            )
            return
        time.sleep(min(2 ** startup_attempt, POLLER_MAX_BACKOFF_SECONDS))
        last_id = get_last_message_id()

    current_date_str = datetime.now().strftime("%A, %B %d, %Y")
    deepseek_sys_instruction = DEEPSEEK_SYSTEM_INSTRUCTION_TEMPLATE.format(
        current_date_str=current_date_str
    )

    consecutive_failures = 0
    first_failure_ts: Optional[float] = None

    def _mark_poll_success() -> None:
        """A completed cycle — including one that found no new message — proves
        the read path works, so it clears the failure streak and the heartbeat."""
        nonlocal consecutive_failures, first_failure_ts
        consecutive_failures = 0
        first_failure_ts = None
        POLLER_STATE["consecutive_failures"] = 0
        POLLER_STATE["last_error"] = None
        POLLER_STATE["last_success_ts"] = time.monotonic()

    while True:
        try:
            time.sleep(POLLING_INTERVAL)
            row = safe_fetch_last_message(last_id)

            if not row:
                _mark_poll_success()
                continue

            msg_id, text, sender = row
            last_id = msg_id

            # ========== Authorization Check ==========
            is_authorized = False
            if sender.lower() == "me":
                is_authorized = True
            else:
                favorites_path = "favorites.json"
                if os.path.exists(favorites_path):
                    try:
                        with open(favorites_path, "r") as f:
                            allowed_contacts = json.load(f)
                        if sender in allowed_contacts:
                            is_authorized = True
                    except Exception as json_err:
                        logger.warning(
                            "⚠️ Security Alert: Failed to parse favorites.json: %s",
                            str(json_err),
                        )
                else:
                    logger.warning(
                        "⚠️ Security Alert: favorites.json missing! "
                        "Blocking external sender %s.",
                        sender,
                    )

            if not is_authorized:
                logger.info(
                    "🛑 Security Exception: Trigger blocked. Unauthorized Contact ID: %s",
                    sender,
                )
                _mark_poll_success()
                continue

            logger.info("📩 Inbound Trigger Isolated: %s", text)
            reply = None

            # ===== REPORT COMMANDS: MORE / WHY <n> / PDF (deterministic) =====
            report_reply = handle_report_command(text, sender)
            if report_reply is not None:
                for _bubble in report_reply:
                    run_local_applescript_send(sender, _bubble)
                _mark_poll_success()
                continue

            history = conversation_history(sender)

            # ========== PHASE 1: DEEPSEEK PRIMARY ==========
            try:
                logger.info("🧠 Querying Primary Engine (DeepSeek, %d prior turns)...", len(history))
                reply = execute_deepseek_call(text, deepseek_sys_instruction, history=history)
            except Exception as deepseek_err:
                logger.error(
                    "❌ DeepSeek Primary Layer Fault: %s. Switching to Backup Protocol...",
                    str(deepseek_err),
                )
                reply = None

            # ========== PHASE 2: GEMINI BACKUP (WITH CACHING) ==========
            if not reply:
                try:
                    logger.info("🛡️ Primary Engine Offline. Engaging Backup Core (Gemini SDK)...")
                    reply = _gemini_backup_reply(text, history=history)
                except Exception as gemini_err:
                    logger.error(
                        "❌ Gemini Backup Layer Fault: %s\nException type: %s\nFull traceback: %s.",
                        str(gemini_err),
                        type(gemini_err).__name__,
                        repr(gemini_err),
                    )
                    reply = None

            # ========== DISPATCH RESPONSE ==========
            if reply:
                send_result = run_local_applescript_send(sender, str(reply))
                if send_result == "SUCCESS":
                    logger.info("📤 Reply delivered to Messages for %s (%d chars).", sender, len(str(reply)))
                remember_turn(sender, text, str(reply))
            else:
                logger.warning("❌ Both Primary and Backup layers produced no usable reply.")
                remember_turn(sender, text, None)

            _mark_poll_success()

        except Exception as database_err:
            consecutive_failures += 1
            if first_failure_ts is None:
                first_failure_ts = time.time()
            failing_for = time.time() - first_failure_ts
            POLLER_STATE["consecutive_failures"] = consecutive_failures
            POLLER_STATE["last_error"] = str(database_err)
            logger.error(
                "❌ Database polling loop exception (#%d, failing for %.0fs): %s",
                consecutive_failures,
                failing_for,
                str(database_err),
            )
            if _is_tcc_denial(database_err) and consecutive_failures >= POLLER_AUTH_DENIED_MAX_FAILURES:
                _escalate_poller_restart(
                    f"chat.db access revoked — 'authorization denied' {consecutive_failures}x in "
                    f"{failing_for:.0f}s; only a relaunch gets Full Disk Access back"
                )
                first_failure_ts = time.time()  # only reached when exit is disabled
            elif failing_for >= POLLER_ESCALATE_AFTER_SECONDS:
                _escalate_poller_restart(
                    f"{consecutive_failures} consecutive polling failures over {failing_for:.0f}s"
                )
                first_failure_ts = time.time()  # only reached when exit is disabled
            # Capped exponential backoff — retry forever rather than exiting the
            # thread, which is what left Ivy silently deaf for three days.
            time.sleep(min(2 ** consecutive_failures, POLLER_MAX_BACKOFF_SECONDS))


# ============================================================================
# FASTAPI ENDPOINTS
# ============================================================================


@app.get("/health")
def health_endpoint(authenticated: bool = Depends(verify_api_key)):
    """Liveness check with per-provider auth status.

    Reports configured/authenticated/reachable for each LLM provider so the
    difference between a missing key and a rejected key is always visible.
    Never touches the network on the request thread — provider auth comes
    from the background probe cache (see cached_provider_status).
    """
    providers = cached_provider_status()
    return {
        "status": "ok",
        "providers": providers,
        "tools": compute_tool_statuses(),
        "caching": {
            "enabled": ENABLE_PROMPT_CACHING and CACHING_AVAILABLE,
            "cache_ttl_seconds": 3600 if ENABLE_PROMPT_CACHING else None,
        },
    }


@app.get("/capabilities")
def capabilities_endpoint(authenticated: bool = Depends(verify_api_key)):
    """List all tools and their readiness status, plus every registered job
    and whether it's actually available. Unavailable jobs (e.g. bravo_scout,
    whose implementation doesn't exist) are surfaced with their reason —
    never silently omitted, which would look like a clean bill of health."""
    return {
        "tools": compute_tool_statuses(),
        "jobs": job_runner.list_jobs() if JOB_RUNNER_AVAILABLE else [],
        "caching_stats": cache_manager.get_cache_statistics() if CACHING_AVAILABLE else None
    }


@app.get("/ready")
def ready_endpoint(authenticated: bool = Depends(verify_api_key)):
    """Readiness probe — distinct from /health's bare liveness check.
    Returns 503 (not 200 with status:"degraded" buried in the body) when a
    component actually required to serve requests is unavailable.

    llm_provider_authenticated requires at least one provider to have passed a
    live auth probe — a key being present in the environment is not sufficient.
    """
    checks: Dict[str, Any] = {
        "chat_db_readable": os.path.exists(CHAT_DB_PATH) and os.access(CHAT_DB_PATH, os.R_OK),
        # Sits next to chat_db_readable deliberately: these two fail together.
        # If uv ever repoints the interpreter, the Full Disk Access grant is
        # silently lost and chat.db reads start failing with no stated cause.
        "interpreter_matches_tcc_grant": _interpreter_matches_tcc_grant(),
        # os.access() only proves the file is readable, not that the poller is
        # actually consuming it — the distinction that hid the 2026-08-24 outage.
        "imessage_poller_healthy": poller_healthy(),
    }
    try:
        receipts.list_recent(limit=1)
        checks["receipts_db_writable"] = True
    except Exception as exc:
        logger.warning("Receipts DB check failed: %s", exc)
        checks["receipts_db_writable"] = False

    # Cached probe only — waits briefly for the first probe right after
    # startup, never runs one on this thread.
    providers = cached_provider_status(wait_for_first=4.0)
    any_authenticated = any(p.get("authenticated") for p in providers.values())
    checks["llm_provider_authenticated"] = any_authenticated

    ready = all(checks.values())
    payload: Dict[str, Any] = {"ready": ready, "checks": checks}
    if not ready:
        raise HTTPException(status_code=503, detail=payload)
    return payload


@app.get("/imessage/attachments")
def imessage_attachments_endpoint(
    since: float,
    filename: Optional[str] = None,
    handle: Optional[str] = None,
    limit: int = 20,
    authenticated: bool = Depends(verify_api_key),
):
    """Outgoing attachment rows from chat.db newer than ``since`` (unix
    seconds), optionally narrowed to one ``filename`` (the name the recipient
    sees) and/or one ``handle`` (phone number). Each row carries a ``state``
    of delivered / failed / pending.

    This is how job subprocesses — which lack Full Disk Access — verify that
    a PDF they just sent actually left the Mac (ivy_core.attachment_verify).
    """
    try:
        rows = attachment_verify.fetch_outgoing_attachments(
            since_ts=since, filename=filename or None, handle=handle or None, limit=limit,
        )
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail=f"chat.db unreadable: {exc}")
    return {"attachments": rows, "count": len(rows)}


@app.get("/version")
def version_endpoint(authenticated: bool = Depends(verify_api_key)):
    """Git SHA, project root, Python executable, PID, start time, hostname,
    dirty-tree state — so "which commit is this gateway actually running"
    is never a guessing game."""
    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=PROJECT_ROOT_DIR, timeout=5,
        ).stdout.strip() or "unknown"
    except Exception:
        git_sha = "unknown"
    try:
        dirty_output = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=PROJECT_ROOT_DIR, timeout=5,
        ).stdout.strip()
        dirty = bool(dirty_output)
    except Exception:
        dirty = None
    return {
        "git_sha": git_sha,
        "dirty_working_tree": dirty,
        "project_root": PROJECT_ROOT_DIR,
        "python_executable": sys.executable,
        # The resolved binary, not the venv symlink. macOS records Full Disk
        # Access against the real interpreter path, and .venv/bin/python points
        # into uv's store via a floating minor-version symlink — so the symlink
        # alone hides the one fact needed to diagnose a lost FDA grant.
        "python_base_executable": os.path.realpath(
            getattr(sys, "_base_executable", sys.executable)
        ),
        "pid": os.getpid(),
        "process_started_at": PROCESS_STARTED_AT.isoformat(),
        "hostname": socket.gethostname(),
    }


@app.get("/executions")
def list_executions_endpoint(
    limit: int = 50,
    job_name: Optional[str] = None,
    authenticated: bool = Depends(verify_api_key),
):
    """Recent job execution receipts — the runtime's own record of what was
    actually dispatched, not something a model gets to assert."""
    return {"executions": receipts.list_recent(limit=limit, job_name=job_name)}


@app.get("/executions/{execution_id}")
def get_execution_endpoint(execution_id: str, authenticated: bool = Depends(verify_api_key)):
    record = receipts.get_execution(execution_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    return record


def get_capabilities() -> str:
    """Return human-readable capabilities summary."""
    statuses = compute_tool_statuses()
    lines = ["Ivy Gateway capabilities:"]
    for s in statuses:
        mark = "✅" if s["status"] == "ready" else "❌"
        suffix = "" if s["status"] == "ready" else f" — {s['reason']}"
        lines.append(f"{mark} {s['tool_name']}: {s['description']}{suffix}")
    return "\n".join(lines)


# ============================================================================
# CACHE METRICS ENDPOINT (NEW)
# ============================================================================

@app.get("/cache-stats")
def get_cache_stats(authenticated: bool = Depends(verify_api_key)):
    """🆕 View prompt caching performance and cost savings."""
    if not CACHING_AVAILABLE:
        return {"error": "Caching not available", "caching_enabled": False}
    
    stats = cache_manager.get_cache_statistics()
    return {
        "caching_enabled": ENABLE_PROMPT_CACHING,
        "statistics": stats,
        "info": "Cache hits save ~90% on input tokens. Monitor hit_rate_percent for optimization."
    }


# ============================================================================
# ============================================================================
# VOICE ASSISTANT ENDPOINTS
# ============================================================================


@app.post("/voice/query", response_model=VoiceQueryResponse)
def voice_query(
    req: VoiceQueryRequest,
    authenticated: bool = Depends(verify_api_key),
):
    """Process a voice query with session management and cache optimization."""
    if not VOICE_ASSISTANT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Voice assistant not available"
        )

    try:
        # Get or create session
        if req.session_id:
            session = voice_session_manager.get_session(req.session_id)
        else:
            session = voice_session_manager.get_user_session(req.user_id)

        if not session:
            raise HTTPException(status_code=400, detail="Invalid session")

        # Record the user's turn exactly once, regardless of which provider
        # ends up answering.
        session.add_message("user", req.query)

        reply = None
        cached_tokens = 0

        # ---- Phase 1: DeepSeek primary ----
        try:
            reply = execute_deepseek_call(
                req.query,
                DEEPSEEK_SYSTEM_INSTRUCTION_TEMPLATE.format(
                    current_date_str=datetime.now().strftime("%A, %B %d, %Y")
                ),
            )
        except Exception as deepseek_err:
            logger.warning(f"Voice: DeepSeek primary layer fault: {deepseek_err}")
            reply = None

        # ---- Phase 2: Gemini backup (cache-optimized, with tool execution) ----
        if not reply:
            try:
                messages = voice_processor.create_voice_prompt(
                    user_query=req.query,
                    session=session,
                    system_instruction=GEMINI_SYSTEM_INSTRUCTION,
                    tool_declarations=GEMINI_TOOL_DECLARATIONS
                )
                response = gemini_model.generate_content(
                    messages,
                    tools=[genai.types.Tool(function_declarations=GEMINI_TOOL_DECLARATIONS)],
                )

                if ENABLE_CACHE_METRICS_LOGGING and CACHING_AVAILABLE:
                    cached_tokens, _ = cache_manager.log_cache_efficiency(
                        response, endpoint="voice_query", model="gemini-2.5-flash"
                    )

                if response.candidates and response.candidates[0].content:
                    parts = response.candidates[0].content.parts
                    text_reply = ""
                    tool_calls = []
                    for part in parts:
                        if hasattr(part, "text") and part.text:
                            text_reply += part.text
                        # Truthiness, not hasattr — see _gemini_backup_reply.
                        if getattr(part, "function_call", None):
                            tool_calls.append(part.function_call)

                    if tool_calls:
                        tool_results = []
                        for call in tool_calls:
                            tool_name = call.name
                            tool_args = call.args
                            if tool_name in ["add_apple_reminder", "fetch_apple_reminders"]:
                                tool_args["list_name"] = "Household"
                            tool_results.append((tool_name, _execute_tool_call(tool_name, tool_args)))

                        follow_up_response = gemini_model.generate_content(
                            [
                                *messages,
                                {"role": "model", "parts": parts},
                                {
                                    "role": "function",
                                    "parts": [
                                        {"function_response": {"name": name, "response": {"result": result}}}
                                        for name, result in tool_results
                                    ],
                                },
                            ],
                            tools=[genai.types.Tool(function_declarations=GEMINI_TOOL_DECLARATIONS)],
                        )
                        if follow_up_response.candidates:
                            follow_up_parts = follow_up_response.candidates[0].content.parts
                            reply = "".join(
                                p.text for p in follow_up_parts if hasattr(p, "text")
                            ).strip() or None
                    else:
                        reply = text_reply.strip() or None
            except Exception as gemini_err:
                logger.warning(f"Voice: Gemini backup layer fault: {gemini_err}")
                reply = None

        if not reply:
            reply = "I didn't understand that. Please try again."

        session.add_message("assistant", reply)
        voice_processor.log_voice_query(session, reply, cached_tokens)

        return VoiceQueryResponse(
            session_id=session.session_id,
            response=reply,
            cached_tokens=cached_tokens,
            total_queries=session.total_queries,
            cache_hit_rate=(session.cache_hits / session.total_queries * 100) if session.total_queries > 0 else 0.0
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Voice query error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/voice/session")
def create_voice_session(
    user_id: str,
    authenticated: bool = Depends(verify_api_key),
):
    """Create a new voice session for a user."""
    if not VOICE_ASSISTANT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Voice assistant not available"
        )

    session = voice_session_manager.create_session(user_id)
    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "created_at": session.created_at.isoformat(),
        "ttl_seconds": session.ttl_seconds
    }


@app.get("/voice/session/{session_id}")
def get_voice_session(
    session_id: str,
    authenticated: bool = Depends(verify_api_key),
):
    """Get voice session details."""
    if not VOICE_ASSISTANT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Voice assistant not available"
        )

    session = voice_session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return VoiceSessionResponse(
        session_id=session.session_id,
        user_id=session.user_id,
        state=session.state.value,
        message_count=len(session.messages),
        cache_hit_rate=(session.cache_hits / session.total_queries * 100) if session.total_queries > 0 else 0.0
    )


@app.delete("/voice/session/{session_id}")
def close_voice_session(
    session_id: str,
    authenticated: bool = Depends(verify_api_key),
):
    """Close a voice session."""
    if not VOICE_ASSISTANT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Voice assistant not available"
        )

    if voice_session_manager.close_session(session_id):
        return {"status": "closed", "session_id": session_id}
    raise HTTPException(status_code=404, detail="Session not found")


@app.get("/voice/stats")
def get_voice_stats(authenticated: bool = Depends(verify_api_key)):
    """Get voice assistant statistics."""
    if not VOICE_ASSISTANT_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Voice assistant not available"
        )

    stats = voice_session_manager.get_stats()
    return {
        "voice_stats": stats,
        "cache_stats": cache_manager.get_cache_statistics() if CACHING_AVAILABLE else None
    }


@app.get("/jobs")
def list_jobs(authenticated: bool = Depends(verify_api_key)):
    """List all available jobs that can be run on-demand."""
    if not JOB_RUNNER_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Job runner not available"
        )

    return {
        "jobs": job_runner.list_jobs(),
        "message": "Run jobs via 'ivy run <job_name>' in iMessage or POST /run-job with X-API-Key header"
    }


@app.post("/run-job")
def run_job_endpoint(
    job_name: str,
    authenticated: bool = Depends(verify_api_key)
):
    """Execute a job by name (API endpoint for direct access)."""
    if not JOB_RUNNER_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="Job runner not available"
        )

    result = run_job(job_name)
    return {
        "job": job_name,
        "result": result
    }


# ============================================================================
# MAIN
# ============================================================================


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
    )
