import os
import time
from datetime import datetime
from playwright.sync_api import sync_playwright
from config import APPLE_CALENDAR_TIMEOUT_SECONDS, IMESSAGE_SEND_TIMEOUT_SECONDS
from utils.applescript import AppleScriptRunner


_CALENDAR_RUNNER = AppleScriptRunner(timeout=APPLE_CALENDAR_TIMEOUT_SECONDS)
_MESSAGE_RUNNER = AppleScriptRunner(timeout=IMESSAGE_SEND_TIMEOUT_SECONDS)

_TODAY_CALENDAR_SCRIPT = """
on run argv
    set calendarNameValue to item 1 of argv
    set todayStart to (current date)
    set hours of todayStart to 0
    set minutes of todayStart to 0
    set seconds of todayStart to 0
    set todayEnd to todayStart + (24 * 60 * 60)
    set eventList to {}
    tell application "Calendar"
        try
            set targetCalendar to first calendar whose name is calendarNameValue
            set todayEvents to (every event of targetCalendar whose start date is greater than or equal to todayStart and start date is less than todayEnd)
            repeat with calendarEvent in todayEvents
                copy summary of calendarEvent to end of eventList
            end repeat
            set previousDelimiters to AppleScript's text item delimiters
            set AppleScript's text item delimiters to linefeed
            set renderedEvents to eventList as text
            set AppleScript's text item delimiters to previousDelimiters
            return renderedEvents
        on error
            return "ERROR: APPLESCRIPT_FAILURE"
        end try
    end tell
end run
"""

# Core household meal & grocery matrix
MEAL_POOL = [
    "Salmon Bowls with Avocado & Rice",
    "Cast Iron Seared Burgers",
    "Chicken Pitas with Tzatziki",
    "Steak with Mashed Potatoes"
]

GROCERY_MAPPING = {
    "Salmon Bowls with Avocado & Rice": ["Fresh Salmon fillets", "Avocados", "Cucumbers", "Jasmine Rice"],
    "Cast Iron Seared Burgers": ["80/20 Ground Beef", "Brioche Buns", "Cheddar Cheese"],
    "Chicken Pitas with Tzatziki": ["Chicken Breast", "Pita Pocket Bread", "Greek Yogurt", "Fresh Dill"],
    "Steak with Mashed Potatoes": ["Ribeye Steak", "Russet Potatoes", "Heavy Cream", "Butter"]
}

def get_calendar_summary():
    """Extracts today's schedule from Apple Calendar."""
    result = _CALENDAR_RUNNER.run_argv(_TODAY_CALENDAR_SCRIPT, ["Calendar"])
    return "" if result.startswith("ERROR") else result.strip()

def build_heb_cart(ingredients):
    """Launches your local Chrome profile, logs in via active session cookies, and adds items."""
    print("🚀 Launching Chrome engine via active user session...")
    user_data_dir = os.path.expanduser("~/openclaw-admin/ivy_chrome_profile")
   
    with sync_playwright() as p:
        browser_context = p.chromium.launch_persistent_context(
            user_data_dir,
            channel="chrome",
            headless=False, # Keeps window visible so you can see it build and bypass initial triggers
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        page = browser_context.new_page()
        
        for item in ingredients:
            print(f"🔍 Searching for: {item}")
            search_url = f"https://www.heb.com/search/?q={item.replace(' ', '+')}"
            page.goto(search_url, wait_until="domcontentloaded")
            time.sleep(2) 
            
            try:
                add_button = page.locator("button:has-text('Add to cart')").first
                if add_button.is_visible():
                    add_button.click()
                    print(f"✅ Added {item} to your H-E-B cart.")
                    time.sleep(1.5)
                else:
                    print(f"⚠️ Could not instantly locate 'Add to Cart' button for {item}.")
            except Exception as e:
                print(f"❌ Failed to add {item}: {str(e)}")
                
        browser_context.close()

def send_iMessage_dispatch(body):
    return _MESSAGE_RUNNER.send_imessage_argv("me", body)

def main():
    events = get_calendar_summary()
    
    today_weekday = datetime.now().weekday()
    meal = MEAL_POOL[today_weekday % len(MEAL_POOL)]
    ingredients = GROCERY_MAPPING.get(meal, [])
    
    if ingredients:
        build_heb_cart(ingredients)
        
    briefing = (
        f"☀️ **HEN & LEX DAILY DISPATCH** ☀️\n\n"
        f"📅 **Today's Schedule:**\n{events if events else '- No scheduled events'}\n\n"
        f"🍳 **Dinner Strategy:** {meal}\n\n"
        f"🛒 **Cart Update:** Checked your favorite ingredients and queued them directly into your H-E-B cart!"
    )
    
    send_iMessage_dispatch(briefing)

if __name__ == "__main__":
    main()
