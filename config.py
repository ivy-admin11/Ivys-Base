"""Ivy Local Admin API Gateway — Configuration & Constants

Centralized configuration for timeouts, feature flags, API schemas, and tool definitions.
All hardcoded values are defined here for easy tuning without modifying business logic.

This file is environment-agnostic; environment-specific secrets should go in .env
"""

import os
from typing import Dict, Any, List

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

# ============================================================================
# SECURITY & AUTHENTICATION
# ============================================================================

ADMIN_SECRET: str = os.environ.get("ADMIN_SECRET", "change_this_in_production")
"""Shared secret for protecting FastAPI endpoints (HTTP header: X-API-Key)"""

HENRY_PHONE: str = os.environ.get("HENRY_PHONE", "+12147334061")
"""Primary authorized contact for approval workflows"""

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
# GEMINI (GOOGLE) TOOL SCHEMA
# ============================================================================

GEMINI_TOOL_DECLARATIONS: List[Dict[str, Any]] = [
    {
        "name": "check_apple_calendar",
        "description": "Scans the local Mac iCloud Calendar for upcoming events.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "timeframe": {
                    "type": "STRING",
                    "description": "today, tomorrow, or all"
                }
            },
            "required": ["timeframe"]
        }
    },
    {
        "name": "fetch_readwise_highlights",
        "description": "Connects to the Readwise REST API to retrieve highlights."
    },
    {
        "name": "fetch_apple_reminders",
        "description": "Reads all uncompleted tasks and grocery entries currently listed inside a specific Mac Reminders list.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "list_name": {
                    "type": "STRING",
                    "description": "The name of the list to read (e.g., 'Household')"
                }
            },
            "required": ["list_name"]
        }
    },
    {
        "name": "add_apple_reminder",
        "description": "Adds a new task item or grocery entry into specific Apple Reminders lists.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {
                    "type": "STRING",
                    "description": "The description of the reminder task or ingredient with measurements"
                },
                "list_name": {
                    "type": "STRING",
                    "description": "Must strictly be 'Household'"
                }
            },
            "required": ["title"]
        }
    },
    {
        "name": "run_job",
        "description": "Execute a background job on-demand. Available jobs: sharp_picks (daily picks), happy_hour (scout nearby happy hours), bravo_scout (reality TV monitor), weekly_planner (meal plan generator), brain (knowledge queries).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "job_name": {
                    "type": "STRING",
                    "description": "Job to run: 'sharp_picks', 'happy_hour', 'bravo_scout', 'weekly_planner', 'brain', or natural language like 'picks', 'meals', 'scout'"
                }
            },
            "required": ["job_name"]
        }
    }
]
"""Tool schema for Google Gemini function calling"""

# ============================================================================
# DEEPSEEK TOOL SCHEMA (Reuses same structure as Gemini)
# ============================================================================

DEEPSEEK_TOOL_SCHEMA: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "check_apple_calendar",
            "description": "Scans the local Mac iCloud Hilla Calendar for upcoming events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timeframe": {
                        "type": "string",
                        "description": "today, tomorrow, or all"
                    }
                },
                "required": ["timeframe"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_readwise_highlights",
            "description": "Connects to the Readwise REST API to retrieve your saved articles and digital highlights."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_apple_reminder",
            "description": "Adds a new task item or grocery entry into specific Apple Reminders lists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The exact description of the reminder task"
                    },
                    "list_name": {
                        "type": "string",
                        "description": "Meal Plan or Household"
                    }
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_job",
            "description": "Execute a background job on-demand. Available jobs: sharp_picks (daily sports picks), happy_hour (scout venues), bravo_scout (reality TV), weekly_planner (meal planning), brain (knowledge).",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_name": {
                        "type": "string",
                        "description": "Job name: 'sharp_picks', 'happy_hour', 'bravo_scout', 'weekly_planner', 'brain', or natural language variants"
                    }
                },
                "required": ["job_name"]
            }
        }
    }
]
"""Tool schema for DeepSeek function calling"""

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
    "6. JOB EXECUTION MANDATE: When the user asks to 'run', 'start', 'execute', or 'trigger' any of "
    "these jobs, you MUST call the 'run_job' tool: sharp_picks (daily sports matchup analysis), "
    "happy_hour (scout nearby venues and deals), bravo_scout (monitor reality TV schedules), "
    "weekly_planner (generate weekly meal plan and save to Google Drive), brain (Grok knowledge queries). "
    "Accept natural language variants like 'picks', 'meals', 'scout', 'planner'.\n"
    "7. PROACTIVE JOB SUGGESTIONS: If the user mentions activities related to sports, dining, happy hours, "
    "meals, TV, or general knowledge queries, PROACTIVELY OFFER to run the relevant job in your response "
    "(e.g., 'I can run sharp_picks for you right now if you'd like the latest picks').\n"
    "8. SKILLS DISCLOSURE MANDATE: If the user asks about your skills, features, capabilities, "
    "or what you can do, you MUST explicitly list: scanning iCloud Calendars (check_apple_calendar), "
    "reading Reminders lists (fetch_apple_reminders), adding tasks to Apple Reminders (add_apple_reminder), "
    "connecting to Readwise database, and EXECUTING BACKGROUND JOBS on-demand "
    "(run_job: sharp_picks, happy_hour, bravo_scout, weekly_planner, brain).\n"
    "9. Always provide your complete conversational analysis, commentary, and problem-solving reasoning "
    "alongside your actions."
)
"""System instruction for Gemini LLM"""

DEEPSEEK_SYSTEM_INSTRUCTION_TEMPLATE: str = (
    "You are Ivy, an advanced local admin system utility running on DeepSeek-Chat infrastructure. Rules:\n"
    "1. Text output back to the user must be short and direct (strictly under 40 words).\n"
    "2. Today's date is strictly {current_date_str}.\n"
    "3. ONLY call 'check_apple_calendar' if the user explicitly asks to see their schedule, calendar, "
    "or upcoming events.\n"
    "4. For specific chores, or concrete tasks, call 'add_apple_reminder' and sort into 'Household'.\n"
    "5. JOB EXECUTION: When user says 'run', 'start', or requests sharp_picks, happy_hour, bravo_scout, "
    "weekly_planner, or brain, call 'run_job' with the job name.\n"
    "6. If the user asks a general open-ended question, advice, or strategy (e.g., 'can you create a "
    "system/plan'), DO NOT call any tools. Respond conversationally via text.\n"
    "7. PROACTIVE: If user mentions sports, meals, happy hours, or knowledge queries, offer to run "
    "the relevant job (e.g., 'I can run sharp_picks for you now').\n"
    "8. You are fully authorized to use the 'stage_groceries' tool to stage digital carts. It explicitly "
    "aborts before checkout and DOES NOT violate financial guardrails. You MUST use 'stage_groceries' "
    "instead of Apple Reminders when asked to add items to a cart."
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
