"""One brief in the morning, one in the evening — not seven separate texts.

Seven scheduled digests would have landed four times between 06:30 and 09:05
and three more between 16:05 and 19:55. Added to Sharp Picks, Happy Hour and
the meal planner that is roughly eleven texts a day, which is how a channel
stops being read. Everything here goes out in two messages instead.

Three rules, each of them a lesson from this project:

**A block that fails must not take the brief with it.** One dead RSS feed
cannot cost Henry the weather. Every block is run in isolation; a failure
becomes one honest line in the brief rather than an exception.

**Numbers come from an API, never from a model.** The market and weather
blocks build their figures from fetched data and hand the model prose at
most. A confabulated closing price reads exactly as confidently as a correct
one, which is the failure mode this whole codebase has been paying for.

**A block says where it got the number.** Each carries its sources into the
report detail, so WHY <n> can answer "according to what?".

Adding a topic is a Block entry, not a new agent, a new plist and a new
launchd unit.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Sequence

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: E402,F401  (imported for its load_dotenv side effect)
from ivy_core import require_env  # noqa: E402
from ivy_core.text_delivery import deliver_report  # noqa: E402

MORNING, EVENING = "morning", "evening"

# Frisco, TX. The weather block is the only thing that needs a location, and
# api.weather.gov wants coordinates rather than a place name.
HOME_LAT, HOME_LON = 33.1507, -96.8236
HTTP_TIMEOUT_S = 15
MAX_ITEMS_PER_BLOCK = 4


@dataclass
class BlockResult:
    """What one section of the brief produced."""
    text: str
    sources: List[str] = field(default_factory=list)
    failed: bool = False

    @classmethod
    def unavailable(cls, why: str) -> "BlockResult":
        """A failed block still occupies its slot, and says so.

        Silence would be indistinguishable from "nothing happened today",
        which is the ambiguity that hid a month of undelivered reports.
        """
        return cls(text=f"(unavailable — {why})", failed=True)


@dataclass
class Block:
    key: str
    title: str
    slot: str
    fetch: Callable[[], BlockResult]
    enabled: bool = True


# --------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------
def _get_json(url: str) -> dict:
    import requests
    r = requests.get(url, timeout=HTTP_TIMEOUT_S,
                     headers={"User-Agent": "ivy-daily-brief (personal use)"})
    r.raise_for_status()
    return r.json()


def fetch_weather() -> BlockResult:
    """Today's forecast from the US National Weather Service.

    Chosen because it needs no API key, has no quota, and is the authoritative
    source for a US location — the figures are reported verbatim rather than
    described by a model.
    """
    try:
        points = _get_json(f"https://api.weather.gov/points/{HOME_LAT},{HOME_LON}")
        forecast_url = points["properties"]["forecast"]
        periods = _get_json(forecast_url)["properties"]["periods"]
    except Exception as exc:
        return BlockResult.unavailable(f"weather.gov: {type(exc).__name__}")

    if not periods:
        return BlockResult.unavailable("weather.gov returned no periods")

    today = periods[0]
    lines = [f"{today['name']}: {today['temperature']}°{today['temperatureUnit']}"
             f" — {today['shortForecast']}"]
    detail = (today.get("detailedForecast") or "").strip()
    if detail:
        lines.append(detail)
    if len(periods) > 1:
        nxt = periods[1]
        lines.append(f"{nxt['name']}: {nxt['temperature']}°{nxt['temperatureUnit']}"
                     f" — {nxt['shortForecast']}")
    return BlockResult(text="\n".join(lines), sources=["api.weather.gov"])


def _feed_items(feeds: Sequence[tuple], limit: int = MAX_ITEMS_PER_BLOCK) -> BlockResult:
    """Headlines from a set of RSS feeds, newest first, deduplicated.

    Headlines are reported as published. Nothing here asks a model to
    summarise them, so nothing here can invent one.
    """
    import feedparser

    items, seen, used, failures = [], set(), [], []
    for source, url in feeds:
        try:
            parsed = feedparser.parse(url, agent="ivy-daily-brief")
            entries = parsed.entries or []
            if not entries:
                failures.append(source)
                continue
            used.append(source)
            for e in entries[:8]:
                title = (e.get("title") or "").strip()
                if not title:
                    continue
                k = title.lower()
                if k in seen:
                    continue
                seen.add(k)
                items.append(f"• {title} ({source})")
        except Exception:
            failures.append(source)

    if not items:
        return BlockResult.unavailable(
            f"no feed reachable ({', '.join(failures) or 'unknown'})")
    text = "\n".join(items[:limit])
    if failures:
        text += f"\n(skipped: {', '.join(failures)})"
    return BlockResult(text=text, sources=used)


GEOPOLITICAL_FEEDS = [
    ("Reuters World", "https://feeds.reuters.com/Reuters/worldNews"),
    ("AP Top", "https://feeds.apnews.com/rss/apf-topnews"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
]
AI_FEEDS = [
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
    ("Hacker News", "https://hnrss.org/frontpage?points=200"),
]
MARKET_FEEDS = [
    ("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
]


def fetch_geopolitical() -> BlockResult:
    return _feed_items(GEOPOLITICAL_FEEDS)


def fetch_ai_news() -> BlockResult:
    return _feed_items(AI_FEEDS)


def fetch_market_news() -> BlockResult:
    return _feed_items(MARKET_FEEDS)


def fetch_readwise_review() -> BlockResult:
    """Readwise's Daily Review — its own spaced-repetition pick for today.

    /api/v2/review/ takes no parameters and needs no tuning: Readwise has
    already chosen what is worth resurfacing. The highlights it returns carry
    their title and author, so each line can say where it came from — unlike
    the /highlights/ LIST endpoint, whose rows have neither.

    Highlights are printed as written. Nothing summarises them, so nothing
    can put words in an author's mouth.
    """
    import requests

    token = os.environ.get("READWISE_API_KEY", "")
    if not token:
        return BlockResult.unavailable("READWISE_API_KEY not set")
    try:
        r = requests.get(
            "https://readwise.io/api/v2/review/",
            headers={"Authorization": f"Token {token}"},
            timeout=HTTP_TIMEOUT_S,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        return BlockResult.unavailable(f"readwise: {type(exc).__name__}")

    highlights = payload.get("highlights") or []
    if not highlights:
        return BlockResult.unavailable("no highlights in today's review")

    lines = []
    for hl in highlights[:2]:
        text = (hl.get("text") or "").strip()
        if not text:
            continue
        if len(text) > 280:
            text = text[:277].rstrip() + "…"
        title = (hl.get("title") or "").strip()
        author = (hl.get("author") or "").strip()
        attribution = " — ".join(p for p in (title, author) if p)
        lines.append(f"“{text}”" + (f"\n  {attribution}" if attribution else ""))

    if not lines:
        return BlockResult.unavailable("review returned no usable highlights")
    return BlockResult(text="\n\n".join(lines), sources=["readwise.io/api/v2/review"])


MARKET_SYMBOLS = [("S&P 500", "^spx"), ("Nasdaq", "^ndq"), ("Dow", "^dji")]


def fetch_market_close() -> BlockResult:
    """Index closes from Stooq's CSV endpoint — no key, no quota.

    Every figure here is parsed from the response. If the source is
    unreachable or a row does not parse, the block says so rather than
    presenting a number it is not sure of.
    """
    import csv
    import io

    import requests

    rows, sources = [], []
    for label, sym in MARKET_SYMBOLS:
        try:
            r = requests.get(
                f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv",
                timeout=HTTP_TIMEOUT_S,
            )
            r.raise_for_status()
            rec = next(csv.DictReader(io.StringIO(r.text)))
            close, open_ = float(rec["Close"]), float(rec["Open"])
            pct = ((close - open_) / open_ * 100) if open_ else 0.0
            rows.append(f"{label}: {close:,.2f} ({pct:+.2f}% on the day)")
            sources.append("stooq.com")
        except Exception:
            rows.append(f"{label}: unavailable")
    if all("unavailable" in r for r in rows):
        return BlockResult.unavailable("no index data reachable")
    return BlockResult(text="\n".join(rows), sources=sorted(set(sources)))


# --------------------------------------------------------------------------
# The brief
# --------------------------------------------------------------------------
BLOCKS: List[Block] = [
    Block("weather", "☀️ Weather", MORNING, fetch_weather),
    Block("geopolitical", "🌍 World", MORNING, fetch_geopolitical),
    Block("ai_news", "🤖 AI", MORNING, fetch_ai_news),
    Block("readwise", "📚 From your highlights", MORNING, fetch_readwise_review),
    Block("market_close", "📊 Market close", EVENING, fetch_market_close),
    Block("market_news", "📰 Markets", EVENING, fetch_market_news),
]


def blocks_for(slot: str, blocks: Optional[Sequence[Block]] = None) -> List[Block]:
    return [b for b in (blocks or BLOCKS) if b.slot == slot and b.enabled]


def build_brief(slot: str, blocks: Optional[Sequence[Block]] = None,
                now: Optional[datetime] = None) -> tuple:
    """Assemble one brief. Returns (body, detail).

    Every block runs even if an earlier one failed -- that isolation is the
    point of the design.
    """
    now = now or datetime.now()
    heading = "Morning brief" if slot == MORNING else "Evening brief"
    parts = [f"{heading} — {now:%a %-d %b}"]
    detail = {"slot": slot, "blocks": [], "generated": now.isoformat()}

    for block in blocks_for(slot, blocks):
        try:
            result = block.fetch()
        except Exception as exc:
            result = BlockResult.unavailable(f"{type(exc).__name__}")
        parts.append(f"\n{block.title}\n{result.text}")
        detail["blocks"].append({
            "key": block.key,
            "title": block.title,
            "text": result.text,
            "sources": result.sources,
            "failed": result.failed,
        })

    failed = [b["title"] for b in detail["blocks"] if b["failed"]]
    if failed and len(failed) == len(detail["blocks"]):
        parts.append("\nEvery section failed to fetch — likely a network problem.")
    return "\n".join(parts), detail


def run(slot: str, send: bool = False, blocks: Optional[Sequence[Block]] = None) -> dict:
    body, detail = build_brief(slot, blocks)
    ok_count = sum(1 for b in detail["blocks"] if not b["failed"])
    summary = f"{ok_count}/{len(detail['blocks'])} section(s)"

    if not send:
        print(body)
        return {"status": "dry-run", "body": body, "detail": detail}

    delivery = deliver_report(
        require_env("HENRY_PHONE"),
        job_name=f"daily_brief_{slot}",
        body=body,
        detail=detail,
        content_summary=summary,
        commands=("MORE", "WHY <n>"),
    )
    return {
        "status": "sent" if delivery.delivered else "failed",
        "delivered": delivery.delivered,
        "report_id": delivery.report_id,
        "detail": detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slot", choices=[MORNING, EVENING], required=True)
    ap.add_argument("--send", action="store_true", help="actually deliver")
    args = ap.parse_args()
    outcome = run(args.slot, send=args.send)
    return 0 if outcome["status"] in ("dry-run", "sent") else 1


if __name__ == "__main__":
    sys.exit(main())
