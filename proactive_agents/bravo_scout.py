#!/usr/bin/env python3
"""Bravo & Entertainment Scout — a real, gossipy morning brief texted to Lexi.

Four live sources, none of them invented:

* Entertainment RSS (Deadline, Variety, TVLine, Us Weekly, THR, Reality Tea,
  E! News, Decider, Page Six) filtered to Lexi's watchlist and Bravo-reality
  keywords, then ranked by how much drama the headline actually carries
  (see ``drama_score``) so the juiciest item leads the brief.
* Bravo subreddit feeds for overnight fan chatter.
* TVmaze for what is genuinely airing next: each watchlist show's next
  episode, plus every Reality episode on the US schedule for the coming week,
  with season premieres flagged.
* That same schedule doubles as the candidate pool for "if you liked X, try
  Y" — every recommendation is a real show with a real network and a real
  airdate, rather than a title the model made up.

The LLM only ever *arranges* this material. It is told, repeatedly, that it
may not add a title, a date, a feud or a network that isn't in the payload;
if the morning is quiet it must say so rather than pad.

On-demand only: no launchd plist is installed, so it runs when dispatched
(`./ivy run bravo`, or `python -m proactive_agents.bravo_scout`).

Set IVY_DRY_RUN=1 (or pass --dry-run) to print the brief instead of texting it.
"""
import os
import sys
import json
import socket
import time
from datetime import date, datetime, timedelta

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
import requests

from ivy_core import require_env, send_imessage, query_llm
from ivy_core.report_fallback import split_imessage_content

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
               "vanderpump", "below deck", "summer house", "andy cohen", "reality tv",
               "the valley", "watch what happens live", "peacock reality", "bravocon"]

# Exact TVmaze titles for the shows Lexi actually follows. TARGET_WATCHLIST
# holds loose keywords for headline matching ("The Real Housewives" matches
# any franchise); these are the specific series to look up an airdate for.
SCHEDULE_WATCHLIST = [
    "The Real Housewives of Orange County",
    "The Real Housewives of Beverly Hills",
    "The Real Housewives of Atlanta",
    "The Real Housewives of New Jersey",
    "The Real Housewives of Potomac",
    "Vanderpump Rules",
    "Summer House",
    "Below Deck",
    "The Valley",
    "Watch What Happens Live with Andy Cohen",
    "Abbott Elementary",
    "Shrinking",
]

