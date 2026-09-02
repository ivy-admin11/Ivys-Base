"""
Ivy Local Admin API Gateway v2.2 — Voice Assistant Edition

Architecture:
- Phase 1: Critical fixes (duplicates, auth, f-string bugs)
- Phase 2: Config consolidation (tool schemas, timeouts, feature flags)
- Phase 3: Gemini SDK refactor (use google.genai official SDK)
- Phase 4: Prompt caching for 80-90% token cost reduction ✅ IMPLEMENTED
- Phase 5: Voice assistant with session management and cache optimization ✅ IMPLEMENTED

All hardcoded values are extracted to config.py for centralized tuning.
Environment-specific secrets go in .env (see .env.example).

Security:
- All FastAPI endpoints require X-API-Key header matching ADMIN_SECRET
- Database reads use SQLite read-only mode to prevent accidental mutations
- iMessage poller validates sender against the IVY_FAVORITES_FILE allowlist

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
import queue
import shutil as _shutil
import socket
import sys
import time
import sqlite3
import threading
import logging
import json
import re
import requests
import subprocess
import atexit
from dataclasses import dataclass
from google import genai
from google.genai import types as genai_types
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any, Callable
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel

# Import centralized configuration
from config import (
    POLLING_INTERVAL,
    IMESSAGE_FETCH_BATCH_SIZE,
    IMESSAGE_QUEUE_MAXSIZE,
    IMESSAGE_SLOW_QUEUE_MAXSIZE,
    IMESSAGE_DEBOUNCE_SECONDS,
    IMESSAGE_QUEUE_PUT_TIMEOUT_SECONDS,
    IMESSAGE_STALE_QUEUE_SECONDS,
    IMESSAGE_WORKER_JOIN_TIMEOUT_SECONDS,
    IMESSAGE_SLOW_ACK_SECONDS,
    DB_TIMEOUT,
    DB_RETRY_ATTEMPTS,
    DB_RETRY_BACKOFF,
    CHAT_DB_PATH,
    EXTERNAL_API_TIMEOUT,
    IMESSAGE_SEND_TIMEOUT_SECONDS,
    APPLE_CALENDAR_TIMEOUT_SECONDS,
    APPLE_REMINDERS_TIMEOUT_SECONDS,
    STATUS_COMMAND_TIMEOUT_SECONDS,
    ENABLE_IMESSAGE_POLLER,
    ENABLE_CALENDAR_INTEGRATION,
    ENABLE_REMINDERS_INTEGRATION,
    ENABLE_READWISE_INTEGRATION,
    PLAYWRIGHT_ENABLED,
    ADMIN_SECRET,
    IVY_FAVORITES_FILE,
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
from ivy_core import attachment_verify
from ivy_core import receipts
from ivy_core import outbox as _outbox
from ivy_core.imessage_state import (
    InboxStateStore,
    InboundMessage,
    runtime_metrics as _imessage_metrics,
)
from ivy_core.messaging import send_imessage_attachment
from ivy_core.report_fallback import build_attachment_failure_notice
from filelock import FileLock, Timeout as FileLockTimeout
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

_GEMINI_MODEL = "gemini-2.5-flash"
_gemini_client = None


def _get_gemini_client():
    """Lazily initialize Gemini client using API-key auth."""
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY not configured in environment")

    # Keep ADC env noise from interfering with explicit API-key auth.
    saved = {
        k: os.environ.pop(k)
        for k in ("GOOGLE_APPLICATION_CREDENTIALS", "GCLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT")
        if k in os.environ
    }
    try:
        _gemini_client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(
                timeout=EXTERNAL_API_TIMEOUT * 1000,
            ),
        )
    finally:
        os.environ.update(saved)
    return _gemini_client


def _make_gemini_config(
    system_instruction: Optional[str] = None,
    *,
    include_tools: bool = True,
) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    if include_tools:
        config["tools"] = [{"function_declarations": GEMINI_TOOL_DECLARATIONS}]
    if system_instruction:
        config["system_instruction"] = system_instruction
    return config


def _part_text(part: Any) -> str:
    if isinstance(part, dict):
        return part.get("text") or ""
    return getattr(part, "text", "") or ""


def _part_function_call(part: Any) -> Optional[Dict[str, Any]]:
    # Handles both SDK objects and plain dict parts used in tests/follow-up payloads.
    raw = part.get("function_call") if isinstance(part, dict) else getattr(part, "function_call", None)
    if not raw:
        return None
    if isinstance(raw, dict):
        return {"name": raw.get("name"), "args": raw.get("args") or {}}
    return {"name": getattr(raw, "name", None), "args": getattr(raw, "args", {}) or {}}


def _extract_gemini_parts(response: Any) -> List[Any]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    return getattr(content, "parts", None) or []


def _extract_gemini_reply_and_tool_calls(response: Any) -> tuple[str, List[Dict[str, Any]], List[Any]]:
    parts = _extract_gemini_parts(response)
    text_reply = "".join(_part_text(part) for part in parts)
    tool_calls = []
    for part in parts:
        call = _part_function_call(part)
        if call and call.get("name"):
            tool_calls.append(call)
    return text_reply, tool_calls, parts


def _serialize_parts(parts: List[Any]) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for part in parts:
        text = _part_text(part)
        if text:
            serialized.append({"text": text})
        call = _part_function_call(part)
        if call and call.get("name"):
            serialized.append(
                {"function_call": {"name": call["name"], "args": call.get("args") or {}}}
            )
    return serialized


def _gemini_generate_content(
    *,
    contents: Any,
    system_instruction: Optional[str] = None,
    include_tools: bool = True,
):
    client = _get_gemini_client()
    return client.models.generate_content(
        model=_GEMINI_MODEL,
        contents=contents,
        config=_make_gemini_config(system_instruction, include_tools=include_tools),
    )

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
        "name": "openai",
        "description": "First failover AI conversation engine via the OpenAI API.",
        "required_env": [["OPENAI_API_KEY"]],
        "role": "failover",
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


# ---------------------------------------------------------------------------
# iMessage runtime state
# ---------------------------------------------------------------------------

_IMESSAGE_STOP_EVENT = threading.Event()
_IMESSAGE_WORKER_THREAD: Optional[threading.Thread] = None
_PROVIDER_PROBE_STOP_EVENT = threading.Event()
_PROVIDER_PROBE_THREAD: Optional[threading.Thread] = None
_IMESSAGE_INBOX_QUEUE: queue.Queue[InboundMessage] = queue.Queue(
    maxsize=IMESSAGE_QUEUE_MAXSIZE
)
_IMESSAGE_SLOW_QUEUE: queue.Queue["ProcessingUnit"] = queue.Queue(
    maxsize=IMESSAGE_SLOW_QUEUE_MAXSIZE
)
_IMESSAGE_STATE = InboxStateStore()
_IMESSAGE_LATEST_BY_SENDER: Dict[str, int] = {}
_IMESSAGE_LATEST_LOCK = threading.RLock()
_IMESSAGE_SEND_LOCK = threading.RLock()
_IMESSAGE_TOOL_CONTEXT = threading.local()

# Per-sender short-term memory so follow-ups are answered in context. Without
# it every inbound text is a standalone prompt: "Yes, I want the full recipe"
# arrived with no trace of the recipe Ivy had just offered, and DeepSeek —
# which still has run_job in its tool schema — read it as a fresh command and
# launched the Familia Meal Planner instead of answering.
CONVERSATION_MAX_MESSAGES = int(os.environ.get("CONVERSATION_MAX_MESSAGES", "8"))
CONVERSATION_TTL_SECONDS = int(os.environ.get("CONVERSATION_TTL_SECONDS", "2700"))
_CONVERSATIONS: Dict[str, List[Dict[str, Any]]] = {}
_CONVERSATION_LOCK = threading.RLock()
_IMESSAGE_POLLER_LOCK_PATH = Path(__file__).resolve().parent / "logs" / "imessage-poller.lock"

_CALENDAR_RUNNER = AppleScriptRunner(timeout=APPLE_CALENDAR_TIMEOUT_SECONDS)
_REMINDERS_RUNNER = AppleScriptRunner(timeout=APPLE_REMINDERS_TIMEOUT_SECONDS)
_IMESSAGE_RUNNER = AppleScriptRunner(timeout=IMESSAGE_SEND_TIMEOUT_SECONDS)


@dataclass(frozen=True)
class ProcessingUnit:
    """One authorized, in-memory unit of iMessage work.

    Sender identifiers and message text stay in memory only.  The durable
    inbox journal stores ROWIDs and sanitized state categories, never content.
    """

    messages: tuple[InboundMessage, ...]
    category: str

    @property
    def message_ids(self) -> tuple[int, ...]:
        return tuple(message.message_id for message in self.messages)

    @property
    def newest_message_id(self) -> int:
        return max(self.message_ids)

    @property
    def sender(self) -> str:
        return self.messages[0].sender

    @property
    def text(self) -> str:
        return "\n".join(message.text for message in self.messages)

    @property
    def collected_monotonic(self) -> float:
        return min(message.collected_monotonic for message in self.messages)

# ============================================================================
# PERFORMANCE OPTIMIZATIONS (Thread-safe, Fail-Closed)
# ============================================================================

# ---- Favorites Cache State Model ----
# State tracking for immutable, thread-safe favorites.json caching.
# Distinguishes: uninitialized, valid (nonempty/empty), missing, unreadable,
# malformed JSON, invalid schema.

_FAVORITES_CACHE_LOCK = threading.RLock()

# Cache state: None (uninitialized), True (valid), False (invalid/unreadable)
_FAVORITES_CACHE_STATE = None

# Immutable contact list (frozenset or tuple) — None if uninitialized/invalid
_FAVORITES_CACHE_CONTACTS = None

# File metadata for change detection
_FAVORITES_CACHE_MTIME_NS = None
_FAVORITES_CACHE_SIZE = None

# Warning suppression — track which invalid paths have been warned about
_FAVORITES_WARNED_INVALID_PATHS = set()
_DEFAULT_FAVORITES_FILE = str(Path(__file__).resolve().parent / "favorites.json")


def _warn_once_favorites(message: str, identity: object) -> None:
    """Log a sanitized warning once per unchanged file-state identity."""
    if identity not in _FAVORITES_WARNED_INVALID_PATHS:
        logger.warning(message)
        _FAVORITES_WARNED_INVALID_PATHS.add(identity)


def _get_project_root() -> Path:
    """Get the project root directory independent of current working directory."""
    return Path(__file__).resolve().parent


def _get_favorites_path() -> Path:
    """Return the configured allowlist path without depending on the CWD.

    The default follows the application directory; keeping that lookup dynamic
    also preserves test isolation when the project-root helper is patched.
    """
    if os.path.abspath(IVY_FAVORITES_FILE) == os.path.abspath(_DEFAULT_FAVORITES_FILE):
        return _get_project_root() / "favorites.json"
    return Path(IVY_FAVORITES_FILE).expanduser().resolve()


# ---- SQLite Connection Lifecycle ----
# Persistent, thread-safe read-only connection to the Apple Messages database.
# Health-checked on first use and reconnected on failure.

_CHAT_DB_CONN = None
_CHAT_DB_LOCK = threading.RLock()
_CHAT_DB_SHUTDOWN_REGISTERED = False


def _create_chat_db_connection() -> Optional[sqlite3.Connection]:
    """Create a new read-only SQLite connection to CHAT_DB_PATH.
    
    Returns:
        sqlite3.Connection or None if connection fails.
    """
    try:
        # Build read-only URI from CHAT_DB_PATH
        conn = sqlite3.connect(
            f"file:{CHAT_DB_PATH}?mode=ro&uri=true",
            uri=True,
            timeout=DB_TIMEOUT,
            check_same_thread=False,  # Safe because all access serialized with _CHAT_DB_LOCK
        )
        return conn
    except Exception as exc:
        logger.error(
            "Failed to create chat.db connection error=%s", type(exc).__name__
        )
        return None


def _health_check_connection(conn: sqlite3.Connection) -> bool:
    """Test the connection with SELECT 1; explicitly close cursor.
    
    Returns:
        True if connection is healthy, False otherwise.
    """
    if not conn:
        return False
    
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        return True
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return False
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass


def _close_chat_db_locked() -> None:
    """Close the persistent connection while holding _CHAT_DB_LOCK.
    
    Must be called only when _CHAT_DB_LOCK is already held.
    Clears _CHAT_DB_CONN to None.
    """
    global _CHAT_DB_CONN
    
    if _CHAT_DB_CONN:
        try:
            _CHAT_DB_CONN.close()
        except Exception as exc:
            logger.debug(
                "Error closing chat.db connection error=%s", type(exc).__name__
            )
    
    _CHAT_DB_CONN = None


def close_chat_db() -> None:
    """Public idempotent shutdown function for the persistent connection.
    
    Called at program exit via atexit.
    """
    with _CHAT_DB_LOCK:
        _close_chat_db_locked()


def _register_chat_db_atexit() -> None:
    """Register atexit cleanup once (idempotent)."""
    global _CHAT_DB_SHUTDOWN_REGISTERED
    
    if not _CHAT_DB_SHUTDOWN_REGISTERED:
        atexit.register(close_chat_db)
        _CHAT_DB_SHUTDOWN_REGISTERED = True


def init_chat_db() -> Optional[sqlite3.Connection]:
    """Initialize or return the persistent chat.db connection.
    
    On first call:
        - Creates a new connection
        - Registers atexit cleanup
        - Health-checks the connection
    
    On subsequent calls:
        - Health-checks the existing connection
        - If stale, closes it and creates a new one
    
    Returns:
        sqlite3.Connection (healthy) or None if all attempts fail.
    """
    global _CHAT_DB_CONN
    
    with _CHAT_DB_LOCK:
        # Register atexit cleanup once
        _register_chat_db_atexit()
        
        # If we have a connection, health-check it
        if _CHAT_DB_CONN:
            if _health_check_connection(_CHAT_DB_CONN):
                return _CHAT_DB_CONN
            
            # Stale connection; close and clear
            _close_chat_db_locked()
        
        # Create a new connection
        _CHAT_DB_CONN = _create_chat_db_connection()
        if _CHAT_DB_CONN and _health_check_connection(_CHAT_DB_CONN):
            logger.info("✅ Persistent chat.db connection established")
            return _CHAT_DB_CONN
        
        # Failed to create or health-check
        if _CHAT_DB_CONN:
            _close_chat_db_locked()
        
        logger.error("Failed to initialize chat.db connection")
        return None


def _db_retry_backoff(attempt: int) -> float:
    """Return the exponential backoff delay for chat.db retries."""
    return DB_RETRY_BACKOFF * (2 ** attempt)


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
                "model": "deepseek-chat",
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
    except requests.exceptions.ConnectionError:
        return {
            "configured": True, "authenticated": False, "reachable": False,
            "role": "primary", "status": "unreachable", "reason": "Connection failed",
        }
    except Exception as exc:
        return {
            "configured": True, "authenticated": False, "reachable": True,
            "role": "primary", "status": "error", "reason": type(exc).__name__,
        }


def _probe_openai() -> Dict[str, Any]:
    """Make a minimal bounded OpenAI call and return sanitized health fields."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {
            "configured": False,
            "authenticated": False,
            "reachable": False,
            "role": "failover",
            "status": "unconfigured",
            "reason": "OPENAI_API_KEY not set",
        }
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
            timeout=8,
        )
        if response.status_code == 200:
            return {
                "configured": True,
                "authenticated": True,
                "reachable": True,
                "role": "failover",
                "status": "ready",
                "reason": None,
            }
        if response.status_code in (401, 403):
            return {
                "configured": True,
                "authenticated": False,
                "reachable": True,
                "role": "failover",
                "status": "degraded",
                "reason": f"Provider returned HTTP {response.status_code}",
            }
        return {
            "configured": True,
            "authenticated": False,
            "reachable": True,
            "role": "failover",
            "status": "error",
            "reason": f"Unexpected HTTP {response.status_code}",
        }
    except requests.exceptions.Timeout:
        return {
            "configured": True,
            "authenticated": False,
            "reachable": False,
            "role": "failover",
            "status": "unreachable",
            "reason": "Request timed out",
        }
    except requests.exceptions.ConnectionError:
        return {
            "configured": True,
            "authenticated": False,
            "reachable": False,
            "role": "failover",
            "status": "unreachable",
            "reason": "Connection failed",
        }
    except Exception as exc:
        return {
            "configured": True,
            "authenticated": False,
            "reachable": True,
            "role": "failover",
            "status": "error",
            "reason": type(exc).__name__,
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
        client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=8000),
        )
        client.models.generate_content(
            model=_GEMINI_MODEL,
            contents="hi",
            config={"max_output_tokens": 1},
        )
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
            "role": "failover", "status": "error", "reason": type(exc).__name__,
        }


