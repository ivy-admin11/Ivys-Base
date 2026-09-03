"""Ivy Local Admin API Gateway — Configuration & Constants

Centralized configuration for timeouts, feature flags, API schemas, and tool definitions.
All hardcoded values are defined here for easy tuning without modifying business logic.

This file is environment-agnostic; environment-specific secrets should go in .env
"""

import os
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

# Load .env from the project root before any os.environ.get() calls below run.
# override=False so variables already exported by the shell or launchd win —
# .env only fills in what isn't already set.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

# ============================================================================
# POLLING & DATABASE CONFIGURATION
# ============================================================================

POLLING_INTERVAL: int = int(os.environ.get("POLLING_INTERVAL", "1"))
"""Seconds between iMessage database polls"""

DB_TIMEOUT: float = float(os.environ.get("DB_TIMEOUT", "5"))
"""SQLite connection timeout (seconds)"""

DB_RETRY_ATTEMPTS: int = 3
"""Number of retries for failed database reads"""

DB_RETRY_BACKOFF: float = 0.5
"""Initial backoff for database retry (seconds); grows exponentially"""

CHAT_DB_PATH: str = os.path.expanduser(
    os.environ.get("CHAT_DB_PATH", "~/Library/Messages/chat.db")
)
"""Path to macOS iMessage database"""

# ============================================================================
# API TIMEOUTS & RATE LIMITING
# ============================================================================

EXTERNAL_API_TIMEOUT: int = int(os.environ.get("EXTERNAL_API_TIMEOUT", "20"))
"""Timeout for external API calls (requests, Readwise, etc.) in seconds"""

PLAYWRIGHT_TIMEOUT_MS: int = 10000
"""Playwright step timeout in milliseconds"""

API_RATE_LIMIT: int = int(os.environ.get("API_RATE_LIMIT", "100"))
"""Maximum requests per minute (for future rate limiting)"""

# ============================================================================
# FEATURE FLAGS
# ============================================================================

ENABLE_IMESSAGE_POLLER: bool = os.environ.get("ENABLE_IMESSAGE_POLLER", "true").lower() == "true"
"""Enable/disable background iMessage polling thread"""

ENABLE_GROCERY_STAGING: bool = os.environ.get("ENABLE_GROCERY_STAGING", "true").lower() == "true"
"""Enable/disable /stage_groceries endpoint"""

ENABLE_CALENDAR_INTEGRATION: bool = os.environ.get("ENABLE_CALENDAR_INTEGRATION", "true").lower() == "true"
"""Enable/disable check_apple_calendar tool"""

ENABLE_REMINDERS_INTEGRATION: bool = os.environ.get("ENABLE_REMINDERS_INTEGRATION", "true").lower() == "true"
"""Enable/disable Apple Reminders tools"""

ENABLE_READWISE_INTEGRATION: bool = os.environ.get("ENABLE_READWISE_INTEGRATION", "true").lower() == "true"
"""Enable/disable Readwise integration"""

PLAYWRIGHT_ENABLED: bool = os.environ.get("PLAYWRIGHT_ENABLED", "true").lower() == "true"
"""Enable/disable Playwright browser automation"""

PLAYWRIGHT_HEADLESS: bool = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
"""Run Playwright in headless mode (no window)"""

ENABLE_SPORTS_PICKS: bool = os.environ.get("ENABLE_SPORTS_PICKS", "true").lower() == "true"
"""Enable/disable the Sharp Picks sports-betting job/tool"""

# ============================================================================
# SECURITY & AUTHENTICATION
# ============================================================================

ADMIN_SECRET: str = os.environ.get("ADMIN_SECRET", "")
"""Shared secret for protecting FastAPI endpoints (HTTP header: X-API-Key)"""

if not ADMIN_SECRET:
    if os.environ.get("ALLOW_INSECURE_ADMIN_SECRET", "").lower() == "true":
        ADMIN_SECRET = "insecure-test-secret-do-not-use-in-production"
    else:
        raise RuntimeError(
            "ADMIN_SECRET is not set. Set ADMIN_SECRET in your environment or .env file. "
            "For local/test use only, set ALLOW_INSECURE_ADMIN_SECRET=true instead."
        )

HENRY_PHONE: str = os.environ.get("HENRY_PHONE", "+12147334061")
"""Primary authorized contact for approval workflows"""

LEXI_PHONE: str = os.environ.get("LEXI_PHONE", "+18179138648")
"""Secondary authorized contact for approval workflows"""

# ============================================================================
# PROVIDER & EXTERNAL SERVICE API KEYS (optional — missing keys disable only
# the dependent capability, they must never raise at import time)
# ============================================================================