NEWS_FEEDS = [
    ("Deadline", "https://deadline.com/feed/"),
    ("Variety", "https://variety.com/feed/"),
    ("TVLine", "https://tvline.com/feed/"),
    ("Us Weekly", "https://www.usmagazine.com/feed/"),
    ("Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
    # Added 2026-09-01 for actual gossip volume — the five trades above are
    # mostly business/renewal news. Verified live: all four return entries.
    ("Reality Tea", "https://www.realitytea.com/feed/"),
    ("E! News", "https://www.eonline.com/syndication/feeds/rssfeeds/topstories.xml"),
    ("Decider", "https://decider.com/feed/"),
    ("Page Six", "https://pagesix.com/feed/"),
]
SOCIAL_FEEDS = [
    ("r/BravoRealHousewives", "https://www.reddit.com/r/BravoRealHousewives/hot/.rss"),
    ("r/vanderpumprules", "https://www.reddit.com/r/vanderpumprules/hot/.rss"),
    ("r/realhousewives", "https://www.reddit.com/r/realhousewives/hot/.rss"),
    ("r/BelowDeck", "https://www.reddit.com/r/BelowDeck/hot/.rss"),
    ("r/SummerHouseBravo", "https://www.reddit.com/r/SummerHouseBravo/hot/.rss"),
    ("r/TheValleyBravo", "https://www.reddit.com/r/TheValleyBravo/hot/.rss"),
]
# Reddit rate-limits this IP to roughly one .rss request per cooldown window:
# on 2026-09-01 six rapid calls all returned 429, and 2.5 s spacing plus a 6 s
# retry still got only the first through — 42 s of sleeping for zero extra
# posts. So take as many posts as possible from the first subreddit that
# answers, stop once there are enough, and only move down the list when a feed
# actually fails. In the common case that is a single request and no delay.
_REDDIT_DELAY_S = 15.0
_SOCIAL_TARGET_POSTS = 10
_SOCIAL_PER_FEED = 10

TVMAZE_BASE = "https://api.tvmaze.com"
TVMAZE_TIMEOUT_S = 15
# TVmaze allows ~20 calls per 10 s and needs no API key.
_TVMAZE_DELAY_S = 0.35
UPCOMING_DAYS = 7

# Networks whose reality output is closest to what Lexi already watches, used
# to prioritise which upcoming episodes make the (capped) LLM payload.
BRAVO_FAMILY_NETWORKS = ("bravo", "peacock", "hayu", "e!", "mtv", "vh1", "tlc", "netflix")

MAX_NEWS_ITEMS = 24
MAX_SOCIAL_ITEMS = 18
MAX_UPCOMING_ITEMS = 20
MAX_SIMILAR_CANDIDATES = 14
# Bubble size for the finished brief. The whole point of this rewrite is a
# longer read, so it is split at paragraph boundaries rather than truncated.
BUBBLE_MAX_CHARS = 1200

# Headline drama weighting. Reality coverage is mostly renewals and casting;
# these are the words that separate "Bravo renews X" from the item Lexi
# actually wants first. Weights are deliberately coarse — this only has to
# produce a sensible ordering, not a score anyone reads.
DRAMA_KEYWORDS = {
    5: ("arrested", "lawsuit", "sues", "divorce", "split", "cheating", "affair",
        "restraining order", "fired", "axed", "quits", "exits", "arrest"),
    3: ("feud", "slams", "shades", "drags", "clap back", "claps back", "blasts",
        "accuses", "calls out", "breaks silence", "scandal", "explosive",
        "reunion", "walks off", "storms off", "confronts", "leaked"),
    2: ("drama", "shocking", "reveals", "admits", "confirms", "responds",
        "addresses", "rumors", "engaged", "pregnant", "dating", "breakup",
        "fight", "tears", "apologizes"),
    1: ("premiere", "trailer", "cast", "casting", "returns", "renewed",
        "season", "teases", "first look"),
}
# ========================================================


def _matches(text):
    t = (text or "").lower()
    keys = [w.lower() for w in TARGET_WATCHLIST] + _EXTRA_KEYS
    return any(k in t for k in keys)


def drama_score(text):
    """Score a headline's gossip value. Higher = juicier, ties keep feed order.

    Sums the weight of every distinct drama keyword present, so a headline
    that is both a feud and a firing outranks one that is only a renewal.
    """
    t = (text or "").lower()
    return sum(weight for weight, words in DRAMA_KEYWORDS.items()
               for word in words if word in t)


def fetch_entertainment_feeds():
    """Pull real entertainment RSS, keep watchlist-relevant items, juiciest first."""
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
                        items.append({
                            "source": source,
                            "headline": title,
                            "drama": drama_score(title + " " + summary),
                        })
                        hits += 1
            print(f"   ↳ {source}: {hits} relevant of {len(d.entries)} entries")
        except Exception as ex:
            print(f"   ⚠️ feed failed {source}: {ex}")
    items.sort(key=lambda i: i["drama"], reverse=True)
    return items[:MAX_NEWS_ITEMS]


def fetch_social_sentiment():
    """Trending Bravo subreddit posts, juiciest first.

    Stops at the first feed that yields enough posts rather than marching
    through every subreddit — see _REDDIT_DELAY_S for why.
    """
    print("📱 Checking Bravo subreddit feeds for overnight drama...")
    posts = []
    attempted = 0
    for source, url in SOCIAL_FEEDS:
        if len(posts) >= _SOCIAL_TARGET_POSTS:
            break
        if attempted:
            time.sleep(_REDDIT_DELAY_S)
        attempted += 1
        for e in _fetch_reddit(source, url)[:_SOCIAL_PER_FEED]:
            title = (e.get("title") or "").strip()
            if title:
                posts.append({"source": source, "title": title, "drama": drama_score(title)})
    posts.sort(key=lambda p: p["drama"], reverse=True)
    return posts[:MAX_SOCIAL_ITEMS]


def _fetch_reddit(source, url):
    """One subreddit feed. A 429 is reported and skipped, never retried —
    Reddit's cooldown is far longer than any retry worth blocking a text on."""
    try:
        d = feedparser.parse(url, agent=_UA)
        if getattr(d, "status", None) == 429:
            print(f"   ↳ {source}: rate-limited by Reddit, skipping")
            return []
        print(f"   ↳ {source}: {len(d.entries)} posts")
        return d.entries
    except Exception as ex:
        print(f"   ⚠️ social feed failed {source}: {ex}")
        return []


def _tvmaze_get(path, params):
    resp = requests.get(
        TVMAZE_BASE + path, params=params,
        headers={"User-Agent": "ivy-scout/1.0"}, timeout=TVMAZE_TIMEOUT_S,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _network_name(show):
    return ((show.get("network") or show.get("webChannel")) or {}).get("name") or "TV"


def _pretty_date(iso_date):
    """'2026-09-03' -> 'Wed 9/3'. Returns the input unchanged if unparseable."""
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return iso_date
    return f"{d.strftime('%a')} {d.month}/{d.day}"


def fetch_watchlist_schedule():
    """Next episode for each show on SCHEDULE_WATCHLIST, via TVmaze.

    Shows between seasons legitimately have no next episode; those come back
    with next_airdate None and are reported as "no date announced" rather
    than quietly dropped, because "when is Summer House back" is exactly the
    question this section exists to answer.
    """
    print(f"🗓️  Looking up next episodes for {len(SCHEDULE_WATCHLIST)} shows (TVmaze)...")
    out = []
    for index, title in enumerate(SCHEDULE_WATCHLIST):
        if index:
            time.sleep(_TVMAZE_DELAY_S)
        try:
            show = _tvmaze_get("/singlesearch/shows", {"q": title, "embed": "nextepisode"})
            if not show:
                continue
            nxt = (show.get("_embedded") or {}).get("nextepisode") or {}
            entry = {
                "show": show.get("name") or title,
                "network": _network_name(show),
                "status": show.get("status"),
                "next_airdate": nxt.get("airdate"),
                # Pre-formatted so the model can only ever echo a friendly
                # date; handing it raw ISO produced "on Bravo 2026-09-03".
                "when": _pretty_date(nxt.get("airdate")) if nxt.get("airdate") else None,
                "next_episode": nxt.get("name"),
                "season": nxt.get("season"),
                "number": nxt.get("number"),
            }
            out.append(entry)
            when = entry["next_airdate"] or "no date announced"
            print(f"   ↳ {entry['show'][:40]:42} {entry['network']:12} next: {when}")
        except Exception as ex:
            print(f"   ⚠️ TVmaze lookup failed for {title}: {ex}")
    return out


def fetch_upcoming_reality(days=UPCOMING_DAYS):
    """Every Reality episode on the US schedule for the next ``days`` days.

    Feeds both the "what else is on" section and the recommendation pool, so
    every suggested show is real and has a real airdate.
    """
    print(f"📡 Pulling the next {days} days of US reality TV (TVmaze)...")
    items, seen = [], set()
    for offset in range(days):
        day = (date.today() + timedelta(days=offset)).isoformat()
        if offset:
            time.sleep(_TVMAZE_DELAY_S)
        try:
            episodes = _tvmaze_get("/schedule", {"country": "US", "date": day}) or []
        except Exception as ex:
            print(f"   ⚠️ schedule fetch failed for {day}: {ex}")
            continue
        for ep in episodes:
            show = ep.get("show") or {}
            if show.get("type") != "Reality":
                continue
            key = (show.get("name"), day)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "show": show.get("name"),
                "network": _network_name(show),
                "date": day,
                "when": _pretty_date(day),
                "episode": ep.get("name"),
                "season": ep.get("season"),
                "number": ep.get("number"),
                "is_premiere": ep.get("number") == 1,
            })
    items.sort(key=lambda i: (i["date"], i["show"] or ""))
    print(f"   ↳ {len(items)} reality episode(s) across the week")
    return items


