"""
Send Ivy Skills Overview to Henry via iMessage

Generates a comprehensive, example-rich explanation of all Ivy capabilities
and sends it via iMessage.
"""

import os
import sys
import importlib.util
from datetime import datetime

parent_dir = os.path.dirname(os.path.abspath(__file__))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

try:
    ivy_core_path = os.path.join(parent_dir, ".ivy", "ivy_core.py")
    spec = importlib.util.spec_from_file_location("ivy_core", ivy_core_path)
    ivy_core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ivy_core)
    send_imessage = ivy_core.send_imessage
except Exception as e:
    print(f"Failed to import send_imessage: {e}")
    sys.exit(1)

from config import HENRY_PHONE

HENRY = HENRY_PHONE or os.environ.get("HENRY_PHONE", "+12147334061")

skills_message = """🤖 IVY SKILLS & EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━

📱 LOCAL MAC TOOLS:
━━━━━━━━━━━━━━━━━━━━━━━━━

1️⃣ IMESSAGE_SEND
Send texts to any contact.
Example: "Text Mom 'Thanks for dinner!'"
→ Ivy routes via AppleScript

2️⃣ CHECK_APPLE_CALENDAR
Scan your calendar for events.
Examples:
• "What's on my calendar today?"
• "Show me tomorrow's schedule"
• "List all upcoming events"
→ Reads iCloud Hilla Calendar

3️⃣ APPLE_REMINDERS (Fetch & Add)
Manage your task lists.
Examples:
• "What do I need to do?"
  → Shows uncompleted tasks
• "Add 'Buy milk' to Household"
  → Auto-categorizes (Household/Meal Plan)
• "Remember to call Henry Friday"
  → Creates reminder

━━━━━━━━━━━━━━━━━━━━━━━━━

🧠 AI REASONING:
━━━━━━━━━━━━━━━━━━━━━━━━━

4️⃣ GEMINI (Primary Brain)
Google Gemini 2.5-flash with smart caching.
Examples:
• "Plan a dinner for 3 people"
• "What's a good recipe with chicken?"
• "Explain quantum computing"
• "Help me debug this code"
→ 80-90% faster/cheaper on repeated topics

5️⃣ DEEPSEEK (Failover Brain)
Auto-activates if Gemini is down.
Same examples work, seamless handoff.

━━━━━━━━━━━━━━━━━━━━━━━━━

🎤 VOICE ASSISTANT (NEW):
━━━━━━━━━━━━━━━━━━━━━━━━━

6️⃣ VOICE_ASSISTANT
Session-based conversation with memory.
Examples:
• Session persists across messages
• "What's my typical dinner?"
  → Remembers previous context
• "Change it to something spicy"
  → Knows what "it" is
→ Cache optimized (reuses context)

━━━━━━━━━━━━━━━━━━━━━━━━━

📖 KNOWLEDGE:
━━━━━━━━━━━━━━━━━━━━━━━━━

7️⃣ READWISE_HIGHLIGHTS
Pull your saved articles.
Example: "Show me my saved articles"
→ Fetches from Readwise API

━━━━━━━━━━━━━━━━━━━━━━━━━

🤖 PROACTIVE AGENTS (Auto):
━━━━━━━━━━━━━━━━━━━━━━━━━

8️⃣ HAPPY_HOUR_SCOUT
Every Sunday 12pm CST → texts best happy hours
(Hudson House, wine specials, oysters, martinis)

9️⃣ FAMILIA_MEAL_PLANNER
Every 2 days (48h gate) → family meal recipes
(Arepas, cachapas, sushi, Ooni pizza, toddler-friendly)

🔟 SPORTS_BETTOR
Every 30min during game windows → daily picks
(KBO, MLB, NFL with live score context)

1️⃣1️⃣ SPORTS_DASHBOARD
Live scoreboard & analytics

1️⃣2️⃣ WATCHLIST_MONITOR
Track assets & alerts

More: WEEKLY_PLANNER, MARKET_ANALYST, etc.

━━━━━━━━━━━━━━━━━━━━━━━━━

🔧 HOW TO USE:

Text or Ask:
✓ "Ivy, what's for dinner?"
✓ "Add to my reminders"
✓ "Check my calendar"
✓ "What happy hours are nearby?"
✓ "Explain [anything]"

API (if running uvicorn):
POST /voice/query
GET /capabilities
GET /health

━━━━━━━━━━━━━━━━━━━━━━━━━
Last Updated: """ + datetime.now().strftime("%I:%M %p CST") + """
━━━━━━━━━━━━━━━━━━━━━━━━━
"""

def main():
    print("📤 Sending Ivy skills overview to Henry...\n")
    print(skills_message)
    print("\n📱 Dispatching via iMessage...\n")

    try:
        success = send_imessage(HENRY, skills_message)
        if success:
            print(f"✅ Skills overview sent to {HENRY}")
            return 0
        else:
            print(f"❌ Failed to send to {HENRY}")
            return 1
    except Exception as e:
        print(f"❌ Error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
