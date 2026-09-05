"""
Trigger script: Ivy capabilities summary to Henry via iMessage

Fetches all available tools/skills and sends a formatted summary to Henry.
"""

import argparse
import os
import sys
import logging
from datetime import datetime

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

# Try importing send_imessage via the version-controlled ivy_core package
try:
    from ivy_core import send_imessage
except Exception as e:
    print(f"⚠️  Could not import send_imessage: {e}")
    send_imessage = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("ivy.capabilities_alert")


# Statuses this alert knows how to bucket. Anything else still has to reach
# Henry — a tool that vanishes from the message but stays in the denominator
# reads as "8/9 ready" with no way to tell which one is missing or why.
KNOWN_STATUSES = ("ready", "unavailable", "disabled")

_REASON_LIMIT = 30


def _tool_name(tool: dict) -> str:
    return str(tool.get("tool_name") or "unknown")


def _reason_text(tool: dict, limit: int = _REASON_LIMIT) -> str:
    """Short, printable reason for a tool's status.

    compute_tool_statuses() uses ``reason: None`` for tools that have nothing to
    explain, so this must never assume a string. It also only marks the text as
    truncated when it actually truncated it.
    """
    reason = tool.get("reason")
    text = str(reason).strip() if reason is not None else ""
    if not text:
        text = "Unknown"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def format_capabilities_alert() -> str:
    """
    Format tool capabilities into ultra-condensed SMS text.

    Returns:
        str: Formatted alert text
    """
    logger.info("📋 Fetching Ivy capabilities...")

    tool_statuses = compute_tool_statuses()

    # Separate by status
    ready_tools = [t for t in tool_statuses if t.get("status") == "ready"]
    unavailable_tools = [t for t in tool_statuses if t.get("status") == "unavailable"]
    disabled_tools = [t for t in tool_statuses if t.get("status") == "disabled"]
    other_tools = [t for t in tool_statuses if t.get("status") not in KNOWN_STATUSES]

    # Build SMS-friendly format
    lines = [
        "🤖 IVY TOOLKIT STATUS",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"✅ READY ({len(ready_tools)}):"
    ]

    # Add ready tools
    for tool in ready_tools:
        lines.append(f"  • {_tool_name(tool)}")

    # Add unavailable if any
    if unavailable_tools:
        lines.append(f"\n❌ UNAVAILABLE ({len(unavailable_tools)}):")
        for tool in unavailable_tools:
            lines.append(f"  • {_tool_name(tool)} ({_reason_text(tool)})")

    # Add disabled if any
    if disabled_tools:
        lines.append(f"\n⊘ DISABLED ({len(disabled_tools)}):")
        for tool in disabled_tools:
            lines.append(f"  • {_tool_name(tool)}")

    # Anything with an unrecognised status still gets named, not dropped.
    if other_tools:
        lines.append(f"\n⚠️ OTHER ({len(other_tools)}):")
        for tool in other_tools:
            status = str(tool.get("status") or "unknown")
            lines.append(f"  • {_tool_name(tool)} [{status}] ({_reason_text(tool)})")

    # Add summary
    total_ready = len(ready_tools)
    total_tools = len(tool_statuses)
    lines.append(f"\n📊 {total_ready}/{total_tools} tools ready")
    # Label the timestamp with the timezone this machine is actually in; the
    # old hardcoded "CST" was wrong for half the year and on any other host.
    lines.append(f"⏱️ Generated: {datetime.now().astimezone().strftime('%I:%M %p %Z')}")

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

    henry_phone = HENRY_PHONE

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trigger Ivy capabilities alert to Henry")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run mode (print alert, don't send)"
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send the alert via iMessage"
    )
    return parser


def resolve_dry_run(args: argparse.Namespace) -> bool:
    """Dry-run unless --send was passed, and always dry-run if --dry-run was.

    --dry-run used to be declared with default=True, which made args.dry_run
    constant and the flag dead: `--dry-run --send` sent a real iMessage.
    An explicit --dry-run now wins over --send.
    """
    if getattr(args, "dry_run", False):
        return True
    return not getattr(args, "send", False)


if __name__ == "__main__":
    cli_args = build_parser().parse_args()
    sys.exit(main(dry_run=resolve_dry_run(cli_args)))