def build_similar_pool(upcoming, watchlist_names):
    """Real shows Lexi is NOT already watching, as a grounded pool for
    'if you liked X, try Y'. Premieres first — a show she can start from
    episode one is a better recommendation than one nine weeks deep."""
    watching = {n.lower() for n in watchlist_names}
    pool, seen = [], set()
    for item in upcoming:
        name = item.get("show") or ""
        low = name.lower()
        if not name or low in seen:
            continue
        if any(w in low or low in w for w in watching):
            continue
        seen.add(low)
        pool.append({
            "show": name,
            "network": item["network"],
            "date": item["date"],
            "when": item["when"],
            "is_premiere": item["is_premiere"],
        })
    pool.sort(key=lambda p: (not p["is_premiere"], p["date"]))
    return pool[:MAX_SIMILAR_CANDIDATES]


def _payload_upcoming(upcoming):
    """The slice of the week's reality that goes to the model: premieres
    first, then Bravo-adjacent networks, then everything else by date. A flat
    date sort filled the cap with cooking and home-reno reruns."""
    def rank(item):
        network = (item.get("network") or "").lower()
        family = next((i for i, n in enumerate(BRAVO_FAMILY_NETWORKS) if n in network),
                      len(BRAVO_FAMILY_NETWORKS))
        return (not item["is_premiere"], family, item["date"])

    return sorted(upcoming, key=rank)[:MAX_UPCOMING_ITEMS]


