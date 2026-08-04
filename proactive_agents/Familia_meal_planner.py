"""
Familia Meal Planner — Venezuelan-American-Asian Fusion Recipe Generator

Proactively generates family-optimized meal plans for a household of three,
featuring toddler-friendly, macro-balanced fusion cuisine. Integrates with
iMessage notification bus for weekly recipe delivery.

Architecture:
- Environment routing via require_env (dual-brain failover: Gemini → DeepSeek)
- Stateful 48-hour cron gatekeeping (JSON state tracking at ~/openclaw-admin/data/meal_plan_state.json)
- Specialized menu rotation: arepas, cachapas, fusion burgers, macro bowls, sushi, Ooni pizza
- Ultra-condensed SMS text compression for iMessage delivery
"""

import os
import sys
import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Any
from pathlib import Path
from zoneinfo import ZoneInfo

# Add parent directory to path for .ivy module access
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Load .env file using dotenv (if it exists) before any env-dependent imports.
# Use override=False so variables already exported by the shell or launchd win.
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(parent_dir) / ".env", override=False)

from ivy_core import send_imessage, send_imessage_attachment, query_llm, strip_json_fence
from ivy_core import outbox as _outbox
from ivy_core.agent_delivery import (
    aggregate_delivery_status,
    attachment_delivery_status,
    is_delivery_submitted,
    text_delivery_status,
)
from ivy_core.report_fallback import (
    build_attachment_failure_notice,
    format_meal_text,
    split_imessage_content,
)

# PDF formatter for professional reports
sys.path.insert(0, parent_dir)
from picks_formatter import PicksReportFormatter
from config import HENRY_PHONE, LEXI_PHONE  # required env vars — raise at startup if unset

logger = logging.getLogger("ivy.familia_meal_planner")

# ============================================================================
# CONFIGURATION & STATE MANAGEMENT
# ============================================================================

STATE_FILE_PATH = Path.home() / "openclaw-admin" / "data" / "meal_plan_state.json"
STATE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Meal plan target parameters
MEAL_PLAN_CONFIG = {
    "household_size": 3,
    "region": "Dallas/Frisco, TX",
    "cuisine_fusion": ["Venezuelan", "American", "Asian"],
    "core_themes": [
        "stuffed arepas",
        "sweet corn cachapas",
        "street-style fusion burgers",
        "balanced macro bowls",
        "sushi & rolls",
        "Ooni pizza oven specials",
    ],
    "dietary_constraints": [
        "toddler-friendly textures",
        "finger-food adaptable",
        "low sodium scaling",
        "minimal spice (adjustable)",
        "macro-balanced proteins/carbs/fats",
    ],
}

# Alert recipients — values come from required env vars
ALERT_RECIPIENTS = {
    "henry": HENRY_PHONE,
    "lexi": LEXI_PHONE,
}

# Initialize state threshold: July 15, 2026 8am America/Chicago (handles DST
# transitions correctly, unlike a permanently fixed UTC-5 offset).
INIT_THRESHOLD = datetime(2026, 7, 15, 8, 0, 0, tzinfo=ZoneInfo("America/Chicago"))


def initialize_state_file() -> None:
    """Initialize state file if it doesn't exist."""
    if not STATE_FILE_PATH.exists():
        initial_state = {
            "last_run_date": INIT_THRESHOLD.isoformat(),
            "recipe_count": 0,
            "execution_history": []
        }
        with open(STATE_FILE_PATH, 'w') as f:
            json.dump(initial_state, f, indent=2)
        logger.info(f"📋 Initialized state file: {STATE_FILE_PATH}")


def load_state() -> Dict[str, Any]:
    """Load state from JSON file."""
    initialize_state_file()
    try:
        with open(STATE_FILE_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load state: {e}")
        return {
            "last_run_date": INIT_THRESHOLD.isoformat(),
            "recipe_count": 0,
            "execution_history": []
        }


def save_state(state: Dict[str, Any]) -> bool:
    """Atomically save state to JSON and report whether it was durable."""
    temp_path = None
    try:
        STATE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".familia_state-",
            dir=str(STATE_FILE_PATH.parent),
        )
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, STATE_FILE_PATH)
        logger.info("Familia state saved")
        return True
    except Exception as e:
        logger.error("Familia state save failed error=%s", type(e).__name__)
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return False


