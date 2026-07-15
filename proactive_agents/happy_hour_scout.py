"""
Happy Hour Scout — Lifestyle Exploration Engine

Proactively discovers local food/beverage specials, patio updates, and premium
cocktail features in the Frisco, TX area (75035 zip). Integrates with the
iMessage notification bus for direct delivery to end users.

Architecture:
- Environment routing via require_env (dual-brain failover: Gemini → DeepSeek)
- Hardcoded Frisco, TX target parameters (zip 75035, 75mi radius)
- fetch_local_specials() stub for web/search tool integration
- Text compression interface for condensed alert delivery
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

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

from ivy_core import require_env, send_imessage, send_imessage_attachment, query_llm, strip_json_fence

# PDF formatter for professional reports
sys.path.insert(0, parent_dir)
from picks_formatter import PicksReportFormatter

logger = logging.getLogger("ivy.happy_hour_scout")

# ============================================================================
# SCOUT TARGET PARAMETERS (Frisco, TX)
# ============================================================================

SCOUT_TARGET = {
    "region": "Frisco, TX",
    "zip_code": "75035",
    "search_radius_miles": 75,
    "focus_categories": [
        "patio dining",
        "cocktails & margaritas",
        "upscale casual",
        "happy hour specials",
        "seasonal menu updates",
    ],
    "preferred_cuisines": [
        "mexican",
        "tex-mex",
        "american",
        "steakhouse",
        "fusion",
    ],
}

# Default alert recipients (pulled from environment or fallback)
ALERT_RECIPIENTS = {
    "henry": os.environ.get("HENRY_PHONE", "+12147334061"),
    "lexi": os.environ.get("LEXI_PHONE", "+18179138648"),
}

# ============================================================================
# DISCOVERY LOOP STUB
# ============================================================================


def fetch_local_specials() -> Dict[str, Any]:
    """
    Sweep for recent patio menu shifts, premium cocktail features, and
    updated operations schedules in the Frisco/Dallas area.

    Constructs targeted search queries for:
    - Premium margarita, martini, and tequila cocktail specials
    - Half-price wine by the glass and premium wine happy hour offerings
    - Fresh oyster specials and upscale dining promotions
    - Hudson House Frisco and other upscale casual venues
    - Live patio dining hours and seasonal menu adjustments
    - Craft cocktails and fine dining happy hour schedules

    Returns:
        dict: Structured discovery payload with keys:
            - venues: list of discovered venues with details
            - specials: list of active specials/promotions
            - updates: timestamp of last discovery sweep
            - source_confidence: data freshness score (0.0-1.0)
    """
    logger.info(
        f"🔍 Initiating discovery sweep for {SCOUT_TARGET['region']} ({SCOUT_TARGET['zip_code']})"
    )

    discovery_payload = {
        "venues": [],
        "specials": [],
        "updates": datetime.utcnow().isoformat(),
        "source_confidence": 0.0,
    }

    try:
        # Build geographic + venue-type search queries
        # Expanded footprint: Hudson House Frisco + existing locations
        # Broadened keywords: margaritas, wine, oysters, martinis, upscale casual
        search_queries = [
            "margarita happy hour specials Frisco TX 75035",
            "tequila cocktail lounge patio Frisco Legacy West",
            "upscale casual restaurant patio hours Frisco TX",
            "happy hour menu updates North DFW Dallas",
            "artisanal tequila drink specials Frisco dining",
            "Hudson House Frisco happy hour specials cocktails",
            "Hudson House Frisco patio dining hours updates",
            "half-price wine by the glass happy hour Frisco Dallas",
            "oyster specials happy hour Frisco upscale dining",
            "martini cocktail specials Frisco TX 75035 happy hour",
            "upscale casual Happy Hour Dallas Frisco fine dining",
            "premium wine happy hour North Dallas Frisco Legacy",
            "fresh oysters happy hour specials Dallas Frisco",
            "craft cocktails martini specials Frisco upscale casual",
        ]

        venues_found = []
        specials_found = []

        for query in search_queries:
            logger.debug(f"Searching: {query}")
            try:
                # Attempt to fetch search results (would integrate with real search API)
                # For now, construct from LLM context awareness
                search_result = query_llm(
                    f"Find current happy hour specials and patio updates for: {query}. "
                    f"Return as JSON with 'venue_name', 'special_detail', 'patio_hours' fields. "
                    f"Only include venues actively open today with fresh updates. "
                    f"Focus on margarita/tequila features and outdoor dining.",
                    temperature=0.3,
                )

                if search_result and search_result.strip().lower() != "none":
                    # Parse LLM response for venues and specials
                    logger.debug(f"Search result: {search_result[:200]}")

                    # Extract venue data — both providers routinely wrap it
                    # in a markdown code fence even when told not to.
                    try:
                        result_data = json.loads(strip_json_fence(search_result))
                        if isinstance(result_data, dict):
                            result_data = [result_data]
                        elif not isinstance(result_data, list):
                            result_data = []

                        for item in result_data:
                            if isinstance(item, dict):
                                venue_name = item.get("venue_name") or item.get("name")
                                special = item.get("special_detail") or item.get("detail")

                                if venue_name and special:
                                    venues_found.append(
                                        {
                                            "name": venue_name,
                                            "category": "Mexican/Tex-Mex/Cocktail",
                                            "region": "Frisco, TX",
                                        }
                                    )
                                    specials_found.append(
                                        {"venue": venue_name, "detail": special}
                                    )
                    except (json.JSONDecodeError, ValueError):
                        logger.debug("Could not parse structured response, continuing")

            except Exception as e:
                logger.debug(f"Search query '{query}' encountered: {e}")
                continue

        # Deduplicate venues and specials
        seen_venues = set()
        deduped_venues = []
        for v in venues_found:
            if v["name"] not in seen_venues:
                seen_venues.add(v["name"])
                deduped_venues.append(v)

        seen_specials = set()
        deduped_specials = []
        for s in specials_found:
            spec_key = f"{s['venue']}:{s['detail']}"
            if spec_key not in seen_specials:
                seen_specials.add(spec_key)
                deduped_specials.append(s)

        discovery_payload["venues"] = deduped_venues[:5]  # Top 5 venues
        discovery_payload["specials"] = deduped_specials[:3]  # Top 3 specials for alert
        discovery_payload["source_confidence"] = (
            0.7 if deduped_specials else 0.0
        )  # Confidence score

        logger.info(
            f"✅ Discovery complete: {len(deduped_venues)} venues, "
            f"{len(deduped_specials)} specials found"
        )

    except Exception as e:
        logger.error(f"❌ fetch_local_specials failed: {e}", exc_info=True)

    logger.debug(f"Discovery payload: {len(discovery_payload['specials'])} specials")
    return discovery_payload


# ============================================================================
# PDF FORMATTER
# ============================================================================


def format_happy_hour_pdf(discovery_data: Dict[str, Any]) -> str:
    """
    Generate a professional PDF report of happy hour specials.

    Args:
        discovery_data: Structured discovery payload from fetch_local_specials()

    Returns:
        str: Path to generated PDF
    """
    specials = discovery_data.get("specials", [])
    venues = discovery_data.get("venues", [])

    formatter = PicksReportFormatter(
        title="Happy Hour Scout Discovery",
        subtitle=f"Frisco/Dallas Happy Hour Specials | {datetime.now():%A, %B %d, %Y}",
        color_scheme="happy_hour",
    )

    # Format specials as "picks" for the formatter
    special_picks = [
        {
            "sport": special.get("venue", "").split()[0],  # Venue name (truncated)
            "matchup": special.get("venue", ""),  # Full venue
            "side": "Happy Hour Special",
            "odds": "ACTIVE",
            "reasoning": special.get("detail", "Check venue for details"),
        }
        for special in specials[:10]  # Top 10 specials
    ]

    summary = (
        f"Ivy discovered {len(venues)} venues with {len(specials)} active happy hour specials "
        f"in the Frisco/Dallas area. Specials include margaritas, wine by the glass, oysters, "
        f"martinis, and upscale casual fine dining promotions."
    )

    metadata = {
        "pick_count": f"{len(specials)} special(s) across {len(venues)} venue(s)",
        "source": "Happy Hour Scout",
        "timestamp": f"{datetime.now():%Y-%m-%d %H:%M}",
    }

    pdf_path = f"/tmp/happy_hour_{datetime.now():%Y%m%d_%H%M%S}.pdf"
    formatter.generate_pdf(
        filename=pdf_path,
        summary=summary,
        consensus_picks=special_picks[:5] if len(special_picks) > 5 else special_picks,
        other_picks=special_picks[5:] if len(special_picks) > 5 else [],
        metadata=metadata,
    )

    return pdf_path


# ============================================================================
# TEXT COMPRESSION INTERFACE
# ============================================================================


def compress_alert(discovery_data: Dict[str, Any]) -> str:
    """
    Process discovery results into ultra-condensed SMS notification string.
    Targets <160 character limit for efficient single-segment iMessage delivery.

    Returns empty string if no fresh updates to prevent blank notifications.

    Args:
        discovery_data: Structured discovery payload from fetch_local_specials()

    Returns:
        str: Ultra-condensed alert text (or empty string if no specials)
    """
    specials = discovery_data.get("specials", [])

    # Return empty string if no fresh updates found
    if not specials:
        return ""

    # Extract top special (most compact format)
    top_special = specials[0]
    venue = top_special.get("venue", "").split()[0]  # First word only
    detail = top_special.get("detail", "")

    # Truncate detail to fit SMS budget
    max_detail_len = 110
    if len(detail) > max_detail_len:
        detail = detail[: max_detail_len - 3] + "…"

    # Ultra-condensed format: emoji + venue + special in <160 chars
    alert_text = f"🍹 {venue}: {detail}"

    # Ensure we stay well under SMS limit
    if len(alert_text) > 160:
        # Further truncation if needed
        alert_text = alert_text[:157] + "…"

    logger.debug(
        f"Compressed alert ({len(alert_text)} chars): {alert_text}"
    )
    return alert_text


# ============================================================================
# EXECUTION SKELETON
# ============================================================================


def execute_scout_cycle(send_alert: bool = True) -> Dict[str, Any]:
    """
    Main execution function: discovery → compression → notification dispatch.

    Orchestrates the full happy hour scout workflow:
    1. Trigger fetch_local_specials() to discover venues/updates
    2. Run compression pipeline on results
    3. Route condensed alert via send_imessage if enabled

    Args:
        send_alert: If True, dispatch notification via iMessage; if False, dry-run only

    Returns:
        dict: Execution summary with keys:
            - status: "success" or "error"
            - discovery_count: number of venues/specials found
            - alert_sent: whether notification was dispatched
            - alert_text: the compressed message (or error detail)
            - timestamp: execution timestamp
    """
    logger.info("=" * 60)
    logger.info("🎯 Happy Hour Scout Cycle Starting...")
    logger.info("=" * 60)

    result = {
        "status": "pending",
        "discovery_count": 0,
        "alert_sent": False,
        "alert_text": "",
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        # Step 1: Fetch latest specials
        logger.info("Step 1/3: Discovering local specials...")
        discovery_data = fetch_local_specials()

        result["discovery_count"] = len(discovery_data.get("specials", []))
        logger.info(
            f"✅ Discovery complete: {result['discovery_count']} specials found"
        )

        # Step 2: Generate PDF report
        logger.info("Step 2/3: Generating PDF report...")
        pdf_path = format_happy_hour_pdf(discovery_data)
        logger.info(f"✅ PDF generated: {pdf_path}")

        # Step 3: Dispatch via iMessage with notification
        logger.info("Step 3/3: Routing notification...")
        if result["discovery_count"] == 0:
            logger.info("⏭️  No specials found; skipping notification")
            result["alert_sent"] = False
        elif send_alert:
            # Send the PDF attachment to each recipient, then a status line
            # that reflects what actually happened — never claim "attached"
            # before knowing the attachment was actually delivered.
            stats_line = (
                f"🍹 Happy Hour Scout Report\n\n"
                f"{result['discovery_count']} specials across Frisco/Dallas\n"
                f"Includes: wine, oysters, martinis, upscale dining\n\n"
            )
            send_results = {}
            attach_results = {}
            for recipient_name, phone in ALERT_RECIPIENTS.items():
                try:
                    attached = send_imessage_attachment(phone, pdf_path)
                    if attached:
                        final_text = stats_line + "Full report attached (PDF)."
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

        result["status"] = "success"
        logger.info("=" * 60)
        logger.info("🎯 Scout Cycle Complete")
        logger.info("=" * 60)

    except Exception as e:
        result["status"] = "error"
        result["alert_text"] = f"Scout Error: {str(e)}"
        logger.error(f"❌ Scout execution failed: {e}", exc_info=True)

    return result


def run(
    *,
    force: bool = False,
    send: bool = True,
    requester: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Standardized entrypoint — forwards to execute_scout_cycle. `force` has
    no gating effect here (this job has no duplicate-suppression window);
    kept for interface consistency with the other proactive agents."""
    return execute_scout_cycle(send_alert=send)


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Happy Hour Scout")
    parser.add_argument("--force", action="store_true", help="No-op here (no duplicate-suppression window)")
    parser.add_argument("--send", action="store_true", help="Actually send the iMessage/PDF")
    parser.add_argument("--dry-run", action="store_true", help="Discover but don't send (default)")
    parser.add_argument("--scheduled", action="store_true", help="Scheduled run")
    cli_args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    result = run(
        force=cli_args.force,
        send=cli_args.send and not cli_args.dry_run,
    )
    print(json.dumps(result, indent=2))