def _plain_digest(news, social, watchlist, upcoming, similar):
    """Fallback text if the LLM is unavailable — real data, no synthesis."""
    lines = ["☕ Ivy's Morning Tea", ""]
    if news:
        lines.append("💅 The Tea:")
        lines += [f"• {n['headline']} ({n['source']})" for n in news[:8]]
        lines.append("")
    dated = [w for w in watchlist if w.get("next_airdate")]
    if dated:
        lines.append("📺 Your shows, next up:")
        for w in sorted(dated, key=lambda x: x["next_airdate"])[:8]:
            ep = f" — {w['next_episode']}" if w.get("next_episode") else ""
            lines.append(f"• {w['show']}: {w['when']} on {w['network']}{ep}")
        lines.append("")
    premieres = [u for u in upcoming if u["is_premiere"]][:6]
    if premieres:
        lines.append("🆕 Premiering this week:")
        lines += [f"• {p['show']} — {p['when']} on {p['network']}" for p in premieres]
        lines.append("")
    if similar:
        lines.append("👀 Also airing, if you want something new:")
        lines += [f"• {s['show']} ({s['network']}, {s['when']})" for s in similar[:6]]
        lines.append("")
    if social:
        lines.append("📱 Reddit buzz:")
        lines += [f"• {s['title']}" for s in social[:6]]
    if not any([news, social, watchlist, upcoming]):
        lines.append("Quiet news morning — nothing fresh on your shows. ☕")
    return "\n".join(lines).strip()