def check_48h_gate(force: bool = False) -> bool:
    """
    Check if 48 hours have elapsed since last execution.

    force=True bypasses the gate entirely — for explicitly requested ad-hoc
    runs. Scheduled runs must always call this with force=False so the
    48-hour cadence is preserved.

    Returns True if execution should proceed, False if within 48-hour window.
    """
    if force:
        logger.info("⚡ force=True — bypassing 48h gate (ad-hoc run)")
        return True

    state = load_state()
    last_run_str = state.get("last_run_date", INIT_THRESHOLD.isoformat())

    try:
        last_run = datetime.fromisoformat(last_run_str)
    except ValueError:
        logger.warning(f"Invalid last_run_date format: {last_run_str}, forcing execution")
        return True

    now = datetime.now(timezone.utc).astimezone()
    elapsed = now - (last_run if last_run.tzinfo else last_run.replace(tzinfo=timezone.utc).astimezone())

    if elapsed >= timedelta(hours=48):
        logger.info(f"✅ 48h gate passed ({elapsed.total_seconds()/3600:.1f}h elapsed)")
        return True
    else:
        remaining = timedelta(hours=48) - elapsed
        logger.info(f"⏭️  Within 48h window ({remaining.total_seconds()/3600:.1f}h remaining)")
        return False


# ============================================================================
# MEAL PLAN GENERATION
# ============================================================================


