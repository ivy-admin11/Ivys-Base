#!/usr/bin/env python3
"""Bravo & Entertainment Scout — real RSS morning brief texted to Lexi.

Pulls live entertainment/TV RSS feeds (Deadline, Variety, TVLine, Us Weekly,
THR) plus Bravo subreddit feeds, keeps only items matching Lexi's watchlist /
Bravo-reality keywords, and asks Gemini to synthesize a fun, gossipy morning
brief — using ONLY the real headlines found (no fabrication). Sends to Lexi's
phone.

On-demand only: no launchd plist is installed for this job, so it runs when
dispatched (`./ivy run bravo`, or `python -m proactive_agents.bravo_scout`).

Set IVY_DRY_RUN=1 (or pass --dry-run) to print the brief instead of texting it.
"""
import os
import sys
import json
import socket
from datetime import datetime

# --- make this script standalone (mirrors sports_bettor.py) ---
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, "r") as _f:
        for _line in _f:
            if "=" in _line and not _line.strip().startswith("#"):
                _k, _v = _line.strip().split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import feedparser

from ivy_core import require_env, send_imessage, query_llm

# Sentinel query_llm returns when both brains are unreachable (ivy_core/llm.py).
_BRAIN_ERROR = "System error: both primary and backup language models are unavailable."

# ========================= CONFIG =========================
LEXI_PHONE = require_env("LEXI_PHONE")
DRY_RUN = os.environ.get("IVY_DRY_RUN") == "1"
socket.setdefaulttimeout(15)
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 ivy-scout/1.0")

TARGET_WATCHLIST = [
    "The Real Housewives", "Vanderpump Rules", "Summer House",
    "Below Deck", "Abbott Elementary", "Shrinking",
    "Tehran", "The Blacklist",
]
# Broader Bravo/reality terms so we still catch relevant drama.
_EXTRA_KEYS = ["bravo", "housewives", "rhoa", "rhobh", "rhonj", "rhoc", "rhop",
               "vanderpump", "below deck", "summer house", "andy cohen", "reality tv"]