def _build_prompt(news, social, watchlist, upcoming, similar):
    # Split the watchlist so the model doesn't spend a paragraph listing ten
    # shows that are between seasons — it named all ten on the first run.
    airing = [w for w in watchlist if w.get("next_airdate")]
    airing.sort(key=lambda w: w["next_airdate"])
    undated = [w["show"] for w in watchlist if not w.get("next_airdate")]
    return (
        "You are Ivy, a sharp, pop-culture-obsessed entertainment assistant writing "
        "Lexi's morning brief. She loves Bravo and reality drama, so lead with the "
        "messiest real story you were given.\n\n"
        "HARD RULES — you are arranging real data, not writing fiction:\n"
        "• Use ONLY the shows, people, networks, dates and storylines in the DATA below.\n"
        "• Never invent a title, a feud, a breakup, an airdate or a network. If a "
        "section's data is empty, skip that section entirely and say the morning is "
        "quiet on that front.\n"
        "• Every airdate you give must match the data exactly.\n"
        "• You may add your own voice, opinions and reactions — that's the fun part — "
        "but not new facts.\n\n"
        "FORMAT — a proper read, roughly 200-320 words, plain text for iMessage. "
        "Use these sections, in this order, skipping any with no data. Put a blank "
        "line between sections so it splits cleanly:\n"
        "💅 THE TEA — the 3-5 juiciest headlines, gossip-column voice, one punchy "
        "line each. Lead with the messiest.\n"
        "📺 ON YOUR LIST — when her shows are next on, using the 'when' field and "
        "the network. If shows she follows have no date yet, name at most two of "
        "them in a single short line and move on — do not list them all.\n"
        "🆕 NEW THIS WEEK — season premieres and notable reality airing in the next "
        "week, using each item's 'when' field.\n"
        "👀 IF YOU LIKED... — 2-3 'if you're into X, try Y' picks drawn ONLY from the "
        "RECOMMENDATION POOL, each with its network and 'when', and one line on why "
        "it scratches the same itch as a specific show she already watches.\n"
        "📱 THE GROUP CHAT — what Bravo Reddit is arguing about, if anything.\n\n"
        "Write every date as the 'when' value you were given (e.g. 'Wed 9/3'). "
        "Never print a raw YYYY-MM-DD date. Emojis welcome. No markdown headers, "
        "no asterisks, no links.\n\n"
        f"DATA — HEADLINES (already ranked by drama, juiciest first):\n{json.dumps(news, indent=2)}\n\n"
        f"DATA — HER SHOWS WITH A CONFIRMED NEXT EPISODE:\n{json.dumps(airing, indent=2)}\n\n"
        f"DATA — HER SHOWS WITH NO DATE ANNOUNCED (mention at most two):\n{json.dumps(undated, indent=2)}\n\n"
        f"DATA — REALITY AIRING THIS WEEK:\n{json.dumps(_payload_upcoming(upcoming), indent=2)}\n\n"
        f"DATA — RECOMMENDATION POOL (she does NOT watch these):\n{json.dumps(similar, indent=2)}\n\n"
        f"DATA — REDDIT BUZZ:\n{json.dumps(social, indent=2)}"
    )


def execute_brief(*, send_alert=True):
    """Build the morning brief from live sources and (optionally) text it.

    Returns a JSON-serializable result dict — never raises for an empty news
    morning, which is a legitimate outcome rather than a failure.
    """
    print("🚀 Bravo & Entertainment Scout — real RSS + TVmaze morning brief...")
    news = fetch_entertainment_feeds()
    social = fetch_social_sentiment()
    watchlist = fetch_watchlist_schedule()
    upcoming = fetch_upcoming_reality()
    similar = build_similar_pool(upcoming, [w["show"] for w in watchlist] + SCHEDULE_WATCHLIST)
    print(f"🧾 {len(news)} headline(s), {len(social)} social post(s), "
          f"{len(watchlist)} tracked show(s), {len(upcoming)} upcoming episode(s), "
          f"{len(similar)} recommendation candidate(s).")

    llm_used = False
    if not any([news, social, watchlist, upcoming]):
        # Nothing real to report — send a short honest note (never fabricate).
        final_text = "☕ Ivy's Morning Tea:\n\nQuiet news morning — nothing fresh on your shows today. 💤"
        status = "quiet"
    else:
        brief = query_llm(_build_prompt(news, social, watchlist, upcoming, similar))
        if not brief or brief.strip() == _BRAIN_ERROR or brief.strip().startswith("System error"):
            print("⚠️ LLM unavailable — falling back to plain digest.")
            final_text = _plain_digest(news, social, watchlist, upcoming, similar)
            status = "digest_fallback"
        else:
            final_text = f"☕ Ivy's Morning Tea:\n\n{brief.strip()}"
            llm_used = True
            status = "ok"

    bubbles = split_imessage_content(final_text, max_chars=BUBBLE_MAX_CHARS)

    sent = False
    if not send_alert:
        print(f"\n----- DRY RUN ({len(final_text)} chars, {len(bubbles)} bubble(s), not sent) -----\n")
        print(final_text)
    else:
        sent = all(send_imessage(LEXI_PHONE, b) for b in bubbles)
        print(f"✅ Morning brief texted to Lexi ({len(bubbles)} bubble(s))." if sent else "⚠️ Send failed.")
        if not sent:
            status = "send_failed"

    return {
        "status": status,
        "news_count": len(news),
        "social_count": len(social),
        "watchlist_count": len(watchlist),
        "upcoming_count": len(upcoming),
        "recommendation_count": len(similar),
        "llm_used": llm_used,
        "sent": sent,
        "bubbles": len(bubbles),
        "chars": len(final_text),
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