def generate_family_meal_plan() -> Dict[str, Any]:
    """
    Generate a Venezuelan-American-Asian fusion meal plan optimized for
    family of three with toddler-friendly adaptations.

    Returns:
        dict: Structured meal plan with recipes and prep instructions.
    """
    logger.info(f"🍳 Generating fusion meal plan for {MEAL_PLAN_CONFIG['household_size']} people")

    meal_plan_prompt = f"""
    Create a weekly family meal plan for {MEAL_PLAN_CONFIG['household_size']} people in {MEAL_PLAN_CONFIG['region']}.

    Cuisine Fusion: {', '.join(MEAL_PLAN_CONFIG['cuisine_fusion'])}

    Meal themes to include:
    {json.dumps(MEAL_PLAN_CONFIG['core_themes'], indent=2)}

    Dietary constraints:
    {json.dumps(MEAL_PLAN_CONFIG['dietary_constraints'], indent=2)}

    Special equipment: Ooni gas-fired pizza oven (optimize 1-2 pizza recipes for this)

    Generate recipes in JSON format with:
    - recipe_name (string)
    - cuisine_origin (Venezuelan/American/Asian)
    - prep_time_minutes (integer, under 30 min)
    - cooking_time_minutes (integer)
    - ingredients (list of simple, available ingredients with quantities)
    - toddler_adaptations (list of modifications for young child)
    - macros (protein_g, carbs_g, fats_g)
    - difficulty_level (easy/medium)

    Include 5-7 recipes total. Return ONLY valid JSON array, no markdown formatting.
    """

    try:
        response = query_llm(
            meal_plan_prompt,
            temperature=0.7,
        )

        if not response or response.strip().lower() == "none":
            logger.warning("LLM returned no recipes")
            return {"status": "error", "recipes": []}

        # Parse JSON response — both providers routinely wrap it in a
        # markdown code fence even when told not to.
        try:
            recipes = json.loads(strip_json_fence(response))
            if not isinstance(recipes, list):
                recipes = [recipes]
        except json.JSONDecodeError:
            logger.error(f"Failed to parse recipe JSON: {response[:200]}")
            return {"status": "error", "recipes": []}

        logger.info(f"✅ Generated {len(recipes)} fusion recipes")

        return {
            "status": "success",
            "recipe_count": len(recipes),
            "recipes": recipes,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Recipe generation failed: {e}")
        return {"status": "error", "recipes": []}


# ============================================================================
# TEXT COMPRESSION FOR SMS
# ============================================================================


def compress_meal_plan(meal_data: Dict[str, Any]) -> str:
    """
    Compress meal plan into ultra-condensed SMS format.
    Strict <160 char per line to prevent multi-bubble splitting.

    Args:
        meal_data: Generated meal plan with recipes

    Returns:
        str: Ultra-condensed notification text
    """
    recipes = meal_data.get("recipes", [])

    if not recipes:
        return ""

    # Build compressed recipe list (ultra-terse format)
    recipe_lines = []
    for recipe in recipes[:5]:  # Top 5 recipes
        name = recipe.get("recipe_name", "Unknown")
        time = recipe.get("prep_time_minutes", 0) + recipe.get("cooking_time_minutes", 0)
        origin = recipe.get("cuisine_origin", "")

        # Abbreviate origin
        origin_short = origin[0] if origin else "?"

        # Format: "🍽️ RecipeName (V) 25m"
        line = f"• {name[:20]} ({origin_short}) {time}m"
        recipe_lines.append(line)

    # Header + recipe list
    header = "📅 Week Menu:\n"
    recipe_text = "\n".join(recipe_lines)

    # Add quick prep tip (one line max)
    tip = f"\n⏱️ Total prep: {sum(r.get('prep_time_minutes', 0) for r in recipes)}min"

    full_text = header + recipe_text + tip

    # Ensure no single line exceeds 160 chars (SMS safe)
    lines = full_text.split("\n")
    safe_lines = []
    for line in lines:
        if len(line) > 155:
            # Truncate with ellipsis
            line = line[:152] + "…"
        safe_lines.append(line)

    final_text = "\n".join(safe_lines)

    logger.debug(f"Compressed meal plan ({len(final_text)} chars):\n{final_text}")
    return final_text


# ============================================================================
# PDF FORMATTER
# ============================================================================


def format_meal_plan_pdf(meal_data: Dict[str, Any]) -> str:
    """
    Generate a professional PDF report of family meal plans.

    Args:
        meal_data: Generated meal plan with recipes

    Returns:
        str: Path to generated PDF
    """
    recipes = meal_data.get("recipes", [])

    formatter = PicksReportFormatter(
        title="Familia Weekly Meal Plan",
        subtitle=f"Venezuelan-American-Asian Fusion | {datetime.now():%A, %B %d, %Y}",
        color_scheme="meals",
    )

    # Format recipes as "picks" for the formatter
    meal_picks = [
        {
            "sport": recipe.get("cuisine_origin", ""),
            "matchup": recipe.get("recipe_name", ""),
            "side": f"{recipe.get('prep_time_minutes', 0) + recipe.get('cooking_time_minutes', 0)} min",
            "odds": f"Difficulty: {recipe.get('difficulty_level', 'medium').title()}",
            "reasoning": ", ".join(recipe.get("toddler_adaptations", ["Family-friendly"])[:2]),
        }
        for recipe in recipes
    ]

    summary = (
        f"Weekly meal plan with {len(recipes)} recipes optimized for a family of three. "
        f"All recipes feature Venezuelan-American-Asian fusion cuisine with toddler-friendly adaptations. "
        f"Includes macro-balanced nutrition and specialized dishes: arepas, cachapas, sushi, and Ooni pizza specials."
    )

    metadata = {
        "pick_count": f"{len(recipes)} recipe(s) for the week",
        "source": "Familia Meal Planner",
        "timestamp": f"{datetime.now():%Y-%m-%d %H:%M}",
    }

    pdf_path = os.path.join(tempfile.gettempdir(), f"meal_plan_{datetime.now():%Y%m%d_%H%M%S}.pdf")
    formatter.generate_pdf(
        filename=pdf_path,
        summary=summary,
        consensus_picks=meal_picks[:3] if len(meal_picks) > 3 else meal_picks,
        other_picks=meal_picks[3:] if len(meal_picks) > 3 else [],
        metadata=metadata,
        headers=["Cuisine", "Recipe", "Time", "Difficulty", "Kid-Friendly Adaptations"],
        col_widths=[0.7, 1.7, 0.6, 0.9, 3.6],
        consensus_heading="🍽️ This Week's Recipes",
        other_heading="More Recipe Ideas",
    )

    return pdf_path


# ============================================================================
# EXECUTION PIPELINE
# ============================================================================


def execute_meal_plan_cycle(send_alert: bool = True, force: bool = False) -> Dict[str, Any]:
    """
    Main execution function: state check → generation → compression → notification dispatch.

    Orchestrates the full meal planner workflow:
    1. Check 48-hour gate (skip if within window, unless force=True)
    2. Generate family meal plan via LLM
    3. Generate PDF report
    4. Route via iMessage to recipients
    5. Update state

    Args:
        send_alert: If True, dispatch notification; if False, dry-run only
        force: If True, bypass the 48-hour gate (for explicit ad-hoc runs)

    Returns:
        dict: Execution summary
    """
    logger.info("=" * 60)
    logger.info("🍽️  Familia Meal Planner Cycle Starting...")
    logger.info("=" * 60)

    result = {
        "status": "pending",
        "gate_passed": False,
        "recipe_count": 0,
        "alert_sent": False,
        "alert_text": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "delivery_status": "not_attempted",
        "deliveries": [],
        "report_ids": [],
    }

    # Step 1: Check 48-hour gate
    logger.info("Step 1/5: Checking 48-hour execution gate...")
    if not check_48h_gate(force=force):
        result["status"] = "skipped"
        result["gate_passed"] = False
        logger.info("⏭️  Skipping execution (within 48-hour window)")
        return result

    result["gate_passed"] = True
    logger.info("✅ Gate check passed")

    # Step 2: Generate meal plan
    logger.info("Step 2/5: Generating fusion meal plan...")
    meal_data = generate_family_meal_plan()

    if meal_data.get("status") != "success":
        result["status"] = "error"
        result["alert_text"] = "Meal plan generation failed"
        logger.error("❌ Generation failed")
        return result

    result["recipe_count"] = meal_data.get("recipe_count", 0)
    logger.info(f"✅ Generated {result['recipe_count']} recipes")

    # Step 3: Generate PDF report
    logger.info("Step 3/5: Generating PDF report...")
    pdf_path = format_meal_plan_pdf(meal_data)
    logger.info(f"✅ PDF generated: {pdf_path}")

    # Persist the execution gate before the first outbound submission.  If
    # durable duplicate/gate state is unavailable, fail closed rather than
    # delivering a plan that a later scheduled run could repeat.
    if send_alert:
        logger.info("Step 4/5: Reserving durable execution state...")
        state = load_state()
        state["last_run_date"] = datetime.now(timezone.utc).astimezone().isoformat()
        state["recipe_count"] = result["recipe_count"]
        state.setdefault("execution_history", []).append({
            "timestamp": result["timestamp"],
            "recipe_count": result["recipe_count"],
            "alert_sent": False,
        })
        state["execution_history"] = state["execution_history"][-10:]
        if save_state(state) is False:
            result["status"] = "error"
            result["alert_text"] = "Meal plan was not sent because execution state could not be saved."
            logger.error("Meal plan delivery skipped because state reservation failed")
            return result

    # Step 5: Dispatch via iMessage with notification
    logger.info("Step 5/5: Routing notification...")
    if result["recipe_count"] == 0:
        logger.info("⏭️  No meal plan content; skipping notification")
        result["alert_sent"] = False
    elif send_alert:
        stats_line = (
            f"🍽️  Familia Meal Plan Ready\n\n"
            f"{result['recipe_count']} recipes (Venezuelan-American-Asian fusion)\n"
            f"Toddler-friendly, macro-balanced\n\n"
        )
        send_results = {}
        attach_results = {}
        deliveries = []
        report_ids = []
        for recipient_name, phone in ALERT_RECIPIENTS.items():
            report_id = None
            attachment_attempted = False
            attachment_status = "not_attempted"
            report_status = "not_attempted"
            delivery = {
                "recipient": recipient_name,
                "channel": "imessage_attachment",
            }
            try:
                # Assign a report ID for tracking.
                report_id = _outbox.make_report_id("familia_meal_planner")
                report_ids.append(report_id)
                delivery["report_id"] = report_id
                local_now = datetime.now(timezone.utc).astimezone()
                content_summary = (
                    f"{result['recipe_count']} recipe(s) — {local_now:%b} {local_now.day}"
                )

                attachment_attempted = True
                receipt = send_imessage_attachment(phone, pdf_path, report_id=report_id)
                attachment_status = attachment_delivery_status(receipt)
                try:
                    _outbox.save_report(
                        report_id, pdf_path,
                        job_name="familia_meal_planner",
                        recipient=phone,
                        content_summary=content_summary,
                    )
                    _r_status = attachment_status
                    _r_attempts = getattr(receipt, "attempts", 1)
                    _outbox.update_report_status(report_id, _r_status, attempts=_r_attempts)
                except Exception as _oe:
                    logger.debug(
                        "Outbox tracking skipped error=%s", type(_oe).__name__
                    )

                if is_delivery_submitted(attachment_status):
                    final_text = stats_line + "Full plan attached (PDF)."
                    report_status = attachment_status
                    delivery["notification_status"] = "unknown"
                    notification_status = text_delivery_status(
                        bool(send_imessage(phone, final_text))
                    )
                    delivery["notification_status"] = notification_status
                    logger.info(
                        "✅ Sent to %s: text=%s attachment=%s",
                        recipient_name,
                        "SUCCESS" if notification_status == "submitted_unverified" else "FAILED",
                        attachment_status,
                    )
                else:
                    # Explicit failure — two-message fallback.
                    notice = build_attachment_failure_notice(
                        report_name="Familia Meal Plan",
                        report_id=report_id,
                        resend_command="RESEND MEAL PLAN",
                        retry_queued=True,
                    )
                    delivery["notice_status"] = "unknown"
                    notice_status = text_delivery_status(
                        bool(send_imessage(phone, notice))
                    )
                    delivery["notice_status"] = notice_status

                    fallback_text = format_meal_text(meal_data)
                    bubbles = split_imessage_content(fallback_text)
                    attempted = 0
                    submitted = 0
                    delivery.update({
                        "fallback_status": "not_attempted",
                        "fallback_messages_attempted": 0,
                        "fallback_messages_submitted": 0,
                    })
                    for bubble in bubbles:
                        attempted += 1
                        delivery["fallback_messages_attempted"] = attempted
                        delivery["fallback_status"] = "unknown"
                        report_status = "unknown"
                        if not send_imessage(phone, bubble):
                            delivery["fallback_status"] = "failed"
                            report_status = "failed"
                            break
                        submitted += 1
                        delivery["fallback_messages_submitted"] = submitted
                    if attempted and submitted == len(bubbles):
                        fallback_status = "submitted_unverified"
                    elif submitted:
                        fallback_status = "partial"
                    else:
                        fallback_status = "failed"
                    report_status = fallback_status
                    delivery.update({
                        "fallback_status": fallback_status,
                        "fallback_messages_attempted": attempted,
                        "fallback_messages_submitted": submitted,
                    })
                    logger.warning(
                        "⚠️ Attachment failed for %s — text fallback %s",
                        recipient_name,
                        "sent" if fallback_status == "submitted_unverified" else "also failed",
                    )
            except Exception as e:
                if attachment_attempted and attachment_status == "not_attempted":
                    attachment_status = "unknown"
                    report_status = "unknown"
                delivery["error_category"] = type(e).__name__
                logger.error(
                    "❌ Failed to send to %s: error=%s",
                    recipient_name,
                    type(e).__name__,
                )
            finally:
                delivery["attachment_status"] = attachment_status
                delivery["status"] = report_status
                deliveries.append(delivery)
                attach_results[recipient_name] = attachment_status
                send_results[recipient_name] = is_delivery_submitted(report_status)

        result["delivery_status"] = aggregate_delivery_status(
            delivery["status"] for delivery in deliveries
        )
        result["alert_sent"] = is_delivery_submitted(result["delivery_status"])
        result["recipients_status"] = send_results
        result["attachment_status"] = attach_results
        result["deliveries"] = deliveries
        result["report_ids"] = report_ids
    else:
        logger.info("⏭️  Dry-run mode: skipping iMessage dispatch")
        result["alert_sent"] = False

    result["status"] = "success"
    logger.info("=" * 60)
    logger.info("🍽️  Meal Planner Cycle Complete")
    logger.info("=" * 60)

    return result


def run(
    *,
    force: bool = False,
    send: bool = True,
    requester: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Standardized entrypoint. force=True bypasses the 48-hour gate for an
    explicitly requested ad-hoc run; scheduled runs must call with
    force=False so the normal cadence is preserved."""
    return execute_meal_plan_cycle(send_alert=send, force=force)


# ============================================================================
# ENTRY POINT
# ============================================================================


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Familia Meal Planner")
    parser.add_argument("--force", action="store_true", help="Bypass the 48-hour gate")
    parser.add_argument("--send", action="store_true", help="Actually send the iMessage/PDF")
    parser.add_argument("--dry-run", action="store_true", help="Generate but don't send (default)")
    parser.add_argument("--scheduled", action="store_true", help="Scheduled run (preserves the 48h gate)")
    cli_args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    result = run(
        force=cli_args.force and not cli_args.scheduled,
        send=cli_args.send and not cli_args.dry_run,
    )
    print(json.dumps(result, indent=2))