def probe_providers(*, force: bool = False) -> Dict[str, Any]:
    """Return per-provider auth status. Cached for _PROVIDER_PROBE_TTL seconds
    so repeated /health polls don't hammer external APIs.

    Pass force=True to bypass the cache (e.g., after a key rotation)."""
    import time as _time
    now = _time.monotonic()
    with _PROVIDER_PROBE_LOCK:
        cached_at = _PROVIDER_PROBE_CACHE.get("_ts", 0.0)
        if not force and (now - cached_at) < _PROVIDER_PROBE_TTL:
            return {k: v for k, v in _PROVIDER_PROBE_CACHE.items() if k != "_ts"}

    result: Dict[str, Any] = {
        "deepseek": _probe_deepseek(),
        "openai": _probe_openai(),
        "gemini": _probe_gemini(),
        "_ts": now,
    }
    with _PROVIDER_PROBE_LOCK:
        _PROVIDER_PROBE_CACHE.clear()
        _PROVIDER_PROBE_CACHE.update(result)
        return {k: v for k, v in result.items() if k != "_ts"}


def _cached_provider_snapshot() -> Dict[str, Any]:
    """Return cached probe state without performing network I/O."""
    with _PROVIDER_PROBE_LOCK:
        return {
            key: dict(value)
            for key, value in _PROVIDER_PROBE_CACHE.items()
            if key != "_ts" and isinstance(value, dict)
        }