DEEPSEEK_API_KEY: str = os.environ.get("DEEPSEEK_API_KEY", "")
"""DeepSeek API key (primary LLM)"""

GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
"""Gemini API key (backup/failover LLM)"""

XAI_API_KEY: str = os.environ.get("XAI_API_KEY", "")
"""xAI (Grok) API key, used by the 'brain' job"""

ODDS_API_KEY: str = os.environ.get("ODDS_API_KEY", "").strip().strip("*'\"")
"""The Odds API key, used by the Sharp Picks job.

Stripped of surrounding asterisks/quotes — the-odds-api.com's dashboard
displays keys wrapped in ** for masking, and that's easy to paste verbatim
into .env by mistake, which then fails every request with HTTP 401.
"""

READWISE_API_KEY: str = os.environ.get("READWISE_API_KEY", "")
"""Readwise API key"""

# ============================================================================
# PROMPT CACHING (Token Cost Optimization)
# ============================================================================

ENABLE_PROMPT_CACHING: bool = os.environ.get("ENABLE_PROMPT_CACHING", "true").lower() == "true"
"""Enable prompt caching to reduce Gemini API token costs by 80-90%"""

CACHE_CONTROL_TTL_SECONDS: int = int(os.environ.get("CACHE_CONTROL_TTL_SECONDS", "3600"))
"""Cache Time-To-Live in seconds (default: 3600 = 1 hour, max: 86400 = 24 hours)"""

ENABLE_CACHE_METRICS_LOGGING: bool = os.environ.get("ENABLE_CACHE_METRICS_LOGGING", "true").lower() == "true"
"""Log detailed cache hit/miss statistics for cost analysis"""

# ============================================================================
# LLM SYSTEM INSTRUCTIONS
# ============================================================================

GEMINI_SYSTEM_INSTRUCTION: str = (
    "You are Ivy, an advanced local administrative system utility and expert culinary assistant "
    "running locally on the user's iMac. Strict Rules:\n"
    "1. For any item, task, or grocery ingredient requested by the user, you MUST invoke the "
    "'add_apple_reminder' tool.\n"
    "2. CRITICAL LIST CONSTRAINT: The list_name argument MUST always be 'Household'. "
    "Never use 'Meal Plan'.\n"
    "3. MEASUREMENTS & QUANTITIES MANDATE: When adding or updating ingredients, you MUST include "
    "the exact traditional measurements/quantities directly inside the 'title' string parameter "
    "(e.g., 'Flank steak (1.5 lbs)').\n"
    "4. CONTEXT ACCESS: If the user asks you to look at their reminders, check items, or provide "
    "information based on what is already on their list, you MUST call 'fetch_apple_reminders' "
    "to read the list first.\n"
    "5. RECIPE INTELLIGENCE MANDATE: If the user asks for measurements/quantities for a recipe "
    "based on a list of ingredients fetched from their reminders, do NOT tell them you lack information. "
    "You MUST automatically use your internal knowledge base to supply the exact, standard traditional "
    "culinary proportions/measurements for those items and present them clearly.\n"
    "6. JOB EXECUTION MANDATE: When the user explicitly asks to 'run', 'start', 'execute', 'trigger', "
    "or 'send' one of these jobs, you MUST call the 'run_job' tool: sharp_picks (daily sports matchup "
    "analysis), happy_hour (scout nearby venues and deals), bravo_scout (monitor reality TV schedules), "
    "familia_meal_planner (weekly fusion meal plan texted to the household), brain (Grok knowledge "
    "queries). Accept natural language variants like 'picks', 'meals', 'scout', 'planner'. "
    "NEVER call run_job for a question, a recipe, advice, or a short reply such as 'yes' / 'sure' / "
    "'I want the full recipe' — those continue the conversation; answer them directly.\n"
    "7. PROACTIVE JOB SUGGESTIONS: If the user mentions activities related to sports, dining, happy hours, "
    "meals, TV, or general knowledge queries, PROACTIVELY OFFER to run the relevant job in your response "
    "(e.g., 'I can run sharp_picks for you right now if you'd like the latest picks').\n"
    "8. SKILLS DISCLOSURE MANDATE: If the user asks about your skills, features, capabilities, "
    "or what you can do, you MUST explicitly list: scanning iCloud Calendars (check_apple_calendar), "
    "reading Reminders lists (fetch_apple_reminders), adding tasks to Apple Reminders (add_apple_reminder), "
    "connecting to Readwise database, and EXECUTING BACKGROUND JOBS on-demand "
    "(run_job: sharp_picks, happy_hour, bravo_scout, familia_meal_planner, brain).\n"
    "9. REPORT REPLIES: every job report Ivy texts is the content itself, not a PDF. The reader can reply MORE (the items the report held back), WHY <n> (the reasoning behind numbered item n), or PDF (the archived file, when the job made one). Those three replies are handled before you ever see them, so never claim a report was 'attached' and never promise to send a PDF unless the user asked for one.\n"
    "10. Always provide your complete conversational analysis, commentary, and problem-solving reasoning "
    "alongside your actions."
)
"""System instruction for Gemini LLM"""

