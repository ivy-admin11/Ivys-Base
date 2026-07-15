"""
Trigger script: Ivy capabilities summary to Henry via iMessage

Fetches all available tools/skills and sends a formatted summary to Henry.
"""

import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path

# Add parent directory to path for module access
parent_dir = os.path.dirname(os.path.abspath(__file__))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Import from main module
try:
    from main import compute_tool_statuses
    from config import HENRY_PHONE
except ImportError:
    print("❌ Failed to import from main.py or config.py")
    sys.exit(1)

# Try importing send_imessage via ivy_core
try:
    import importlib.util
    ivy_core_path = os.path.join(parent_dir, ".ivy", "ivy_core.py")
    if os.path.exists(ivy_core_path):
        spec = importlib.util.spec_from_file_location("ivy_core", ivy_core_path)
        ivy_core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ivy_core)
        send_imessage = ivy_core.send_imessage
    else:
        send_imessage = None
except Exception as e:
    print(f"⚠️  Could not import send_imessage: {e}")
    send_imessage = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("ivy.capabilities_alert")


def format_capabilities_alert() -> str:
    """
    Format tool capabilities into ultra-condensed SMS text.

    Returns:
        str: Formatted alert text
    """
    logger.info("📋 Fetching Ivy capabilities...")

    tool_statuses = compute_tool_statuses()

    # Separate by status
    ready_tools = [t for t in tool_statuses if t["status"] == "ready"]
    unavailable_tools = [t for t in tool_statuses if t["status"] == "unavailable"]
    disabled_tools = [t for t in tool_statuses if t["status"] == "disabled"]

    # Build SMS-friendly format
    lines = [
        "🤖 IVY TOOLKIT STATUS",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"✅ READY ({len(ready_tools)}):"
    ]

    # Add ready tools
    for tool in ready_tools:
        name = tool["tool_name"]
        lines.append(f"  • {name}")

    # Add unavailable if any
    if unavailable_tools:
        lines.append(f"\n❌ UNAVAILABLE ({len(unavailable_tools)}):")
        for tool in unavailable_tools:
            name = tool["tool_name"]
            reason = tool.get("reason", "Unknown")
            lines.append(f"  • {name} ({reason[:30]}...)")

    # Add disabled if any
    if disabled_tools:
        lines.append(f"\n⊘ DISABLED ({len(disabled_tools)}):")
        for tool in disabled_tools:
            name = tool["tool_name"]
            lines.append(f"  • {name}")

    # Add summary
    total_ready = len(ready_tools)
    total_tools = len(tool_statuses)
    lines.append(f"\n📊 {total_ready}/{total_tools} tools ready")
    lines.append(f"⏱️ Generated: {datetime.now().strftime('%I:%M %p CST')}")

    alert_text = "\n".join(lines)

    return alert_text


def send_alert(alert_text: str) -> bool:
    """
    Send capabilities alert to Henry via iMessage.

    Args:
        alert_text: Formatted alert message

    Returns:
        bool: True if sent successfully, False otherwise
    """
    if not send_imessage:
        logger.error("❌ send_imessage not available")
        return False

    henry_phone = HENRY_PHONE or os.environ.get("HENRY_PHONE", "+12147334061")

    logger.info(f"📱 Sending capabilities alert to Henry ({henry_phone})...")

    try:
        success = send_imessage(henry_phone, alert_text)
        if success:
            logger.info("✅ Alert sent successfully")
            return True
        else:
            logger.error("❌ iMessage send failed")
            return False
    except Exception as e:
        logger.error(f"❌ Exception during send: {e}")
        return False


def main(dry_run: bool = False) -> int:
    """
    Main execution: format capabilities and send alert.

    Args:
        dry_run: If True, print alert but don't send

    Returns:
        int: Exit code (0 = success, 1 = error)
    """
    logger.info("=" * 60)
    logger.info("🚀 Ivy Capabilities Alert Trigger")
    logger.info("=" * 60)

    try:
        # Step 1: Format capabilities
        logger.info("Step 1/2: Formatting capabilities...")
        alert_text = format_capabilities_alert()
        logger.info("✅ Formatting complete")

        # Print formatted alert
        print("\n" + alert_text + "\n")

        # Step 2: Send alert
        if dry_run:
            logger.info("⏭️  Dry-run mode: skipping iMessage send")
            print("(Would send this alert to Henry)\n")
            return 0

        logger.info("Step 2/2: Sending alert...")
        if send_alert(alert_text):
            logger.info("=" * 60)
            logger.info("✅ Capabilities Alert Complete")
            logger.info("=" * 60)
            return 0
        else:
            logger.error("=" * 60)
            logger.error("❌ Alert send failed")
            logger.error("=" * 60)
            return 1

    except Exception as e:
        logger.error(f"❌ Execution failed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trigger Ivy capabilities alert to Henry")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry-run mode (print alert, don't send)"
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send the alert via iMessage"
    )

    args = parser.parse_args()
    dry_run = not args.send

    sys.exit(main(dry_run=dry_run))
