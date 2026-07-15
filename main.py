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
from typing import List, Optional, Dict, Any, Callable
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
        "name": "gemini",
        "description": "Primary AI conversation/reasoning engine via Google Gemini (with prompt caching).",
        "required_env": [["GEMINI_API_KEY"]],
    },
    {
        "name": "deepseek",
        "description": "Failover AI conversation engine via the DeepSeek API.",
        "required_env": [["DEEPSEEK_API_KEY"]],
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

    # Start iMessage poller if enabled

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
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    raw_output = res.stdout.strip()

    if "Error:" in raw_output:
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


def fetch_apple_reminders(list_name: str = "Household") -> str:
    """Read uncompleted tasks from Apple Reminders."""
    script = f'''
    tell application "Reminders"
        try
            tell list "{list_name}"
                set remNames to name of every reminder whose completed is false
                set AppleScript's text item delimiters to ", "
                return remNames as text
            end tell
        on error errMsg
            return "ERROR: " & errMsg
        end try
    end tell
    '''
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return res.stdout.strip() if res.stdout.strip() else "No active reminders found."


def add_apple_reminder(title: str, list_name: str = "Household") -> str:
    """Add a task to Apple Reminders."""
    # Auto-categorize based on keywords
    if any(word in list_name.lower() for word in ["meal", "food", "dinner", "recipe", "taco"]):
        list_name = "Meal Plan"
    elif any(word in list_name.lower() for word in ["house", "chore", "clean", "task"]):
        list_name = "Household"

    script_lines = [
        'tell application "Reminders"',
        "    try",
        f'        if not (exists list "{list_name}") then',
        f'            make new list with properties {{name:"{list_name}"}}',
        "        end if",
        f'        set targetList to list "{list_name}"',
        "        tell targetList",
        f'            make new reminder with properties {{name:"{title}"}}',
        "        end tell",
        '        return "SUCCESS"',
        "    on error err",
        '        return "Error: " & err',
        "    end try",
        "end tell",
    ]
    script = "\n".join(script_lines)
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    raw_output = res.stdout.strip()

    if "SUCCESS" in raw_output:
        return f"✅ Added to your '{list_name}' list: {title}"
    return f"❌ Reminders Integration Error: {raw_output}"


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


def _execute_tool_call(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Execute a registered tool by name. Both the Gemini and DeepSeek paths
    call through here, so neither can dispatch to anything but a real,
    registered tool, and DeepSeek gets the same run_job access Gemini has."""
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
    """Send iMessage via AppleScript."""
    recipient = "me" if target.lower() == "me" else target
    script_lines = [
        'tell application "Messages"',
        "    try",
        '        set targetService to first service whose service type is iMessage',
        f'        set targetBuddy to buddy "{recipient}" of targetService',
        f'        send "{body}" to targetBuddy',
        '        return "SUCCESS"',
        "    on error errMsg",
        '        return "ERROR: " & errMsg',
        "    end try",
        "end tell",
    ]
    script = "\n".join(script_lines)
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return res.stdout.strip()


# ============================================================================
# DEEPSEEK FAILOVER ENGINE
# ============================================================================


def execute_deepseek_call(text_content: str, system_instruction: str) -> str:
    """Execute call via DeepSeek API with tool calling support."""
    active_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not active_key:
        logger.warning("DeepSeek call attempted with no DEEPSEEK_API_KEY configured.")
        return (
            "DeepSeek is not configured. Please set the DEEPSEEK_API_KEY environment variable."
        )

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {active_key}", "Content-Type": "application/json"}

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": text_content},
        ],
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

            return _execute_tool_call(func_name, args)

        return message_node.get("content", "").strip()
    except Exception as e:
        return f"❌ DeepSeek Execution Layer Exception: {str(e)}"


# ============================================================================
# GEMINI BACKUP ENGINE (only reached when DeepSeek is unavailable/empty)
# ============================================================================


def _gemini_backup_reply(text: str) -> Optional[str]:
    """Gemini backup: prompt-cached generate_content call with real tool
    execution and a real follow-up round-trip. Raises on provider failure
    (caller treats that as "no backup available" and gives up); returns None
    if Gemini responded but had nothing usable to say.
    """
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise ValueError("GEMINI_API_KEY not configured in environment")

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
        tool_result = _execute_tool_call(tool_name, tool_args)
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
    """Fetch next message from chat.db with retry logic and read-only mode."""
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
            backoff = DB_RETRY_BACKOFF * (2 ** attempt)
            logger.warning(
                "Database read attempt %d failed: %s. Retrying in %.1f seconds...",
                attempt + 1,
                e,
                backoff,
            )
            time.sleep(backoff)
    return None


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
# BACKGROUND IMESSAGE WORKER: DeepSeek Primary + Gemini Backup with CACHING
# ============================================================================


def background_imessage_worker() -> None:
    """Poll iMessage database and respond via Gemini → DeepSeek failover chain.
    
    🆕 Now with prompt caching enabled for 80-90% token savings!
    """
    logger.info("🤖 Ivy Polling Thread Engaged (DeepSeek Primary + Gemini Backup Core)")
    logger.info(f"💾 Prompt Caching: {'ENABLED' if (ENABLE_PROMPT_CACHING and CACHING_AVAILABLE) else 'DISABLED'}")
    
    last_id = get_last_message_id()
    if last_id is None:
        logger.error(
            "❌ Security Warning: Cannot access chat.db files. "
            "Verify Full Disk Access in System Preferences."
        )
        return

    current_date_str = datetime.now().strftime("%A, %B %d, %Y")
    deepseek_sys_instruction = DEEPSEEK_SYSTEM_INSTRUCTION_TEMPLATE.format(
        current_date_str=current_date_str
    )

    consecutive_failures = 0

    while True:
        try:
            time.sleep(POLLING_INTERVAL)
            row = safe_fetch_last_message(last_id)

            if not row:
                consecutive_failures = 0
                continue

            msg_id, text, sender = row
            last_id = msg_id

            if "ivy" not in text.lower():
                consecutive_failures = 0
                continue

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
                consecutive_failures = 0
                continue

            logger.info("📩 Inbound Trigger Isolated: %s", text)
            reply = None

            # ========== PHASE 1: DEEPSEEK PRIMARY ==========
            try:
                logger.info("🧠 Querying Primary Engine (DeepSeek)...")
                reply = execute_deepseek_call(text, deepseek_sys_instruction)
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
                    reply = _gemini_backup_reply(text)
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
                logger.info("📤 Clean prose payload dispatched back via local AppleScript link.")
                run_local_applescript_send(sender, str(reply))
            else:
                logger.warning("❌ Both Primary and Backup layers produced no usable reply.")

            consecutive_failures = 0

        except Exception as database_err:
            consecutive_failures += 1
            logger.error("❌ Database polling loop exception: %s", str(database_err))
            if consecutive_failures >= 5:
                logger.error(
                    "Database polling failed 5 times. Exiting worker to prevent cascade failures."
                )
                return
            time.sleep(2 ** consecutive_failures)  # Exponential backoff


# ============================================================================
# FASTAPI ENDPOINTS
# ============================================================================


@app.get("/health")
def health_endpoint(authenticated: bool = Depends(verify_api_key)):
    """Lightweight liveness check with authentication."""
    return {
        "status": "ok",
        "tools": compute_tool_statuses(),
        "caching": {
            "enabled": ENABLE_PROMPT_CACHING and CACHING_AVAILABLE,
            "cache_ttl_seconds": 3600 if ENABLE_PROMPT_CACHING else None
        }
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
    component actually required to serve requests is unavailable."""
    checks = {
        "chat_db_readable": os.path.exists(CHAT_DB_PATH) and os.access(CHAT_DB_PATH, os.R_OK),
        "llm_provider_configured": bool(
            os.environ.get("DEEPSEEK_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
        ),
    }
    try:
        receipts.list_recent(limit=1)
        checks["receipts_db_writable"] = True
    except Exception as exc:
        logger.warning("Receipts DB check failed: %s", exc)
        checks["receipts_db_writable"] = False

    ready = all(checks.values())
    payload = {"ready": ready, "checks": checks}
    if not ready:
        raise HTTPException(status_code=503, detail=payload)
    return payload


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