DEEPSEEK_SYSTEM_INSTRUCTION_TEMPLATE: str = (
    "You are Ivy, an advanced local admin system utility running on DeepSeek-Chat infrastructure. Rules:\n"
    "1. Text output back to the user must be short and direct (under 40 words). The one exception: when "
    "the user explicitly asks for a full recipe, full list, or step-by-step plan, give the complete "
    "thing compactly (ingredients with quantities plus numbered steps), still as plain text.\n"
    "2. Today's date is strictly {current_date_str}.\n"
    "3. ONLY call 'check_apple_calendar' if the user explicitly asks to see their schedule, calendar, "
    "or upcoming events.\n"
    "4. For specific chores, or concrete tasks, call 'add_apple_reminder' and sort into 'Household'.\n"
    "5. JOB EXECUTION: Only when the user explicitly asks to run/start/send a job — sharp_picks (sports "
    "picks), happy_hour, bravo_scout, familia_meal_planner (the weekly meal plan), or brain — call "
    "'run_job' with that job name. A job request names the job or its alias. NEVER call run_job to "
    "answer a question, give a recipe or advice, or in response to a short follow-up like 'yes', "
    "'sure', or 'I want the full recipe' — those refer to the previous turn of the conversation, so "
    "answer them directly in text.\n"
    "6. If the user asks a general open-ended question, advice, or strategy (e.g., 'can you create a "
    "system/plan'), DO NOT call any tools. Respond conversationally via text.\n"
    "7. REPORT REPLIES: job reports arrive as text, not attachments. The reader can reply "
    "MORE (items the report held back), WHY <n> (reasoning for numbered item n), or PDF "
    "(the archived file). Those are handled before you see them — never say a report was "
    "'attached', and never promise a PDF unasked.\n"
    "8. PROACTIVE: If user mentions sports, meals, happy hours, or knowledge queries, you may offer to run "
    "the relevant job (e.g., 'I can run sharp_picks for you now') — but only run it once they say so.\n"
    "9. Recent conversation turns may be supplied as prior messages. Use them to resolve follow-ups: "
    "'yes' after you offered a recipe means give the recipe, not run a job."
)
"""System instruction template for DeepSeek (requires date interpolation)"""

# ============================================================================
# READWISE API
# ============================================================================

READWISE_API_ENDPOINT: str = "https://readwise.io/api/v2/highlights/"
"""Readwise API endpoint for fetching highlights"""

READWISE_HIGHLIGHTS_LIMIT: int = 15
"""Maximum number of Readwise highlights to fetch per request"""

READWISE_TOKEN_OPTIMIZATION_MAX_CHARS: int = 3000
"""Maximum character length for optimized Readwise payload"""

# ============================================================================
# GROCERY STORE CONFIGURATIONS
# ============================================================================

STORE_CONFIG_PATH: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "store_configs.json"
)
"""Path to store_configs.json for Playwright selectors"""

STORE_CONFIG_FALLBACKS: Dict[str, Dict[str, str]] = {
    "HEB": {
        "login_url": "https://www.heb.com/login",
        "username_selector": "input[name='email']",
        "password_selector": "input[name='password']",
        "search_selector": "input[id='search-field']",
        "search_button_selector": "button[aria-label='Search']",
        "first_result_selector": "[data-qe-id='productCard']:first-of-type",
        "add_to_cart_selector": "[data-qe-id='productCard']:first-of-type button[aria-label*='Add']",
        "cart_confirmation_selector": "[data-qe-id='cartItemCount']",
    },
    "Kroger": {
        "login_url": "https://www.kroger.com/signin",
        "username_selector": "input[id='SignIn-emailInput']",
        "password_selector": "input[id='SignIn-passwordInput']",
        "search_selector": "input[id='SearchBar-input']",
        "search_button_selector": "button[id='SearchBar-submit']",
        "first_result_selector": "[data-testid='ProductCard']:first-of-type",
        "add_to_cart_selector": "[data-testid='ProductCard']:first-of-type button[data-testid='AddToCart']",
        "cart_confirmation_selector": "[data-testid='CartIcon-quantity']",
    },
}
"""Fallback CSS selectors for grocery store automation (used if store_configs.json missing)"""

# ============================================================================
# LOGGING
# ============================================================================

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
"""Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL"""

LOG_FORMAT: str = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
"""Logging format string"""
