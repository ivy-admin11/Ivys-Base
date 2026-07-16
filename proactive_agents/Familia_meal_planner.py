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

# Native .env auto-loader (mirrors sports_bettor.py) so this agent works
# standalone — anchored to parent_dir, not the CWD, and never clobbers vars
# already exported in the shell/launchd.
_ENV_PATH = os.path.join(parent_dir, ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, "r") as _f:
        for _line in _f:
            if "=" in _line and not _line.strip().startswith("#"):
                _k, _v = _line.strip().split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from ivy_core import send_imessage, send_imessage_attachment, query_llm, strip_json_fence

# PDF formatter for professional reports
sys.path.insert(0, parent_dir)
from picks_formatter import PicksReportFormatter

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

# Alert recipients
ALERT_RECIPIENTS = {
    "henry": os.environ.get("HENRY_PHONE", "+12147334061"),
    "lexi": os.environ.get("LEXI_PHONE", "+18179138648"),
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


def save_state(state: Dict[str, Any]) -> None:
    """Save state to JSON file."""
    try:
        with open(STATE_FILE_PATH, 'w') as f:
            json.dump(state, f, indent=2)
        logger.info(f"💾 State saved: {STATE_FILE_PATH}")
    except Exception as e:
        logger.error(f"Failed to save state: {e}")


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
            "generated_at": datetime.utcnow().isoformat(),
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
        "timestamp": datetime.utcnow().isoformat(),
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

    # Step 4: Dispatch via iMessage with notification
    logger.info("Step 4/5: Routing notification...")
    if result["recipe_count"] == 0:
        logger.info("⏭️  No meal plan content; skipping notification")
        result["alert_sent"] = False
    elif send_alert:
        # Send the PDF attachment first, then a status line that reflects
        # what actually happened — never claim "attached" up front.
        stats_line = (
            f"🍽️  Familia Meal Plan Ready\n\n"
            f"{result['recipe_count']} recipes (Venezuelan-American-Asian fusion)\n"
            f"Toddler-friendly, macro-balanced\n\n"
        )
        send_results = {}
        attach_results = {}
        for recipient_name, phone in ALERT_RECIPIENTS.items():
            try:
                attached = send_imessage_attachment(phone, pdf_path)
                if attached:
                    final_text = stats_line + "Full plan attached (PDF)."
                else:
                    final_text = stats_line + f"Report generated, but attachment delivery failed. Path: {pdf_path}"
                success = send_imessage(phone, final_text)
                send_results[recipient_name] = success
                attach_results[recipient_name] = attached
                logger.info(
                    f"✅ Sent to {recipient_name}: text={'SUCCESS' if success else 'FAILED'}, "
                    f"attachment={'SUCCESS' if attached else 'FAILED'}"
                )
            except Exception as e:
                send_results[recipient_name] = False
                attach_results[recipient_name] = False
                logger.error(f"❌ Failed to send to {recipient_name}: {e}")

        result["alert_sent"] = any(send_results.values())
        result["recipients_status"] = send_results
        result["attachment_status"] = attach_results
    else:
        logger.info("⏭️  Dry-run mode: skipping iMessage dispatch")
        result["alert_sent"] = False

    # Step 5: Update state file
    logger.info("Step 5/5: Updating state...")
    state = load_state()
    state["last_run_date"] = datetime.now(timezone.utc).astimezone().isoformat()
    state["recipe_count"] = result["recipe_count"]
    state["execution_history"].append({
        "timestamp": result["timestamp"],
        "recipe_count": result["recipe_count"],
        "alert_sent": result["alert_sent"]
    })
    # Keep only last 10 executions
    state["execution_history"] = state["execution_history"][-10:]
    save_state(state)
    logger.info("✅ State updated")

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