def _provider_probe_worker(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            probe_providers(force=True)
        except Exception as exc:
            logger.warning("Provider probe cycle failed error=%s", type(exc).__name__)
        stop_event.wait(_PROVIDER_PROBE_TTL)



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
    """Print banner, initialize persistent connections, start iMessage poller."""
    global _IMESSAGE_WORKER_THREAD, _PROVIDER_PROBE_THREAD

    print_startup_banner()
    
    # Initialize persistent chat.db connection (registers atexit cleanup)
    init_chat_db()
    
    # Initialize voice session manager if available
    if VOICE_ASSISTANT_AVAILABLE:
        logger.info("Voice session manager initialized and ready.")

    _PROVIDER_PROBE_STOP_EVENT.clear()
    _PROVIDER_PROBE_THREAD = threading.Thread(
        target=_provider_probe_worker,
        args=(_PROVIDER_PROBE_STOP_EVENT,),
        name="ivy-provider-health-probe",
        daemon=True,
    )
    _PROVIDER_PROBE_THREAD.start()
    
    # Start iMessage poller if enabled
    if ENABLE_IMESSAGE_POLLER:
        _IMESSAGE_STOP_EVENT.clear()
        _IMESSAGE_WORKER_THREAD = threading.Thread(
            target=background_imessage_worker,
            name="ivy-imessage-collector",
            daemon=True,
        )
        _IMESSAGE_WORKER_THREAD.start()
        logger.info("Background iMessage polling thread started.")
    else:
        _imessage_metrics.set_thread_state("collector", False)
    
    try:
        yield
    finally:
        _IMESSAGE_STOP_EVENT.set()
        _PROVIDER_PROBE_STOP_EVENT.set()
        if _IMESSAGE_WORKER_THREAD is not None:
            _IMESSAGE_WORKER_THREAD.join(timeout=IMESSAGE_WORKER_JOIN_TIMEOUT_SECONDS)
            if _IMESSAGE_WORKER_THREAD.is_alive():
                logger.warning("iMessage collector did not stop within the shutdown grace period")
            _IMESSAGE_WORKER_THREAD = None
        if _PROVIDER_PROBE_THREAD is not None:
            _PROVIDER_PROBE_THREAD.join(timeout=IMESSAGE_WORKER_JOIN_TIMEOUT_SECONDS)
            _PROVIDER_PROBE_THREAD = None
        close_chat_db()
        logger.info("Gateway shutdown complete.")


app = FastAPI(
    title="Ivy Local Admin API Gateway v2.2 — Voice Assistant",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

PROCESS_STARTED_AT = datetime.now()
PROJECT_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
FAVORITES_FILENAME = "favorites.json"

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
    except Exception as exc:
        logger.warning("Readwise request failed error=%s", type(exc).__name__)
        return "❌ Readwise is temporarily unavailable."


# ============================================================================
# APPLE CALENDAR INTEGRATION
# ============================================================================


def _record_applescript_result(runner: AppleScriptRunner, result: str) -> bool:
    """Record safe AppleEvent telemetry and return whether the call succeeded."""
    if runner.last_error_category == "timeout":
        _imessage_metrics.record_apple_event_timeout()
    return not result.upper().startswith("ERROR")


def check_apple_calendar(timeframe: str) -> str:
    """Scan local Mac Hilla Calendar for upcoming events."""
    raw_output = _CALENDAR_RUNNER.fetch_calendar_events("Hilla").strip()

    if not _record_applescript_result(_CALENDAR_RUNNER, raw_output):
        return "❌ Calendar is temporarily unavailable. Please try again."
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
    raw_output = _REMINDERS_RUNNER.fetch_reminders(list_name).strip()
    if not _record_applescript_result(_REMINDERS_RUNNER, raw_output):
        return "❌ Reminders is temporarily unavailable. Please try again."
    return raw_output or "No active reminders found."


def add_apple_reminder(title: str, list_name: str = "Household") -> str:
    """Add a task to Apple Reminders."""
    # Auto-categorize based on keywords
    if any(word in list_name.lower() for word in ["meal", "food", "dinner", "recipe", "taco"]):
        list_name = "Meal Plan"
    elif any(word in list_name.lower() for word in ["house", "chore", "clean", "task"]):
        list_name = "Household"

    raw_output = _REMINDERS_RUNNER.add_reminder(list_name, title).strip()

    if _record_applescript_result(_REMINDERS_RUNNER, raw_output) and raw_output == "SUCCESS":
        return f"✅ Added to your '{list_name}' list: {title}"
    return "❌ Reminders is temporarily unavailable. Please try again."


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

    dispatch = job_runner.run_job_detailed(job_name, force=True)
    status, message = dispatch.status, dispatch.message
    reference = (
        f" (execution {dispatch.execution_id[:8]})"
        if dispatch.execution_id
        else ""
    )

    if status == JobStatus.SUCCESS:
        return f"{message}{reference}"
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
_MUTATING_TOOL_NAMES = frozenset({"add_apple_reminder", "run_job"})


def _execute_tool_call(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Execute a registered tool by name. Both the Gemini and DeepSeek paths
    call through here, so neither can dispatch to anything but a real,
    registered tool, and DeepSeek gets the same run_job access Gemini has."""
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Error: Function {tool_name} is undefined."
    if tool_name in _MUTATING_TOOL_NAMES:
        # The slow-worker thread uses this bit to avoid suppressing a reply
        # after an irreversible action may already have started.
        _IMESSAGE_TOOL_CONTEXT.mutation_started = True
    try:
        return handler(**tool_args)
    except Exception as exec_err:
        logger.warning(
            "Tool execution failed tool=%s error=%s",
            tool_name,
            type(exec_err).__name__,
        )
        return f"Error: {tool_name} could not complete the request."


def _execute_native_tool_calls(tool_calls: Any, provider: str) -> str:
    """Run every valid native tool call in provider order.

    Provider APIs may return several independent tool calls in one assistant
    message.  Silently executing only the first one reports a request as done
    while dropping later reminder/job actions, so execute each registered call
    deterministically and return their bounded user-facing outcomes.
    """
    if not isinstance(tool_calls, list) or not tool_calls:
        return ""

    outcomes: List[str] = []
    for index, call in enumerate(tool_calls, start=1):
        if not isinstance(call, dict):
            outcomes.append(f"Error: {provider} returned an invalid tool call.")
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            outcomes.append(f"Error: {provider} returned an invalid tool call.")
            continue
        func_name = function.get("name")
        if not isinstance(func_name, str) or not func_name:
            outcomes.append(f"Error: {provider} returned an unnamed tool call.")
            continue
        raw_arguments = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError:
            outcomes.append(f"Error: {func_name} received invalid arguments.")
            continue
        if not isinstance(args, dict):
            outcomes.append(f"Error: {func_name} received invalid arguments.")
            continue
        logger.info("%s triggered native tool=%s call=%d", provider, func_name, index)
        outcomes.append(_execute_tool_call(func_name, args))
    return "\n".join(outcome for outcome in outcomes if outcome)


# ============================================================================
# IMESSAGE ROUTING
# ============================================================================


def run_local_applescript_send(target: str, body: str) -> str:
    """Send iMessage through the fixed argv AppleScript path.

    Messages.app automation is serialized and bounded.  Dynamic recipient and
    body values are process arguments, never interpolated into AppleScript.
    """
    with _IMESSAGE_SEND_LOCK:
        result = _IMESSAGE_RUNNER.send_imessage_argv(target, body).strip()
    _record_applescript_result(_IMESSAGE_RUNNER, result)
    return result


# ============================================================================
# DEEPSEEK FAILOVER ENGINE
# ============================================================================


def _provider_messages(
    system_instruction: str,
    text_content: str,
    history: Optional[List[Dict[str, str]]],
) -> List[Dict[str, str]]:
    """Chat-completions message list: system, prior turns, then the current
    message. Only well-formed user/assistant turns are forwarded."""
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_instruction}]
    for turn in history or []:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": text_content})
    return messages


def execute_deepseek_call(
    text_content: str,
    system_instruction: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Execute call via DeepSeek API with tool calling support.

    ``history`` is the sender's recent turns (see conversation_history), sent
    ahead of the current message so a follow-up like "yes, the full recipe"
    resolves against what Ivy just offered rather than reading as a command.
    
    Raises:
        ValueError: If DEEPSEEK_API_KEY is not configured
        ProviderHTTPError: If the API returns a non-200 status code
        Exception: For other runtime errors
    
    Returns:
        The response text from DeepSeek, or a tool execution result.
    """
    from ivy_core.pipeline_status import ProviderHTTPError
    
    active_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not active_key:
        raise ValueError("DeepSeek is not configured. Set DEEPSEEK_API_KEY environment variable.")

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": "Bearer " + active_key,
        "Content-Type": "application/json"
    }

    payload = {
        "model": "deepseek-chat",
        "messages": _provider_messages(system_instruction, text_content, history),
        "tools": DEEPSEEK_TOOL_SCHEMA,
        "temperature": 0.1,
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=EXTERNAL_API_TIMEOUT)
        
        if response.status_code != 200:
            logger.error(
                "DeepSeek API returned HTTP %s",
                response.status_code,
            )
            raise ProviderHTTPError(
                provider="deepseek",
                status_code=response.status_code,
                detail="provider request rejected",
            )

        res_data = response.json()
        message_node = res_data["choices"][0]["message"]

        # Check if DeepSeek triggered tool execution — dispatched through the
        # same TOOL_HANDLERS registry Gemini uses, so DeepSeek can execute
        # every registered tool (including run_job, which it previously
        # could request via its schema but never actually got dispatched).
        if "tool_calls" in message_node and message_node["tool_calls"]:
            return _execute_native_tool_calls(message_node["tool_calls"], "DeepSeek")

        return message_node.get("content", "").strip()
    except ProviderHTTPError:
        raise
    except requests.RequestException as exc:
        logger.error("DeepSeek request failed error=%s", type(exc).__name__)
        raise
    except Exception as exc:
        logger.error("DeepSeek execution failed error=%s", type(exc).__name__)
        raise


# ============================================================================
# OPENAI FALLBACK ENGINE (reached when DeepSeek is unavailable)
# ============================================================================


def execute_openai_call(
    text_content: str,
    system_instruction: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Execute call via OpenAI API with tool calling support.

    ``history`` is the sender's recent turns (see conversation_history), sent
    ahead of the current message so a follow-up like "yes, the full recipe"
    resolves against what Ivy just offered rather than reading as a command.
    
    Raises:
       ValueError: If OPENAI_API_KEY is not configured
       ProviderHTTPError: If the API returns a non-200 status code
       Exception: For other runtime errors
    
    Returns:
       The response text from OpenAI, or a tool execution result.
    """
    from ivy_core.pipeline_status import ProviderHTTPError
    
    active_key = os.environ.get("OPENAI_API_KEY", "")
    if not active_key:
       raise ValueError("OpenAI is not configured. Set OPENAI_API_KEY environment variable.")

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": "Bearer " + active_key,
        "Content-Type": "application/json"
    }

    payload = {
       "model": "gpt-4o-mini",
       "messages": _provider_messages(system_instruction, text_content, history),
       "tools": DEEPSEEK_TOOL_SCHEMA,  # OpenAI uses same format as DeepSeek
       "temperature": 0.1,
       "max_tokens": 2000,
    }

    try:
       logger.info("OpenAI API request started")
       response = requests.post(url, json=payload, headers=headers, timeout=EXTERNAL_API_TIMEOUT)
        
       if response.status_code != 200:
           logger.error("OpenAI API returned HTTP %d", response.status_code)
           raise ProviderHTTPError(
               provider="openai",
               status_code=response.status_code,
               detail="provider request rejected",
           )
        
       response_json = response.json()
        
       # Check if tool was called
       choice = response_json.get("choices", [{}])[0]
       message = choice.get("message", {})
       tool_calls = message.get("tool_calls", [])
        
       if tool_calls:
           logger.info("OpenAI native tool execution triggered count=%d", len(tool_calls))
           return _execute_native_tool_calls(tool_calls, "OpenAI")
        
       # Return text response if no tool was called
       text_response = message.get("content", "")
       return text_response if text_response else "No response from OpenAI."
        
    except Exception as exc:
       logger.error("OpenAI execution failed error=%s", type(exc).__name__)
       raise


# ============================================================================
# GEMINI BACKUP ENGINE (only reached when DeepSeek and OpenAI are unavailable)
# ============================================================================


def _gemini_backup_reply(text: str, history: Optional[List[Dict[str, str]]] = None) -> Optional[str]:
    """Gemini backup: prompt-cached generate_content call with real tool
    execution and a real follow-up round-trip. Raises on provider failure
    (caller treats that as "no backup available" and gives up); returns None
    if Gemini responded but had nothing usable to say.

    Recent turns are folded into the user message as plain text, since the
    cached-request helper takes a single user string.
    """
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise ValueError("GEMINI_API_KEY not configured in environment")

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
        if not messages:
            logger.warning("Caching failed, falling back to non-cached request")
            messages = [{"role": "user", "parts": [{"text": text}]}]
            use_caching = False
    else:
        messages = [{"role": "user", "parts": [{"text": text}]}]

    # ⚠️ IMPORTANT: When using cached messages, don't pass system_instruction again
    # The cache_manager already includes it in the message stream
    if use_caching:
        response = _gemini_generate_content(
            contents=messages,
        )
    else:
        response = _gemini_generate_content(
            contents=messages,
            system_instruction=GEMINI_SYSTEM_INSTRUCTION,
        )

    # 💾 LOG CACHE METRICS
    if ENABLE_CACHE_METRICS_LOGGING and CACHING_AVAILABLE:
        cache_manager.log_cache_efficiency(
            response, endpoint="background_imessage_worker", model="gemini-2.5-flash"
        )

    text_reply, tool_calls, parts = _extract_gemini_reply_and_tool_calls(response)

    if not tool_calls:
        return text_reply.strip() or None

    logger.info("🛠️ Gemini returned %d tool operations", len(tool_calls))
    tool_results = []
    for call in tool_calls:
        tool_name = call["name"]
        tool_args = dict(call.get("args") or {})
        # Enforce Household list for reminders
        if tool_name in ["add_apple_reminder", "fetch_apple_reminders"]:
            tool_args["list_name"] = "Household"
        logger.info("Gemini executing tool=%s", tool_name)
        tool_result = _execute_tool_call(tool_name, tool_args)
        logger.info("Gemini tool completed tool=%s", tool_name)
        tool_results.append((tool_name, tool_result))

    # Follow-up call with the *real* tool results (previously always sent
    # back an empty {} regardless of what the tool actually returned).
    follow_up_response = _gemini_generate_content(
        contents=[
            *messages,
            {"role": "model", "parts": _serialize_parts(parts)},
            {
                "role": "function",
                "parts": [
                    {"function_response": {"name": name, "response": {"result": result}}}
                    for name, result in tool_results
                ],
            },
        ],
        system_instruction=None if use_caching else GEMINI_SYSTEM_INSTRUCTION,
        include_tools=False,
    )
    follow_up_text, _, _ = _extract_gemini_reply_and_tool_calls(follow_up_response)
    return follow_up_text.strip() or None


def query_llm_with_tools(prompt_text: str) -> str:
    """One-shot DeepSeek/OpenAI/Gemini query with real tool execution.

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
        logger.error("CLI query DeepSeek failed error=%s", type(exc).__name__)
        reply = None

    if not reply:
        try:
            reply = execute_openai_call(
                prompt_text,
                DEEPSEEK_SYSTEM_INSTRUCTION_TEMPLATE.format(
                    current_date_str=datetime.now().strftime("%A, %B %d, %Y")
                ),
            )
        except Exception as exc:
            logger.error("CLI query OpenAI failed error=%s", type(exc).__name__)
            reply = None

    if not reply:
        try:
            reply = _gemini_backup_reply(prompt_text)
        except Exception as exc:
            logger.error("CLI query Gemini failed error=%s", type(exc).__name__)
            reply = None

    return reply or "No response."


# ============================================================================
# DATABASE OPERATIONS: Safe SQLite Read-Only Access
# ============================================================================

def load_favorites_cached() -> frozenset:
    """Load favorites.json once, cache in memory. Reload only if file changes.
    
    Fail-closed semantics: Returns immutable empty frozenset on any error.
    
    Cache state model:
    - Uninitialized: _FAVORITES_CACHE_STATE is None
    - Valid (any size): _FAVORITES_CACHE_STATE is True, contacts immutable
    - Invalid: _FAVORITES_CACHE_STATE is False, contacts empty
    
    Changes trigger reload:
    - File stat.st_mtime_ns differs
    - File stat.st_size differs
    - File deleted
    - JSON parsing fails
    - Root is not a list
    - Entry is not a string
    
    🚀 Performance: Eliminates disk I/O on 99% of polls (5-10ms saved per poll)
    """
    global _FAVORITES_CACHE_STATE, _FAVORITES_CACHE_CONTACTS
    global _FAVORITES_CACHE_MTIME_NS, _FAVORITES_CACHE_SIZE
    
    favorites_path = _get_favorites_path()
    
    with _FAVORITES_CACHE_LOCK:
        # Try to stat the file
        try:
            stat = favorites_path.stat()
            current_mtime_ns = stat.st_mtime_ns
            current_size = stat.st_size
        except (OSError, FileNotFoundError):
            # File doesn't exist or is unreadable
            if (
                _FAVORITES_CACHE_STATE is False
                and _FAVORITES_CACHE_MTIME_NS is None
                and _FAVORITES_CACHE_SIZE is None
            ):
                # Already marked invalid; return cached empty set without re-warning
                return frozenset()
            
            # First time seeing this error; warn once
            _warn_once_favorites(
                "favorites allowlist is missing or unreadable; external senders are blocked",
                ("missing", str(favorites_path)),
            )
            _FAVORITES_CACHE_STATE = False
            _FAVORITES_CACHE_CONTACTS = frozenset()
            _FAVORITES_CACHE_MTIME_NS = None
            _FAVORITES_CACHE_SIZE = None
            return frozenset()
        
        # File exists. Check if it's changed.
        if (
            _FAVORITES_CACHE_STATE is not None
            and _FAVORITES_CACHE_MTIME_NS == current_mtime_ns
            and _FAVORITES_CACHE_SIZE == current_size
        ):
            # File unchanged since the last valid OR invalid load.
            return _FAVORITES_CACHE_CONTACTS or frozenset()
        
        # File is new, modified, or first load attempt
        try:
            with open(favorites_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Validate: root must be a list
            if not isinstance(data, list):
                _warn_once_favorites(
                    "favorites allowlist has an invalid root type; external senders are blocked",
                    ("schema-root", current_mtime_ns, current_size),
                )
                _FAVORITES_CACHE_STATE = False
                _FAVORITES_CACHE_CONTACTS = frozenset()
                _FAVORITES_CACHE_MTIME_NS = current_mtime_ns
                _FAVORITES_CACHE_SIZE = current_size
                return frozenset()
            
            # Validate: every entry must be a string
            for i, entry in enumerate(data):
                if not isinstance(entry, str):
                    _warn_once_favorites(
                        "favorites allowlist contains a non-string entry; external senders are blocked",
                        ("schema-entry", current_mtime_ns, current_size),
                    )
                    _FAVORITES_CACHE_STATE = False
                    _FAVORITES_CACHE_CONTACTS = frozenset()
                    _FAVORITES_CACHE_MTIME_NS = current_mtime_ns
                    _FAVORITES_CACHE_SIZE = current_size
                    return frozenset()
            
            # Valid; store immutable contacts
            _FAVORITES_CACHE_CONTACTS = frozenset(data)
            _FAVORITES_CACHE_STATE = True
            _FAVORITES_CACHE_MTIME_NS = current_mtime_ns
            _FAVORITES_CACHE_SIZE = current_size
            
            logger.debug(
                "Reloaded favorites.json (%d contacts)",
                len(_FAVORITES_CACHE_CONTACTS)
            )
            return _FAVORITES_CACHE_CONTACTS
        
        except json.JSONDecodeError:
            _warn_once_favorites(
                "favorites allowlist contains malformed JSON; external senders are blocked",
                ("malformed", current_mtime_ns, current_size),
            )
            _FAVORITES_CACHE_STATE = False
            _FAVORITES_CACHE_CONTACTS = frozenset()
            _FAVORITES_CACHE_MTIME_NS = current_mtime_ns
            _FAVORITES_CACHE_SIZE = current_size
            return frozenset()
        
        except Exception:
            _warn_once_favorites(
                "favorites allowlist could not be read; external senders are blocked",
                ("unreadable", current_mtime_ns, current_size),
            )
            _FAVORITES_CACHE_STATE = False
            _FAVORITES_CACHE_CONTACTS = frozenset()
            _FAVORITES_CACHE_MTIME_NS = current_mtime_ns
            _FAVORITES_CACHE_SIZE = current_size
            return frozenset()


def _fetch_chat_rows_with_retry(
    sql: str,
    parameters: tuple[Any, ...],
    *,
    operation: str,
) -> Optional[List[tuple]]:
    """Execute one fixed read query with connection reset and bounded retry."""
    for attempt in range(DB_RETRY_ATTEMPTS):
        backoff = 0.0
        with _CHAT_DB_LOCK:
            conn = init_chat_db()
            if not conn:
                logger.warning(
                    "chat.db unavailable operation=%s attempt=%d/%d",
                    operation,
                    attempt + 1,
                    DB_RETRY_ATTEMPTS,
                )
                if attempt < DB_RETRY_ATTEMPTS - 1:
                    backoff = _db_retry_backoff(attempt)
            else:
                cursor = None
                try:
                    cursor = conn.cursor()
                    cursor.execute(sql, parameters)
                    if hasattr(cursor, "fetchall"):
                        return list(cursor.fetchall())
                    # Compatibility with small cursor doubles in older tests.
                    row = cursor.fetchone()
                    return [] if row is None else [row]
                except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                    logger.debug(
                        "chat.db read failed operation=%s attempt=%d/%d error=%s",
                        operation,
                        attempt + 1,
                        DB_RETRY_ATTEMPTS,
                        type(exc).__name__,
                    )
                    _close_chat_db_locked()
                    if attempt < DB_RETRY_ATTEMPTS - 1:
                        backoff = _db_retry_backoff(attempt)
                finally:
                    if cursor is not None:
                        try:
                            cursor.close()
                        except Exception:
                            pass

        # Never sleep while holding the shared connection lock.
        if backoff:
            time.sleep(backoff)

    logger.warning(
        "chat.db read exhausted retries operation=%s attempts=%d",
        operation,
        DB_RETRY_ATTEMPTS,
    )
    return None


def safe_fetch_new_messages(
    after_id: int,
    limit: int = IMESSAGE_FETCH_BATCH_SIZE,
) -> Optional[List[tuple]]:
    """Fetch an ordered, bounded batch of inbound Messages rows.

    ``[]`` means a successful idle poll. ``None`` means the database read
    failed after bounded reconnect attempts.  The distinction feeds readiness
    telemetry and avoids treating database failure as normal idleness.
    """
    try:
        bounded_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("message batch limit must be an integer") from exc
    if not 1 <= bounded_limit <= 100:
        raise ValueError("message batch limit must be between 1 and 100")

    return _fetch_chat_rows_with_retry(
        """
        SELECT m.ROWID, m.text, COALESCE(h.id, '')
        FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.ROWID > ? AND m.is_from_me = 0 AND m.text IS NOT NULL
        ORDER BY m.ROWID ASC LIMIT ?
        """,
        (max(0, int(after_id)), bounded_limit),
        operation="fetch_new_messages",
    )


def safe_fetch_messages_by_ids(message_ids: List[int]) -> Optional[List[tuple]]:
    """Rehydrate never-started durable queue entries after a restart."""
    normalized = sorted({max(0, int(value)) for value in message_ids})[:100]
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in normalized)
    return _fetch_chat_rows_with_retry(
        "SELECT m.ROWID, m.text, COALESCE(h.id, '') "
        "FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID "
        f"WHERE m.ROWID IN ({placeholders}) "  # nosec B608 - placeholders only
        "AND m.is_from_me = 0 AND m.text IS NOT NULL ORDER BY m.ROWID ASC",
        tuple(normalized),
        operation="rehydrate_messages",
    )


def safe_fetch_last_message(last_id: int) -> Optional[tuple]:
    """Backward-compatible one-row wrapper around the batch collector."""
    rows = safe_fetch_new_messages(last_id, limit=1)
    return rows[0] if rows else None


def get_last_message_id() -> Optional[int]:
    """Get the highest ROWID from the message table with retry logic.
    
    Returns 0 if the message table is empty (no error).
    Returns None only if connection fails after all retries.
    
    Behavior:
    - Retries up to DB_RETRY_ATTEMPTS on recoverable errors
    - Explicitly closes cursor in finally block
    - Never logs message data
    
    Returns:
        int (possibly 0 if table empty) or None if error after retries exhausted.
    """
    for attempt in range(DB_RETRY_ATTEMPTS):
        _backoff = 0
        with _CHAT_DB_LOCK:
            conn = init_chat_db()
            if not conn:
                logger.warning("Could not establish chat.db connection (attempt %d/%d)",
                             attempt + 1, DB_RETRY_ATTEMPTS)
                if attempt < DB_RETRY_ATTEMPTS - 1:
                    _backoff = DB_RETRY_BACKOFF * (2 ** attempt)
            else:
                cursor = None
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT MAX(ROWID) FROM message")
                    row = cursor.fetchone()
                    return row[0] if row and row[0] else 0
                
                except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                    logger.debug("Database query error (attempt %d/%d): %s",
                               attempt + 1, DB_RETRY_ATTEMPTS, type(e).__name__)
                    _close_chat_db_locked()
                    
                    if attempt < DB_RETRY_ATTEMPTS - 1:
                        _backoff = DB_RETRY_BACKOFF * (2 ** attempt)
                
                finally:
                    if cursor:
                        try:
                            cursor.close()
                        except Exception:
                            pass
        
        # Backoff sleep after releasing the lock
        if _backoff:
            time.sleep(_backoff)
    
    logger.warning("Failed to get last message ID after %d retries", DB_RETRY_ATTEMPTS)
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

_RESEND_PATTERN = re.compile(
    r"^\s*resend\s+"
    r"(picks|sharp\s+picks|happy\s+hour|meal\s+plan|[A-Z]{2}-\d{8}-\d{4}(?::\d{2})?)\s*$",
    re.IGNORECASE,
)

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
}


def handle_resend_command(text: str, sender: str) -> Optional[str]:
    """Deterministic RESEND handler — never calls an LLM.

    Returns a user-facing reply string if the text is a RESEND command,
    or None if the text is not a RESEND command (caller should proceed to LLM).

    Supported commands:
      RESEND PICKS / RESEND SHARP PICKS
      RESEND HAPPY HOUR
      RESEND MEAL PLAN
      RESEND <REPORT_ID>          e.g. RESEND SP-20260719-1430
    """
    m = _RESEND_PATTERN.match(text.strip())
    if not m:
        return None

    target = m.group(1).strip().lower()
    job_name: Optional[str] = None
    report_id: Optional[str] = None

    # Is this an explicit report ID (e.g. SP-20260719-1430)?
    if re.match(r"^[a-z]{2}-\d{8}-\d{4}", target, re.IGNORECASE):
        report_id = target.upper()
        job_name = _outbox.job_name_for_report_id(report_id)
        if not job_name:
            return "I don't recognise that report ID. Try RESEND PICKS, RESEND HAPPY HOUR, or RESEND MEAL PLAN."
    else:
        job_name = _RESEND_ALIASES.get(target)
        if not job_name:
            return "I didn't understand that resend command. Try RESEND PICKS, RESEND HAPPY HOUR, or RESEND MEAL PLAN."
        report_id = _outbox.find_newest_pending(job_name)
        if not report_id:
            return f"No pending {_RESEND_REPORT_NAMES.get(job_name, job_name)} report found to resend."

    meta = _outbox.load_report_meta(report_id)
    if not meta:
        return f"Report {report_id} metadata not found. It may have expired."

    pdf_path = _outbox.get_outbox_pdf_path(report_id)
    if not pdf_path:
        return f"The PDF for {report_id} is no longer available. You may need to run the job again."

    logger.info("RESEND: retrying preserved attachment report_id=%s", report_id)
    receipt = send_imessage_attachment(sender, str(pdf_path), report_id=report_id)
    attempts = (meta.get("send_attempts") or 0) + receipt.attempts
    _outbox.update_report_status(report_id, receipt.status, attempts=attempts)

    report_name = _RESEND_REPORT_NAMES.get(job_name, job_name)
    resend_cmd = _RESEND_COMMANDS.get(job_name, "RESEND")

    if receipt:
        return f"✅ {report_name} PDF resent (ref: {report_id})."

    # Attachment failed again.
    notice = build_attachment_failure_notice(
        report_name=report_name,
        report_id=report_id,
        resend_command=resend_cmd,
        retry_queued=False,
    )
    return notice


# ============================================================================
# JOB COMMAND ROUTING (deterministic, no LLM)
# ============================================================================

# Pattern for operational job commands (e.g., "run sharp picks", "send me happy hour")
_JOB_COMMAND_PATTERN = re.compile(
    r"^\s*(?:run|send|launch|start|dispatch)\s+"
    r"(?:me\s+)?(?:the\s+)?"
    r"([a-z0-9\s_\-]+?)\s*$",
    re.IGNORECASE,
)

_JOB_ALIASES: Dict[str, str] = {
    # Sharp Picks aliases
    "picks": "sharp_picks",
    "sharp picks": "sharp_picks",
    "sharppicks": "sharp_picks",
    "daily picks": "sharp_picks",
    "sports picks": "sharp_picks",
    "sports bettor": "sharp_picks",
    "sports_bettor": "sharp_picks",
    "my sports picks": "sharp_picks",
    "run picks": "sharp_picks",
    "send me sharp picks": "sharp_picks",
    
    # Happy Hour Scout aliases
    "happy hour": "happy_hour",
    "happy hour scout": "happy_hour",
    "happy_hour_scout": "happy_hour",
    "happy_hour scout": "happy_hour",
    "hh scout": "happy_hour",
    "scout": "happy_hour",
    
    # Familia Meal Planner aliases
    "meals": "familia_meal_planner",
    "meal plan": "familia_meal_planner",
    "meal planner": "familia_meal_planner",
    "meal planning": "familia_meal_planner",
    "planner": "familia_meal_planner",
    "weekly planner": "familia_meal_planner",
    "familia": "familia_meal_planner",
    "familia meal planner": "familia_meal_planner",
    "familia_meal_planner": "familia_meal_planner",
    "household meal plan": "familia_meal_planner",
    "household meal planner": "familia_meal_planner",
}


def _resolve_job_command(text: str) -> Optional[str]:
    """Resolve a deterministic job command without dispatching it."""
    normalized = " ".join(text.strip().lower().split())
    match = _JOB_COMMAND_PATTERN.match(normalized)
    if match:
        job_query = match.group(1).strip().lower()
    elif normalized in _JOB_ALIASES:
        # Preserve the short commands users already send, e.g. "sharp picks".
        job_query = normalized
    else:
        return None

    canonical = _JOB_ALIASES.get(job_query)
    if canonical:
        return canonical
    for alias, job_name in _JOB_ALIASES.items():
        if job_query in alias or alias in job_query:
            return job_name
    return None


def handle_job_command(text: str, sender: str) -> Optional[str]:
    """Deterministic JOB COMMAND handler — never calls an LLM.

    Returns a user-facing reply string if the text is a job command
    (e.g., "Run sharp picks"), or None if the text is not a job command
    (caller should proceed to LLM).

    Supported commands:
      RUN / SEND / LAUNCH / START / DISPATCH <JOB_NAME>
      where JOB_NAME can be:
        - picks, sharp picks, sports picks
        - happy hour, hh scout
        - meals, meal plan, planner, familia meal planner
    """
    canonical_job_name = _resolve_job_command(text)
    if not canonical_job_name:
        return None
    
    # Dispatch the job using job_runner
    try:
        from job_runner import job_runner, JobStatus
        status, message = job_runner.run_job(
            canonical_job_name,
            force=True,
            send=True,
            requester=sender,
        )
        
        # Return a user-facing message based on the status
        if status == JobStatus.SUCCESS:
            # Extract the canonical job display name
            job_display = {
                "sharp_picks": "Sharp Picks",
                "happy_hour": "Happy Hour Scout",
                "familia_meal_planner": "Familia Meal Planner",
            }.get(canonical_job_name, canonical_job_name)
            
            return f"✅ {job_display} was dispatched. I'll send the report when generation and delivery complete."
        
        elif status == JobStatus.ALREADY_RUNNING:
            return "⏳ That job is already running. Please wait for it to complete."
        
        elif status == JobStatus.NOT_FOUND:
            return f"❌ Job '{canonical_job_name}' not found. Try: Run Sharp Picks, Run Happy Hour, or Run Meal Planner."
        
        elif status == JobStatus.UNAVAILABLE:
            return f"⚠️ {canonical_job_name} is currently unavailable. Please try again later."
        
        else:  # ERROR or other status
            return f"❌ Could not start {canonical_job_name}. Please try again."
    
    except Exception as exc:
        logger.error(
            "Job command dispatch failed job=%s error=%s",
            canonical_job_name,
            type(exc).__name__,
        )
        return "❌ An error occurred while starting that job. Please try again."

# OPERATIONS COMMAND HANDLER (deterministic — no LLM)
# ============================================================================


def get_tailscale_status() -> str:
    """Return a concise, iMessage-safe Tailscale status summary.

    Uses ``tailscale status --json`` via subprocess (no shell=True).
    Never exposes auth keys, node keys, machine keys, or control URLs.
    """
    cli = _shutil.which("tailscale")
    if not cli:
        return "🔴 Tailscale unavailable\nThe tailscale CLI was not found on the iMac."

    try:
        result = subprocess.run(
            [cli, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=STATUS_COMMAND_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return "🟠 Tailscale status could not be read.\nThe local status check timed out."
    except Exception as exc:
        logger.warning("Tailscale status failed error=%s", type(exc).__name__)
        return "🟠 Tailscale status could not be read."

    if result.returncode != 0:
        return "🟠 Tailscale status could not be read."

    try:
        data = json.loads(result.stdout)
    except Exception:
        return "🟠 Tailscale status could not be read."

    # --- Extract safe fields only ---
    backend_state: str = data.get("BackendState", "Unknown")
    self_node: Dict[str, Any] = data.get("Self", {})

    hostname: str = self_node.get("HostName") or self_node.get("DNSName", "unknown")
    # Strip trailing dot and domain suffix from DNSName if present
    if "." in hostname:
        hostname = hostname.split(".")[0]

    ts_ip: str = ""
    tailscale_ips = self_node.get("TailscaleIPs") or []
    for ip in tailscale_ips:
        # Prefer IPv4
        if ":" not in str(ip):
            ts_ip = str(ip)
            break
    if not ts_ip and tailscale_ips:
        ts_ip = str(tailscale_ips[0])

    running = backend_state in ("Running", "Starting")
    state_label = "Running" if running else backend_state

    # Exit node
    exit_node_status = data.get("ExitNodeStatus") or {}
    exit_node_id = exit_node_status.get("ID", "")
    exit_node_name = "None"
    if exit_node_id:
        # Try to resolve name from peer list
        for peer in (data.get("Peer") or {}).values():
            if peer.get("ID") == exit_node_id:
                peer_host = peer.get("HostName") or peer.get("DNSName", "")
                if "." in peer_host:
                    peer_host = peer_host.split(".")[0]
                exit_node_name = peer_host or exit_node_id
                break
        if exit_node_name == "None":
            exit_node_name = exit_node_id[:16]

    # Online peers
    peers: Dict[str, Any] = data.get("Peer") or {}
    online_peers = []
    for peer in peers.values():
        if peer.get("Online"):
            name = peer.get("HostName") or peer.get("DNSName", "")
            if "." in name:
                name = name.split(".")[0]
            if name:
                online_peers.append(name)
    online_peers.sort()

    # Build summary
    icon = "🟢" if running else "🟠"
    lines = [
        f"{icon} Tailscale Status",
        "",
        f"Local device: {hostname}",
        f"Tailscale IP: {ts_ip or 'N/A'}",
        f"Backend: {state_label}",
        f"Exit node: {exit_node_name}",
        f"Online peers: {len(online_peers)}",
    ]
    for p in online_peers:
        lines.append(f"• {p}")

    return "\n".join(lines)


def get_imessage_runtime_snapshot() -> Dict[str, Any]:
    """Return non-sensitive poller readiness and latency metrics."""
    snapshot = _imessage_metrics.snapshot()
    snapshot["enabled"] = ENABLE_IMESSAGE_POLLER
    try:
        snapshot["durable_states"] = _IMESSAGE_STATE.recent_counts()
    except Exception as exc:
        snapshot["durable_states"] = {}
        snapshot["state_store_error"] = type(exc).__name__

    last_poll_age = snapshot.get("last_poll_age_seconds")
    oldest_age = snapshot.get("oldest_queued_age_seconds")
    thread_ready = all(
        snapshot.get(name)
        for name in ("collector_alive", "dispatcher_alive", "slow_worker_alive")
    )
    poll_fresh = last_poll_age is not None and last_poll_age <= max(10, POLLING_INTERVAL * 5)
    queue_fresh = oldest_age is None or oldest_age <= IMESSAGE_STALE_QUEUE_SECONDS
    snapshot["ready"] = (
        not ENABLE_IMESSAGE_POLLER
        or (thread_ready and poll_fresh and queue_fresh)
    )
    return snapshot


def _get_ivy_status_text() -> str:
    """Return a deterministic status grounded in runtime telemetry."""
    uptime = datetime.now() - PROCESS_STARTED_AT
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60
    uptime_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

    statuses = compute_tool_statuses()
    ready_count = sum(1 for s in statuses if s["status"] == "ready")
    total_count = len(statuses)

    jobs_available = 0
    jobs_total = 0
    if JOB_RUNNER_AVAILABLE:
        all_jobs = job_runner.list_jobs()
        jobs_total = len(all_jobs)
        jobs_available = sum(1 for j in all_jobs if j["available"])

    runtime = get_imessage_runtime_snapshot()
    chat_readable = os.path.exists(CHAT_DB_PATH) and os.access(CHAT_DB_PATH, os.R_OK)
    providers = _cached_provider_snapshot()
    provider_ready = sum(1 for value in providers.values() if value.get("authenticated"))
    configured_providers = sum(
        1
        for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")
        if os.environ.get(name, "").strip()
    )

    critical_failure = (
        not chat_readable
        or (ENABLE_IMESSAGE_POLLER and not runtime.get("collector_alive"))
    )
    degraded = (
        not runtime.get("ready", True)
        or configured_providers == 0
        or runtime.get("last_error_category") is not None
    )
    if critical_failure:
        icon, overall = "🔴", "UNAVAILABLE"
    elif degraded:
        icon, overall = "🟠", "DEGRADED"
    else:
        icon, overall = "🟢", "READY"

    lines = [
        f"{icon} Ivy Gateway — {overall}",
        "",
        f"Uptime: {uptime_str}",
        "Poller: " + (
            "disabled"
            if not ENABLE_IMESSAGE_POLLER
            else "active" if runtime.get("collector_alive") else "stopped"
        ),
        f"chat.db: {'readable' if chat_readable else 'unavailable'}",
        f"Queue: {runtime.get('queue_depth', 0)} inbound / "
        f"{runtime.get('slow_queue_depth', 0)} processing",
        f"Oldest queued: {runtime.get('oldest_queued_age_seconds') or 0:.1f}s",
        f"Last response: {runtime.get('last_response_latency_ms') or 0}ms",
        f"AppleEvent timeouts: {runtime.get('apple_event_timeouts', 0)}",
        f"Providers authenticated: {provider_ready}/{configured_providers}",
        f"Tools ready: {ready_count}/{total_count}",
        f"Jobs available: {jobs_available}/{jobs_total}",
        f"Caching: {'on' if (ENABLE_PROMPT_CACHING and CACHING_AVAILABLE) else 'off'}",
        f"Host: {socket.gethostname()}",
    ]
    return "\n".join(lines)


def _get_capabilities_text() -> str:
    """Return a human-readable list of all tools and jobs."""
    statuses = compute_tool_statuses()
    lines = ["📋 Ivy Skills & Capabilities", ""]
    lines.append("Tools:")
    for s in statuses:
        if s["status"] == "ready":
            lines.append(f"  ✅ {s['tool_name']}")
        elif s["status"] == "disabled":
            lines.append(f"  ⊘ {s['tool_name']} (disabled)")
        else:
            lines.append(f"  ❌ {s['tool_name']} (unavailable)")

    if JOB_RUNNER_AVAILABLE:
        lines.append("")
        lines.append("Jobs:")
        for job in job_runner.list_jobs():
            if job["available"]:
                sched = job.get("schedule") or "on-demand"
                lines.append(f"  ✅ {job['display_name']} — {sched}")
            else:
                lines.append(f"  ❌ {job['display_name']} (unavailable)")

    return "\n".join(lines)


def _get_job_status_text(job_name: Optional[str] = None) -> str:
    """Return recent execution history for a job (or all jobs)."""
    try:
        recent = receipts.list_recent(limit=5, job_name=job_name)
    except Exception as exc:
        logger.warning("receipts.list_recent failed error=%s", type(exc).__name__)
        return "⚠️ Could not read execution history."

    label = job_name.replace("_", " ").title() if job_name else "All Jobs"
    if not recent:
        return f"📋 {label} — no recent executions found."

    lines = [f"📋 {label} — recent runs:", ""]
    for rec in recent:
        started = rec.get("started_at", "")[:16].replace("T", " ")
        status = rec.get("status", "?")
        name = rec.get("job_name", "?")
        execution_ref = str(rec.get("execution_id", ""))[:8] or "unknown"
        if status == "completed":
            icon = "✅"
        elif status in {"queued", "dispatched", "running"}:
            icon = "🔄"
        elif status in {"completion_unknown", "triggered_unobserved"}:
            icon = "🟠"
        elif status == "skipped":
            icon = "⏭️"
        else:
            icon = "❌"
        lines.append(f"{icon} {name} @ {started} — {status} ({execution_ref})")
        delivery = rec.get("delivery_status")
        if delivery:
            lines.append(f"  Delivery: {delivery}")
        report_ids = rec.get("report_ids") or []
        if report_ids:
            lines.append(f"  Report: {str(report_ids[0])[:40]}")
        log_path = rec.get("log_path")
        if log_path:
            lines.append(f"  Log: {Path(str(log_path)).name}")
        if status in {"completion_unknown", "triggered_unobserved"}:
            lines.append("  Completion or delivery has not been confirmed.")
        elif status in {"failed", "dispatch_failed", "timed_out"}:
            outcome = str(rec.get("outcome") or "failed")[:80]
            lines.append(f"  Outcome: {outcome}")
    return "\n".join(lines)


# Phrase → category mapping for deterministic operations routing.
# Phrases are matched after stripping a leading "ivy" or "ivy," token and
# collapsing whitespace.  Order does not matter; exact set-membership lookup.
_OPS_TAILSCALE: frozenset = frozenset({
    "tailscale status",
    "check tailscale",
    "is tailscale online",
    "tailscale",
    "vpn status",
    "network status",
})

_OPS_IVY_STATUS: frozenset = frozenset({
    "ivy status",
    "status",
    "health check",
    "system health",
    "gateway status",
    "poller status",
    "is ivy online",
    "are you working",
})

_OPS_CAPABILITIES: frozenset = frozenset({
    "skills",
    "ivy skills",
    "list skills",
    "tell me all your skills",
    "tell me all of your skills",
    "what can you do",
    "capabilities",
    "list capabilities",
    "what is turned on",
    "what things are turned on",
    "are all your skills active",
    "advise if all your skills are active",
    # variants with "of your" wording (seen in real messages)
    "advise if all of your skills are active",
    "tell me what are all of the things that are turned on",
})

_OPS_JOB_STATUS: frozenset = frozenset({
    "sharp picks status",
    "last sharp picks",
    "last job",
    "recent jobs",
    "execution status",
})

_OPS_HELP: frozenset = frozenset({"ivy", "help", "ivy help"})


def _normalize_ops_text(text: str) -> str:
    """Normalize text for operations command recognition only.

    - Strips leading/trailing whitespace.
    - Collapses repeated whitespace to a single space.
    - Lowercases.
    - Optionally removes a leading ``ivy`` or ``ivy,`` token.
    """
    normalized = " ".join(text.strip().split()).lower()
    # Strip optional leading "ivy," or "ivy " prefix
    for prefix in ("ivy, ", "ivy,", "ivy "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
            break
    return normalized


def handle_operations_command(text: str, sender: str) -> Optional[str]:  # noqa: ARG001
    """Deterministic operations command handler — never calls an LLM.

    Returns a user-facing reply string when the text matches a known
    operations command, or ``None`` to let the caller fall through to the
    LLM provider chain.

    Routing order handled here (after auth & resend, before LLM):
      tailscale status  →  get_tailscale_status()
      ivy/gateway status  →  _get_ivy_status_text()
      skills/capabilities  →  _get_capabilities_text()
      job status  →  _get_job_status_text()
    """
    key = _normalize_ops_text(text)

    if key in _OPS_HELP:
        return (
            "Ivy is online. Try: Ivy status, Tailscale status, list skills, "
            "recent jobs, or Run Sharp Picks."
        )

    if key in _OPS_TAILSCALE:
        logger.info("OPS: tailscale status request")
        return get_tailscale_status()

    if key in _OPS_IVY_STATUS:
        logger.info("OPS: ivy status request")
        return _get_ivy_status_text()

    if key in _OPS_CAPABILITIES:
        logger.info("OPS: capabilities request")
        return _get_capabilities_text()

    if key in _OPS_JOB_STATUS:
        logger.info("OPS: job status request")
        # "sharp picks status" → filter to that job
        job_filter: Optional[str] = "sharp_picks" if "sharp picks" in key else None
        return _get_job_status_text(job_filter)

    return None


# ============================================================================
# BACKGROUND IMESSAGE WORKER: DeepSeek Primary + Gemini Backup with CACHING
# ============================================================================


def _is_authorized_sender(sender: str) -> bool:
    """Apply exact, fail-closed sender authorization."""
    if not sender:
        return False
    if sender.casefold() == "me":
        return True
    return sender in load_favorites_cached()


def _operations_category(text: str) -> Optional[str]:
    key = _normalize_ops_text(text)
    if key in _OPS_TAILSCALE:
        return "ops_tailscale"
    if key in _OPS_IVY_STATUS:
        return "ops_status"
    if key in _OPS_CAPABILITIES:
        return "ops_capabilities"
    if key in _OPS_JOB_STATUS:
        return "ops_jobs"
    if key in _OPS_HELP:
        return "ops_help"
    return None


_MUTATING_CONVERSATION_PATTERN = re.compile(
    r"\b(add|create|delete|remove|send|text|message|schedule|book|cancel|"
    r"update|write|set|remind|resend|run|launch|start|put|place|order|buy|"
    r"pay|share|forward|complete|finish|move|rename|upload|download)\b",
    re.IGNORECASE,
)


def classify_imessage_text(text: str) -> str:
    """Classify without executing a tool or calling a model."""
    if _RESEND_PATTERN.match(text.strip()):
        return "resend"
    if _resolve_job_command(text):
        return "job"
    operations = _operations_category(text)
    if operations:
        return operations
    if _MUTATING_CONVERSATION_PATTERN.search(text):
        return "conversation_action"
    return "conversation_read_only"


def coalesce_imessage_messages(messages: List[InboundMessage]) -> ProcessingUnit:
    """Build one ordered processing unit from same-sender rapid messages."""
    if not messages:
        raise ValueError("cannot coalesce an empty message list")
    ordered = tuple(sorted(messages, key=lambda message: message.message_id))
    categories = [classify_imessage_text(message.text) for message in ordered]
    unique_categories = set(categories)
    if all(category.startswith("ops_") for category in categories):
        category = categories[0] if len(unique_categories) == 1 else "operations"
    elif "conversation_action" in unique_categories:
        category = "conversation_action"
    elif unique_categories == {"conversation_read_only"}:
        category = "conversation_read_only"
    else:
        # Dispatcher keeps jobs/resends separate; this is a defensive fallback.
        category = categories[0]
    return ProcessingUnit(messages=ordered, category=category)


def _queue_oldest_monotonic() -> Optional[float]:
    values: List[float] = []
    with _IMESSAGE_INBOX_QUEUE.mutex:
        values.extend(
            item.collected_monotonic
            for item in _IMESSAGE_INBOX_QUEUE.queue
            if isinstance(item, InboundMessage)
        )
    with _IMESSAGE_SLOW_QUEUE.mutex:
        values.extend(
            item.collected_monotonic
            for item in _IMESSAGE_SLOW_QUEUE.queue
            if isinstance(item, ProcessingUnit)
        )
    return min(values) if values else None


def _update_imessage_queue_metrics() -> None:
    _imessage_metrics.update_queues(
        _IMESSAGE_INBOX_QUEUE.qsize(),
        _IMESSAGE_SLOW_QUEUE.qsize(),
        _queue_oldest_monotonic(),
    )


def _is_superseded(unit: ProcessingUnit) -> bool:
    if not _category_can_be_superseded(unit.category):
        return False
    with _IMESSAGE_LATEST_LOCK:
        return _IMESSAGE_LATEST_BY_SENDER.get(unit.sender, 0) > unit.newest_message_id


def _category_can_be_superseded(category: str) -> bool:
    return category in {
        "operations",
        "ops_tailscale",
        "ops_status",
        "ops_capabilities",
        "ops_jobs",
        "ops_help",
        "conversation_read_only",
    }


def conversation_history(sender: str) -> List[Dict[str, str]]:
    """Recent turns for ``sender`` as [{role, content}], oldest first.
    Entries older than CONVERSATION_TTL_SECONDS are dropped on read."""
    cutoff = time.time() - CONVERSATION_TTL_SECONDS
    with _CONVERSATION_LOCK:
        turns = [t for t in _CONVERSATIONS.get(sender, []) if t["ts"] >= cutoff]
        if turns:
            _CONVERSATIONS[sender] = turns
        else:
            _CONVERSATIONS.pop(sender, None)
        return [{"role": t["role"], "content": t["content"]} for t in turns]


def remember_turn(sender: str, user_text: str, reply_text: Optional[str]) -> None:
    """Record one exchange. An unanswered turn is still recorded, so a retry
    after a provider outage still sees what the user originally asked."""
    now = time.time()
    with _CONVERSATION_LOCK:
        turns = _CONVERSATIONS.setdefault(sender, [])
        turns.append({"role": "user", "content": user_text, "ts": now})
        if reply_text:
            turns.append({"role": "assistant", "content": str(reply_text), "ts": now})
        del turns[:-CONVERSATION_MAX_MESSAGES]


def forget_conversation(sender: str) -> None:
    with _CONVERSATION_LOCK:
        _CONVERSATIONS.pop(sender, None)


def format_history_for_prompt(history: List[Dict[str, str]]) -> str:
    """Flatten turns for providers that take a single prompt string."""
    lines = ["Recent conversation (oldest first):"]
    for turn in history:
        speaker = "User" if turn.get("role") == "user" else "Ivy"
        lines.append(f"{speaker}: {turn.get('content', '')}")
    return "\n".join(lines)


def _conversation_reply(text: str, sender: Optional[str] = None) -> str:
    """Run the bounded DeepSeek → OpenAI → Gemini provider chain, with the
    sender's recent turns supplied as context."""
    instruction = DEEPSEEK_SYSTEM_INSTRUCTION_TEMPLATE.format(
        current_date_str=datetime.now().strftime("%A, %B %d, %Y")
    )
    history = conversation_history(sender) if sender else []
    reply: Optional[str] = None
    for provider_name, provider_call in (
        ("deepseek", lambda: execute_deepseek_call(text, instruction, history=history)),
        ("openai", lambda: execute_openai_call(text, instruction, history=history)),
        ("gemini", lambda: _gemini_backup_reply(text, history=history)),
    ):
        try:
            reply = provider_call()
        except Exception as exc:
            logger.warning(
                "Conversation provider failed provider=%s error=%s",
                provider_name,
                type(exc).__name__,
            )
            reply = None
        if reply:
            if sender:
                remember_turn(sender, text, str(reply))
            return str(reply)
    if sender:
        remember_turn(sender, text, None)
    return (
        "Ivy's conversation engines are temporarily unavailable. "
        "Local commands such as Run Sharp Picks still work."
    )


def _operations_reply(unit: ProcessingUnit) -> str:
    replies: List[str] = []
    seen: set[str] = set()
    for message in unit.messages:
        category = _operations_category(message.text)
        if not category or category in seen:
            continue
        seen.add(category)
        reply = handle_operations_command(message.text, message.sender)
        if reply:
            replies.append(reply)
    return "\n\n".join(replies) or "Ivy is online. Text Ivy status for details."


def _send_unit_reply(
    unit: ProcessingUnit,
    reply: str,
    *,
    outcome: str = "replied",
    terminal_status: str = "completed",
    allow_supersession: bool = True,
) -> None:
    if allow_supersession and _is_superseded(unit):
        _IMESSAGE_STATE.mark_terminal(
            unit.message_ids,
            "superseded",
            outcome="newer_request_received",
        )
        return

    _IMESSAGE_STATE.mark_sending(unit.message_ids)
    result = run_local_applescript_send(unit.sender, reply)
    elapsed_ms = int((time.monotonic() - unit.collected_monotonic) * 1000)
    if result == "SUCCESS":
        try:
            _IMESSAGE_STATE.mark_terminal(
                unit.message_ids,
                terminal_status,
                outcome=outcome,
            )
        except Exception as exc:
            # The reply was already submitted. Never turn a bookkeeping
            # failure into a second contradictory iMessage. If a transient
            # write failure clears, preserve the conservative ambiguous state
            # for recovery; otherwise the original queued state remains.
            logger.error(
                "iMessage terminal receipt failed after send error=%s",
                type(exc).__name__,
            )
            _imessage_metrics.record_error("inbox_state_after_send_failed")
            try:
                _IMESSAGE_STATE.mark_terminal(
                    unit.message_ids,
                    "completion_unknown",
                    outcome="sent_receipt_unavailable",
                )
            except Exception:
                pass
        _imessage_metrics.record_response(unit.category, elapsed_ms)
    else:
        try:
            _IMESSAGE_STATE.mark_terminal(
                unit.message_ids,
                "failed",
                outcome="outbound_send_failed",
                detail=_IMESSAGE_RUNNER.last_error_category or "applescript_error",
            )
        except Exception as exc:
            logger.error(
                "iMessage failure receipt could not be persisted error=%s",
                type(exc).__name__,
            )
        _imessage_metrics.record_error("outbound_send_failed")


def _start_slow_ack_timer(
    unit: ProcessingUnit,
) -> tuple[threading.Timer, threading.Event, Any]:
    finished = threading.Event()
    send_gate = threading.Lock()

    def send_ack() -> None:
        with send_gate:
            if finished.is_set() or _is_superseded(unit):
                return
            run_local_applescript_send(
                unit.sender,
                "Working on that now. I'll send the result when it finishes.",
            )

    timer = threading.Timer(IMESSAGE_SLOW_ACK_SECONDS, send_ack)
    timer.name = f"ivy-imessage-ack-{unit.newest_message_id}"
    timer.daemon = True
    timer.start()
    return timer, finished, send_gate


def _stop_slow_ack_timer(
    timer: Optional[threading.Timer],
    finished: Optional[threading.Event],
    send_gate: Optional[Any],
) -> None:
    """Prevent an acknowledgement from being sent after the final reply."""
    if finished is not None:
        finished.set()
    if timer is not None:
        timer.cancel()
    # If the callback already passed its event check, wait until it has either
    # sent its acknowledgement or observed completion before sending final text.
    if send_gate is not None:
        with send_gate:
            pass


def _process_imessage_unit(unit: ProcessingUnit) -> None:
    if not _IMESSAGE_STATE.mark_processing(unit.message_ids, unit.category):
        return
    if _is_superseded(unit):
        _IMESSAGE_STATE.mark_terminal(
            unit.message_ids,
            "superseded",
            outcome="superseded_before_processing",
        )
        return

    timer: Optional[threading.Timer] = None
    ack_finished: Optional[threading.Event] = None
    ack_send_gate: Optional[Any] = None
    _IMESSAGE_TOOL_CONTEXT.mutation_started = False
    try:
        if unit.category.startswith("ops_") or unit.category == "operations":
            reply = _operations_reply(unit)
        elif unit.category == "resend":
            reply = handle_resend_command(unit.text, unit.sender)
        elif unit.category == "job":
            reply = handle_job_command(unit.text, unit.sender)
        else:
            timer, ack_finished, ack_send_gate = _start_slow_ack_timer(unit)
            reply = _conversation_reply(unit.text, unit.sender)

        if not reply:
            raise RuntimeError("handler produced no reply")
        _stop_slow_ack_timer(timer, ack_finished, ack_send_gate)
        timer = None
        ack_finished = None
        ack_send_gate = None
        _send_unit_reply(
            unit,
            str(reply),
            allow_supersession=not bool(
                getattr(_IMESSAGE_TOOL_CONTEXT, "mutation_started", False)
            ),
        )
    except Exception as exc:
        logger.error(
            "iMessage processing failed category=%s error=%s",
            unit.category,
            type(exc).__name__,
        )
        _imessage_metrics.record_error(f"handler:{unit.category}")
        _send_unit_reply(
            unit,
            "I couldn't complete that request. Please try again.",
            outcome="handler_failed",
            terminal_status="failed",
            allow_supersession=not bool(
                getattr(_IMESSAGE_TOOL_CONTEXT, "mutation_started", False)
            ),
        )
    finally:
        _stop_slow_ack_timer(timer, ack_finished, ack_send_gate)
        _IMESSAGE_TOOL_CONTEXT.mutation_started = False
        _update_imessage_queue_metrics()


def _enqueue_slow_unit(unit: ProcessingUnit) -> None:
    while not _IMESSAGE_STOP_EVENT.is_set():
        try:
            _IMESSAGE_SLOW_QUEUE.put(
                unit,
                timeout=IMESSAGE_QUEUE_PUT_TIMEOUT_SECONDS,
            )
            _update_imessage_queue_metrics()
            return
        except queue.Full:
            _imessage_metrics.record_error("slow_queue_full")
    # Durable status remains queued and can be rehydrated on restart.


def _imessage_dispatcher_worker() -> None:
    """Debounce by sender and keep deterministic status work off the slow lane."""
    _imessage_metrics.set_thread_state("dispatcher", True)
    pending: Dict[str, tuple[List[InboundMessage], str, float]] = {}

    def flush(sender: str) -> None:
        entry = pending.pop(sender, None)
        if not entry:
            return
        messages, _group, _deadline = entry
        unit = coalesce_imessage_messages(messages)
        _IMESSAGE_STATE.update_category(unit.message_ids, unit.category)
        if unit.category.startswith("ops_") or unit.category == "operations":
            _process_imessage_unit(unit)
        else:
            _enqueue_slow_unit(unit)

    try:
        while not _IMESSAGE_STOP_EVENT.is_set() or not _IMESSAGE_INBOX_QUEUE.empty():
            now = time.monotonic()
            for sender, (_messages, _group, deadline) in list(pending.items()):
                if deadline <= now:
                    flush(sender)

            next_deadline = min((entry[2] for entry in pending.values()), default=now + 0.2)
            timeout = max(0.01, min(0.2, next_deadline - now))
            try:
                message = _IMESSAGE_INBOX_QUEUE.get(timeout=timeout)
            except queue.Empty:
                continue

            try:
                category = classify_imessage_text(message.text)
                group = (
                    "operations" if category.startswith("ops_")
                    else category
                )
                if category in {"job", "resend"}:
                    flush(message.sender)
                    unit = coalesce_imessage_messages([message])
                    _IMESSAGE_STATE.update_category(unit.message_ids, unit.category)
                    if category == "job":
                        # Dispatching a registered job is deterministic and
                        # bounded; do not strand it behind a slow provider.
                        _process_imessage_unit(unit)
                    else:
                        _enqueue_slow_unit(unit)
                    continue

                existing = pending.get(message.sender)
                if existing and existing[1] != group:
                    flush(message.sender)
                    existing = None
                messages = list(existing[0]) if existing else []
                messages.append(message)
                pending[message.sender] = (
                    messages,
                    group,
                    time.monotonic() + IMESSAGE_DEBOUNCE_SECONDS,
                )
            finally:
                _IMESSAGE_INBOX_QUEUE.task_done()
                _update_imessage_queue_metrics()
    finally:
        for sender in list(pending):
            flush(sender)
        _imessage_metrics.set_thread_state("dispatcher", False)


def _imessage_slow_worker() -> None:
    _imessage_metrics.set_thread_state("slow_worker", True)
    try:
        while not _IMESSAGE_STOP_EVENT.is_set() or not _IMESSAGE_SLOW_QUEUE.empty():
            try:
                unit = _IMESSAGE_SLOW_QUEUE.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                _process_imessage_unit(unit)
            finally:
                _IMESSAGE_SLOW_QUEUE.task_done()
                _update_imessage_queue_metrics()
    finally:
        _imessage_metrics.set_thread_state("slow_worker", False)


def _enqueue_recovered_messages(message_ids: List[int]) -> List[int]:
    """Rehydrate queued ROWIDs and return only IDs still awaiting recovery."""
    for offset in range(0, len(message_ids), 100):
        chunk = message_ids[offset:offset + 100]
        rows = safe_fetch_messages_by_ids(chunk)
        if rows is None:
            _imessage_metrics.record_error("recovery_chat_db_failed")
            return message_ids[offset:]
        rows_by_id = {int(row[0]): row for row in rows}
        for message_id in chunk:
            row = rows_by_id.get(int(message_id))
            if not row:
                _IMESSAGE_STATE.mark_terminal(
                    [message_id],
                    "failed",
                    outcome="source_row_unavailable_after_restart",
                )
                continue
            msg_id, text, sender = row
            if not _is_authorized_sender(str(sender)):
                _IMESSAGE_STATE.mark_terminal([msg_id], "blocked", outcome="unauthorized")
                continue
            message = InboundMessage(
                message_id=int(msg_id),
                text=str(text),
                sender=str(sender),
                collected_monotonic=time.monotonic(),
            )
            while not _IMESSAGE_STOP_EVENT.is_set():
                try:
                    _IMESSAGE_INBOX_QUEUE.put(
                        message,
                        timeout=IMESSAGE_QUEUE_PUT_TIMEOUT_SECONDS,
                    )
                    break
                except queue.Full:
                    _imessage_metrics.record_error("recovery_queue_full")
            else:
                position = offset + chunk.index(message_id)
                return message_ids[position:]
            if _category_can_be_superseded(classify_imessage_text(message.text)):
                with _IMESSAGE_LATEST_LOCK:
                    _IMESSAGE_LATEST_BY_SENDER[message.sender] = max(
                        message.message_id,
                        _IMESSAGE_LATEST_BY_SENDER.get(message.sender, 0),
                    )
    return []


def _collect_imessage_rows(rows: List[tuple], cursor: int) -> int:
    """Authorize and reserve one ordered batch, returning the durable cursor."""
    for row in rows:
        try:
            msg_id, text, sender = int(row[0]), str(row[1]), str(row[2])
        except (IndexError, TypeError, ValueError):
            _imessage_metrics.record_error("invalid_chat_db_row")
            continue
        if msg_id <= cursor:
            continue
        if not _IMESSAGE_STATE.reserve(msg_id):
            cursor = msg_id
            _IMESSAGE_STATE.advance_cursor(cursor)
            continue
        if not _is_authorized_sender(sender):
            _IMESSAGE_STATE.mark_terminal([msg_id], "blocked", outcome="unauthorized")
            cursor = msg_id
            _IMESSAGE_STATE.advance_cursor(cursor)
            continue

        message = InboundMessage(
            message_id=msg_id,
            text=text,
            sender=sender,
            collected_monotonic=time.monotonic(),
        )
        try:
            _IMESSAGE_INBOX_QUEUE.put(
                message,
                timeout=IMESSAGE_QUEUE_PUT_TIMEOUT_SECONDS,
            )
        except queue.Full:
            _IMESSAGE_STATE.release_reservation(msg_id)
            _imessage_metrics.record_error("inbox_queue_full")
            break

        if _category_can_be_superseded(classify_imessage_text(text)):
            with _IMESSAGE_LATEST_LOCK:
                _IMESSAGE_LATEST_BY_SENDER[sender] = max(
                    msg_id,
                    _IMESSAGE_LATEST_BY_SENDER.get(sender, 0),
                )
        cursor = msg_id
        _IMESSAGE_STATE.advance_cursor(cursor)
    _update_imessage_queue_metrics()
    return cursor


def background_imessage_worker() -> None:
    """Collect chat.db rows in batches while bounded workers process them."""
    _IMESSAGE_POLLER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    poller_lock = FileLock(str(_IMESSAGE_POLLER_LOCK_PATH))
    try:
        poller_lock.acquire(timeout=0)
    except FileLockTimeout:
        _imessage_metrics.record_error("duplicate_poller_instance")
        logger.error("Another iMessage collector already owns the runtime lock")
        return
    except Exception as exc:
        _imessage_metrics.record_error("poller_lock_failed")
        logger.error("iMessage runtime lock failed error=%s", type(exc).__name__)
        return

    dispatcher: Optional[threading.Thread] = None
    slow_worker: Optional[threading.Thread] = None
    _imessage_metrics.set_thread_state("collector", True)
    try:
        current_max = get_last_message_id()
        if current_max is None:
            _imessage_metrics.record_error("chat_db_unavailable")
            logger.error("Cannot initialize iMessage collection; chat.db is unavailable")
            return

        cursor = _IMESSAGE_STATE.initialize_cursor(current_max)
        if cursor > current_max:
            # Messages replaced/rotated chat.db and ROWIDs restarted.  Start at
            # the new high-water mark rather than remaining permanently stuck.
            cursor = current_max
            _IMESSAGE_STATE.reset_cursor(cursor)
            _imessage_metrics.record_error("chat_db_rowid_reset")

        dispatcher = threading.Thread(
            target=_imessage_dispatcher_worker,
            name="ivy-imessage-dispatcher",
            daemon=True,
        )
        slow_worker = threading.Thread(
            target=_imessage_slow_worker,
            name="ivy-imessage-processor",
            daemon=True,
        )
        dispatcher.start()
        slow_worker.start()

        recovery_ids = _IMESSAGE_STATE.recover_after_restart()
        while recovery_ids and not _IMESSAGE_STOP_EVENT.is_set():
            recovery_ids = _enqueue_recovered_messages(recovery_ids)
            if not recovery_ids:
                break
            _IMESSAGE_STOP_EVENT.wait(_db_retry_backoff(0))
        last_prune = time.monotonic()
        consecutive_failures = 0

        while not _IMESSAGE_STOP_EVENT.wait(POLLING_INTERVAL):
            rows = safe_fetch_new_messages(cursor, IMESSAGE_FETCH_BATCH_SIZE)
            if rows is None:
                consecutive_failures += 1
                _imessage_metrics.record_error("chat_db_poll_failed")
                _IMESSAGE_STOP_EVENT.wait(min(30.0, 2 ** min(consecutive_failures, 5)))
                continue

            consecutive_failures = 0
            _imessage_metrics.record_poll()
            cursor = _collect_imessage_rows(rows, cursor)
            if time.monotonic() - last_prune >= 3600:
                _IMESSAGE_STATE.prune_terminal()
                last_prune = time.monotonic()
    except Exception as exc:
        _imessage_metrics.record_error("collector_unhandled_error")
        logger.error("iMessage collector stopped error=%s", type(exc).__name__)
    finally:
        _IMESSAGE_STOP_EVENT.set()
        for thread in (dispatcher, slow_worker):
            if thread is not None:
                thread.join(timeout=IMESSAGE_WORKER_JOIN_TIMEOUT_SECONDS)
        _imessage_metrics.set_thread_state("collector", False)
        poller_lock.release()


# ============================================================================
# FASTAPI ENDPOINTS
# ============================================================================


@app.get("/health")
def health_endpoint(authenticated: bool = Depends(verify_api_key)):
    """Pure process-liveness check; never waits on an external provider."""
    return {
        "status": "ok",
        "pid": os.getpid(),
        "uptime_seconds": get_imessage_runtime_snapshot()["uptime_seconds"],
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
        "messages_runtime_available": (
            sys.platform == "darwin" and _shutil.which("osascript") is not None
        ),
    }
    try:
        # Readiness is a read-only observation.  Watchdog reconciliation runs
        # in job dispatch/status flows, not inside a monitoring GET.
        receipts.list_recent(limit=1, reconcile=False)
        checks["receipts_db_writable"] = True
    except Exception as exc:
        logger.warning("Receipts DB check failed error=%s", type(exc).__name__)
        checks["receipts_db_writable"] = False

    # Read only the background probe cache; readiness itself never performs
    # network I/O or waits for a provider.
    providers = _cached_provider_snapshot()
    any_authenticated = any(p.get("authenticated") for p in providers.values())
    checks["llm_provider_authenticated"] = any_authenticated

    runtime = get_imessage_runtime_snapshot()
    checks["imessage_worker_ready"] = runtime["ready"]

    ready = all(checks.values())
    payload: Dict[str, Any] = {"ready": ready, "checks": checks}
    if not ready:
        raise HTTPException(status_code=503, detail=payload)
    return payload


@app.get("/runtime")
def runtime_endpoint(authenticated: bool = Depends(verify_api_key)):
    """Authenticated, privacy-minimizing operational metrics snapshot."""
    return {
        "imessage": get_imessage_runtime_snapshot(),
        "providers": _cached_provider_snapshot(),
        "tools": compute_tool_statuses(),
    }


@app.get("/imessage/attachments")
def imessage_attachments_endpoint(
    since: float,
    filename: Optional[str] = None,
    handle: Optional[str] = None,
    limit: int = 20,
    authenticated: bool = Depends(verify_api_key),
):
    """Outgoing attachment rows from chat.db newer than ``since`` (unix
    seconds), optionally narrowed to one ``filename`` (the staged name the
    recipient sees) and/or one ``handle``. Each row carries a ``state`` of
    delivered / failed / pending.

    This is how job subprocesses — which do not have Full Disk Access — find
    out whether a PDF they just sent actually left the Mac. The gateway does
    have that access, so it does the chat.db read on their behalf
    (ivy_core.attachment_verify falls back to this endpoint automatically).
    """
    try:
        rows = attachment_verify.fetch_outgoing_attachments(
            since_ts=since, filename=filename or None, handle=handle or None, limit=limit,
        )
    except sqlite3.Error as exc:
        logger.warning("chat.db attachment lookup failed error=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="chat.db is not readable")
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
    return {
        "executions": [
            _public_execution_record(record)
            for record in receipts.list_recent(
                limit=limit,
                job_name=job_name,
                reconcile=False,
            )
        ]
    }


@app.get("/executions/{execution_id}")
def get_execution_endpoint(execution_id: str, authenticated: bool = Depends(verify_api_key)):
    record = receipts.get_execution(execution_id, reconcile=False)
    if not record:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    return _public_execution_record(record)


def _public_execution_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Remove requester identifiers, raw results, and private paths."""
    fields = (
        "execution_id",
        "job_name",
        "status",
        "started_at",
        "finished_at",
        "worker_started_at",
        "heartbeat_at",
        "updated_at",
        "pid",
        "exit_code",
        "outcome",
        "delivery_status",
        "report_ids",
        "terminal",
    )
    public = {field: record.get(field) for field in fields}
    public["log_name"] = (
        Path(str(record["log_path"])).name if record.get("log_path") else None
    )
    status = record.get("status")
    if status == "completion_unknown":
        public["detail"] = "Execution ended without a confirmed terminal result."
    elif status == "timed_out":
        public["detail"] = "Execution exceeded its configured runtime limit."
    elif status in {"failed", "dispatch_failed"}:
        public["detail"] = "Execution failed; inspect the protected local log."
    return public


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
            logger.warning(
                "Voice DeepSeek failed error=%s", type(deepseek_err).__name__
            )
            reply = None

        # ---- Phase 2: OpenAI fallback ----
        if not reply:
            try:
                reply = execute_openai_call(
                    req.query,
                    DEEPSEEK_SYSTEM_INSTRUCTION_TEMPLATE.format(
                        current_date_str=datetime.now().strftime("%A, %B %d, %Y")
                    ),
                )
            except Exception as openai_err:
                logger.warning(
                    "Voice OpenAI failed error=%s", type(openai_err).__name__
                )
                reply = None

        # ---- Phase 3: Gemini backup (cache-optimized, with tool execution) ----
        if not reply:
            try:
                messages = voice_processor.create_voice_prompt(
                    user_query=req.query,
                    session=session,
                    system_instruction=GEMINI_SYSTEM_INSTRUCTION,
                    tool_declarations=GEMINI_TOOL_DECLARATIONS
                )
                response = _gemini_generate_content(
                    contents=messages,
                )

                if ENABLE_CACHE_METRICS_LOGGING and CACHING_AVAILABLE:
                    cached_tokens, _ = cache_manager.log_cache_efficiency(
                        response, endpoint="voice_query", model="gemini-2.5-flash"
                    )

                text_reply, tool_calls, parts = _extract_gemini_reply_and_tool_calls(response)
                if text_reply or tool_calls:

                    if tool_calls:
                        tool_results = []
                        for call in tool_calls:
                            tool_name = call["name"]
                            tool_args = dict(call.get("args") or {})
                            if tool_name in ["add_apple_reminder", "fetch_apple_reminders"]:
                                tool_args["list_name"] = "Household"
                            tool_results.append((tool_name, _execute_tool_call(tool_name, tool_args)))

                        follow_up_response = _gemini_generate_content(
                            contents=[
                                *messages,
                                {"role": "model", "parts": _serialize_parts(parts)},
                                {
                                    "role": "function",
                                    "parts": [
                                        {"function_response": {"name": name, "response": {"result": result}}}
                                        for name, result in tool_results
                                    ],
                                },
                            ],
                            include_tools=False,
                        )
                        follow_up_text, _, _ = _extract_gemini_reply_and_tool_calls(follow_up_response)
                        reply = follow_up_text.strip() or None
                    else:
                        reply = text_reply.strip() or None
            except Exception as gemini_err:
                logger.warning(
                    "Voice Gemini failed error=%s", type(gemini_err).__name__
                )
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
    except Exception as exc:
        logger.error("Voice query failed error=%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Voice request failed") from exc


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

    dispatch = job_runner.run_job_detailed(job_name, force=True)
    return {
        "job": dispatch.canonical_job_name,
        "result": dispatch.message,
        "dispatch_status": dispatch.status.value,
        "lifecycle_status": dispatch.lifecycle_status,
        "execution_id": dispatch.execution_id,
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