NEWS_FEEDS = [
    ("Deadline", "https://deadline.com/feed/"),
    ("Variety", "https://variety.com/feed/"),
    ("TVLine", "https://tvline.com/feed/"),
    ("Us Weekly", "https://www.usmagazine.com/feed/"),
    ("Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
]
SOCIAL_FEEDS = [
    ("r/BravoRealHousewives", "https://www.reddit.com/r/BravoRealHousewives/hot/.rss"),
    ("r/vanderpumprules", "https://www.reddit.com/r/vanderpumprules/hot/.rss"),
]
# ========================================================


def _matches(text):
    t = (text or "").lower()
    keys = [w.lower() for w in TARGET_WATCHLIST] + _EXTRA_KEYS
    return any(k in t for k in keys)


def fetch_entertainment_feeds():
    """Pull real entertainment RSS and keep only watchlist-relevant items."""
    print(f"📺 Scanning {len(NEWS_FEEDS)} entertainment feeds for: {', '.join(TARGET_WATCHLIST)}...")
    items, seen = [], set()
    for source, url in NEWS_FEEDS:
        try:
            d = feedparser.parse(url, agent=_UA)
            hits = 0
            for e in d.entries[:30]:
                title = (e.get("title") or "").strip()
                summary = e.get("summary") or ""
                if title and _matches(title + " " + summary):
                    k = title.lower()
                    if k not in seen:
                        seen.add(k)
                        items.append({"source": source, "headline": title})
                        hits += 1
            print(f"   ↳ {source}: {hits} relevant of {len(d.entries)} entries")
        except Exception as ex:
            print(f"   ⚠️ feed failed {source}: {ex}")
    return items[:12]


def fetch_social_sentiment():
    """Pull trending posts from Bravo subreddits (best-effort; Reddit may throttle)."""
    print("📱 Checking Bravo subreddit feeds for overnight drama...")
    posts = []
    for source, url in SOCIAL_FEEDS:
        try:
            d = feedparser.parse(url, agent=_UA)
            for e in d.entries[:6]:
                title = (e.get("title") or "").strip()
                if title:
                    posts.append({"source": source, "title": title})
            print(f"   ↳ {source}: {len(d.entries)} posts")
        except Exception as ex:
            print(f"   ⚠️ social feed failed {source}: {ex}")
    return posts[:10]


def _plain_digest(news, social):
    """Fallback text if the LLM is unavailable — just the real headlines."""
    lines = ["☕ Ivy's Morning Tea", ""]
    if news:
        lines.append("📺 Headlines:")
        lines += [f"• {n['headline']} ({n['source']})" for n in news[:6]]
    if social:
        lines.append("")
        lines.append("📱 Reddit buzz:")
        lines += [f"• {s['title']}" for s in social[:4]]
    if not news and not social:
        lines.append("Quiet news morning — nothing fresh on your shows. ☕")
    return "\n".join(lines)


def execute_brief(*, send_alert=True):
    """Build the morning brief from live RSS and (optionally) text it.

    Returns a JSON-serializable result dict — never raises for an empty news
    morning, which is a legitimate outcome rather than a failure.
    """
    print("🚀 Bravo & Entertainment Scout — real RSS morning brief...")
    news = fetch_entertainment_feeds()
    social = fetch_social_sentiment()
    print(f"🧾 {len(news)} news item(s), {len(social)} social post(s).")

    llm_used = False
    if not news and not social:
        # Nothing real to report — send a short honest note (never fabricate).
        final_text = "☕ Ivy's Morning Tea:\n\nQuiet news morning — nothing fresh on your shows today. 💤"
        status = "quiet"
    else:
        prompt = (
            "You are Ivy, a sharp, pop-culture-obsessed entertainment assistant. "
            "Using ONLY the REAL headlines and posts below (do NOT invent anything; "
            "if it's sparse, keep it short and say it's a quiet morning), synthesize an "
            "ultra-concise, fun, gossipy morning brief text for Lexi. Lead with the "
            "biggest Bravo/reality drama, then her scripted shows. Emojis welcome; keep "
            "it SMS-short.\n\n"
            f"HEADLINES:\n{json.dumps(news, indent=2)}\n\n"
            f"REDDIT BUZZ:\n{json.dumps(social, indent=2)}"
        )
        brief = query_llm(prompt)
        if not brief or brief.strip() == _BRAIN_ERROR or brief.strip().startswith("System error"):
            print("⚠️ LLM unavailable — falling back to plain headline digest.")
            final_text = _plain_digest(news, social)
            status = "digest_fallback"
        else:
            final_text = f"☕ Ivy's Morning Tea:\n\n{brief.strip()}"
            llm_used = True
            status = "ok"

    sent = False
    if not send_alert:
        print("\n----- DRY RUN (not sent) -----\n" + final_text)
    else:
        sent = send_imessage(LEXI_PHONE, final_text)
        print("✅ Morning brief texted to Lexi." if sent else "⚠️ Send failed.")
        if not sent:
            status = "send_failed"

    return {
        "status": status,
        "news_count": len(news),
        "social_count": len(social),
        "llm_used": llm_used,
        "sent": sent,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "brief": final_text,
    }


def run(*, force=False, send=True, requester=None, request_id=None):
    """Standardized entrypoint — `force` has no gating effect here (this job has
    no duplicate-suppression window); kept for interface consistency with the
    other proactive agents. IVY_DRY_RUN=1 suppresses sending."""
    return execute_brief(send_alert=send and not DRY_RUN)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Bravo & Entertainment Scout")
    parser.add_argument("--force", action="store_true", help="No-op here (no duplicate-suppression window)")
    parser.add_argument("--send", action="store_true", help="Actually send the iMessage")
    parser.add_argument("--dry-run", action="store_true", help="Build the brief but don't send (default)")
    parser.add_argument("--scheduled", action="store_true", help="Scheduled run")
    cli_args = parser.parse_args()

    result = run(
        force=cli_args.force,
        send=cli_args.send and not cli_args.dry_run,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "brief"}, indent=2))
