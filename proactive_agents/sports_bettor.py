#!/usr/bin/env python3
import sys
import os

# Dynamically calculate the project root so the version-controlled ivy_core
# package resolves regardless of CWD.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Native .env auto-loader (mirrors main.py) so this agent works standalone —
# anchored to PROJECT_ROOT, not the CWD, and never clobbers vars already
# exported in the shell. Lets require_env() see XAI_API_KEY without `source`.
_ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, "r") as _f:
        for _line in _f:
            if "=" in _line and not _line.strip().startswith("#"):
                _k, _v = _line.strip().split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
"""
Sports bettor — sharp picks sourced from X, priced against live Vegas odds.

Pulls the live 48-hour slate (scheduled matchups + real-time lines) from The
Odds API, sweeps the curated handicapper accounts via Grok's x_search tool and
aligns their calls to that slate, merges duplicate picks into consensus plays
(2+ sharps → 🔥 HIGH LIKELIHOOD 🔥), enriches them with live Grok context, and
generates a PDF report and sends it to Henry as a real iMessage attachment.

Run by launchd (com.ivy.sharppicks) 3x daily (9am/3pm/9pm CT), so it
deliberately texts only *net-new* reports: a content fingerprint
of the picks is compared against the last report and an unchanged slate is
skipped (see _report_signature / sports_last_report.json). If the X sweep
returns no usable picks, run() returns a "no_picks" result without sending
(unless force=True, which sends an honest "nothing to report" note instead).
"""

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from filelock import FileLock, Timeout

import config
from ivy_core import require_env, send_imessage, send_imessage_attachment
from ivy_core import outbox as _outbox
from ivy_core.picks_tracker import save_picks
from ivy_core.report_fallback import (
    build_attachment_failure_notice,
    split_imessage_content,
)
from ivy_core.pipeline_status import (
    PipelineStatus,
    PipelineResult,
    ProviderAuthenticationError,
    RetryableProviderError,
    ProviderUnavailableError,
)

# xAI SDK (recommended) or OpenAI-compatible client
try:
    from xai_sdk import Client
    from xai_sdk.chat import user, system
    from xai_sdk.tools import x_search
    USE_XAI_SDK = True
except ImportError:
    from openai import OpenAI
    USE_XAI_SDK = False


# ========================= CONFIG =========================
HENRY_PHONE = config.HENRY_PHONE
XAI_API_KEY = require_env("XAI_API_KEY").strip("'\" ")

# The Odds API (https://the-odds-api.com) — live Vegas lines + scheduled games.
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip("'\" ")
ODDS_SPORT_KEYS = {
    "NFL":        "americanfootball_nfl",
    "MLB":        "baseball_mlb",
    "NBA":        "basketball_nba",
    "NHL":        "icehockey_nhl",
    "EPL":        "soccer_epl",
    "La Liga":    "soccer_spain_la_liga",
    "Bundesliga": "soccer_germany_bundesliga",
    "Serie A":    "soccer_italy_serie_a",
    "KBO":        "baseball_kbo",
    "World Cup":  "soccer_fifa_world_cup",
}

# How far ahead to scout — both the odds feed and the X sweep use this window.
WINDOW_HOURS = 48

# X-platform search directive. A single parenthesized group joined by the
# UPPERCASE `OR` operator is X's correct disjunction syntax — the sweep matches
# a post mentioning ANY one of these leagues/sports, not all of them. Each sport
# is paired with its hashtag form because sharps post "#MLB"/"#NFL" far more than
# the bare word; without the hashtag alternates the bare-token group matched
# almost nothing and the sweep returned 0 picks. This surfaces ANY pick touching
# these leagues/sports — NOT just games present in the live odds slate (which
# omits leagues like KBO and tennis).
SPORT_QUERY = (
    "(MLB OR #MLB OR KBO OR #KBO OR NBA OR #NBA OR NHL OR #NHL "
    "OR Soccer OR #Soccer OR \"World Cup\" OR #WorldCup "
    "OR NFL OR #NFL OR PGA OR #PGA OR Golf OR #Golf OR Tennis OR #Tennis)"
)

# Curated handicappers verified (2026-06-29) to actually post bettable picks.
# A prior 16-handle list quietly returned 0 picks every run: 2 handles no longer
# existed (NickyCashin, Cblez), one was a stock trader (CifrOracle), several were
# news reporters who never bet (JeffPassan, DanGrazianoESPN, TalkinBaseball_,
# MySportsUpdate), and the rest were dormant. These 9 were each confirmed live
# and posting picks via x_search before inclusion.
TARGET_X_ACCOUNTS = [
    "MLBHR","KimsPicks", "parlay_bae", "FlamesPickz", "DanGambleAI", "ItsCappersPicks",
    "Picks4Dayzzz", "billhpicks", "MassMoneyline", "PropCaddie", "NBAModel", "Vegasinsider",
    "HarryLockPicks", "cappersforfree",
]

# Grok's x_search + prompt degrade past ~10 handles (truncated handle lists,
# blown context), so the sweep runs in batches and the raw picks are merged.
# 14 accounts → batches of 8 (8 + 6).
X_ACCOUNT_CHUNK_SIZE = 8

# Last-report state — used to suppress duplicate texts. Stores a content
# fingerprint of the picks (see _report_signature) plus the exact message body
# and timestamp of the last report Henry actually received. If a new run's
# fingerprint (or verbatim body) matches this, the run exits without texting so
# Henry only ever gets net-new information.
LAST_REPORT_PATH = os.path.join(PROJECT_ROOT, "proactive_agents", "sports_last_report.json")

# ===================== LIVE ODDS (The Odds API) =====================
def _summarize_book(bookmaker):
    """Condense one bookmaker's markets into Moneyline/Spread/Total strings."""
    out = {}
    for m in (bookmaker or {}).get("markets", []):
        key = m["key"]
        outcomes = m.get("outcomes", [])
        if key == "h2h":
            out["moneyline"] = " / ".join(
                f"{o['name']} {o['price']:+d}" for o in outcomes
            )
        elif key == "spreads":
            out["spread"] = " / ".join(
                f"{o['name']} {o.get('point', 0):+g} ({o['price']:+d})" for o in outcomes
            )
        elif key == "totals":
            out["total"] = " / ".join(
                f"{o['name']} {o.get('point', 0):g} ({o['price']:+d})" for o in outcomes
            )
    return out


def fetch_live_odds(window_hours=WINDOW_HOURS):
    """Pull scheduled games + live lines across all leagues for the next N hours.
    
    Raises:
        ProviderAuthenticationError: If Odds API returns 401/403
        RetryableProviderError: If API returns 429 or 5xx
        ProviderUnavailableError: On network/timeout errors
    """
    if not ODDS_API_KEY:
        # Odds API is optional for X-sweep fallback, so log and continue
        print("⚠️  ODDS_API_KEY not set — skipping live odds (X sweep will continue)")
        return []

    now = datetime.now(timezone.utc).replace(microsecond=0)
    frm = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (now + timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"📊 Pulling live odds for the next {window_hours}h ({frm} → {to})...")

    games = []
    for league, sport_key in ODDS_SPORT_KEYS.items():
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
                params={
                    "apiKey":           ODDS_API_KEY,
                    "regions":          "us",
                    "markets":          "h2h,spreads,totals",
                    "oddsFormat":       "american",
                    "dateFormat":       "iso",
                    "commenceTimeFrom": frm,
                    "commenceTimeTo":   to,
                },
                timeout=12,
            )
            
            # Handle authentication/authorization failures
            if r.status_code in (401, 403):
                raise ProviderAuthenticationError(
                    provider="odds_api",
                    status_code=r.status_code,
                    message=f"Odds API credentials were rejected (HTTP {r.status_code})",
                    endpoint=r.url,
                )
            
            # Handle rate limiting and server errors (retryable)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 60))
                raise RetryableProviderError(
                    provider="odds_api",
                    status_code=429,
                    message="Odds API rate limited",
                    retry_after=retry_after,
                )
            
            if 500 <= r.status_code < 600:
                raise RetryableProviderError(
                    provider="odds_api",
                    status_code=r.status_code,
                    message=f"Odds API server error (HTTP {r.status_code})",
                )
            
            # Generic HTTP error handling
            r.raise_for_status()
            data = r.json() or []
        
        except ProviderAuthenticationError:
            # Re-raise auth errors (caller must handle)
            raise
        
        except RetryableProviderError:
            # Re-raise retryable errors (caller must handle)
            raise
        
        except requests.exceptions.Timeout:
            raise ProviderUnavailableError(
                provider="odds_api",
                message=f"Odds API timeout (league: {league})",
            )
        
        except requests.exceptions.ConnectionError as e:
            raise ProviderUnavailableError(
                provider="odds_api",
                message=f"Odds API connection failed (league: {league})",
                cause=e,
            )
        
        except Exception as e:
            print(f"⚠️  Odds fetch failed for {league}: {e}")
            continue

        for g in data:
            books = g.get("bookmakers", [])
            # Prefer the first US book carrying all three markets, else the first.
            book = books[0] if books else None
            for b in books:
                keys = {m["key"] for m in b.get("markets", [])}
                if {"h2h", "spreads", "totals"}.issubset(keys):
                    book = b
                    break
            summary = _summarize_book(book)
            games.append({
                "sport": league,
                "home": g.get("home_team"),
                "away": g.get("away_team"),
                "commence": g.get("commence_time", ""),
                "moneyline": summary.get("moneyline", ""),
                "spread": summary.get("spread", ""),
                "total": summary.get("total", ""),
            })

    print(f"📊 Odds feed: {len(games)} scheduled game(s) across {len(ODDS_SPORT_KEYS)} leagues.")
    return games


def build_odds_catalog(games):
    """Render the live slate as a compact catalog string for the Grok prompt."""
    if not games:
        return ""
    lines = []
    for g in games:
        parts = [f"[{g['sport']}] {g['away']} @ {g['home']}"]
        if g["spread"]:
            parts.append(f"Spread: {g['spread']}")
        if g["moneyline"]:
            parts.append(f"ML: {g['moneyline']}")
        if g["total"]:
            parts.append(f"Total: {g['total']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


# ===================== X CONSENSUS (Grok x_search) =====================
def _chunk_accounts(accounts, size=X_ACCOUNT_CHUNK_SIZE):
    """Split the target handles into consecutive batches of `size` (last may be shorter)."""
    return [accounts[i:i + size] for i in range(0, len(accounts), size)]


def _build_sweep_prompt(accounts, slate_clause, query):
    """Assemble the Grok x_search prompt for one batch of handles."""
    return (
        "Review the most recent posts from the listed X accounts and surface every "
        "concrete sports betting pick they have made for games SCHEDULED WITHIN THE "
        f"NEXT 48 HOURS. Focus on these leagues/sports: {query}.\n\n"
        f"Target accounts: {', '.join(f'@{a}' for a in accounts)}\n\n"
        f"{slate_clause}"
        "Return a JSON array. Each element is one pick with fields: "
        "sport (league, e.g. MLB/KBO/NBA/NHL/Soccer/World Cup/NFL/PGA Golf/Tennis), "
        "matchup (formatted 'Away @ Home'), "
        "side (the exact side/total/prop the handicapper is taking), "
        "odds (the American odds for that side copied verbatim from the slate "
        "above, or null if not listed), "
        "handicapper (the X handle of the account that POSTED the pick — one of "
        "the target accounts above, no @; never a tipster name merely quoted "
        "inside the post), "
        "confidence (low/medium/high if stated, otherwise null), "
        "game_day (exactly 'today' or 'tomorrow', based on when the game is played), "
        "start_time (ISO 8601 date-time of the scheduled first pitch/tip-off/kickoff "
        "if known, otherwise null), "
        "and reasoning (one short sentence paraphrasing the post). "
        "Only include picks for games happening in the next 48 hours. "
        "Do not invent matchups, picks, or odds that aren't in the posts/slate. "
        "If no accounts have posted a usable pick, return []. "
        "Output JSON only — no preamble, no markdown fence."
    )


def _sweep_chunk(accounts, slate_clause):
    """Run a single Grok x_search sweep over one batch of handles → list of pick dicts."""
    prompt = _build_sweep_prompt(accounts, slate_clause, SPORT_QUERY)

    try:
        if USE_XAI_SDK:
            client = Client(api_key=XAI_API_KEY)
            chat = client.chat.create(
                model="grok-4.3",
                tools=[x_search(allowed_x_handles=accounts)],
            )
            chat.append(system("You are a precise sports betting consensus analyst."))
            chat.append(user(prompt))
            response = chat.sample()
            raw_text = response.content
        else:
            client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
            response = client.responses.create(
                model="grok-4.3",
                input=[
                    {"role": "system", "content": "You are a precise sports betting consensus analyst."},
                    {"role": "user", "content": prompt},
                ],
                tools=[{"type": "x_search", "allowed_x_handles": accounts}],
            )
            raw_text = response.output_text
    except Exception as e:
        print(f"⚠️ Grok X Search failed for batch {accounts}: {e}.")
        return []

    try:
        picks = json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"⚠️ Grok returned non-JSON output for this batch; treating as empty.\n   Raw: {raw_text[:300]}")
        return []

    if not isinstance(picks, list):
        return []
    # Accept either 'side' (new) or 'pick' (legacy) as the wager field.
    cleaned = []
    for p in picks:
        if not isinstance(p, dict):
            continue
        side = p.get("side") or p.get("pick")
        if p.get("matchup") and side:
            p["side"] = side
            cleaned.append(p)
    return cleaned


def fetch_x_picks(games):
    """Sweep curated X accounts via Grok in batches and return one merged master list of pick dicts.

    The handle list is chunked into batches of `X_ACCOUNT_CHUNK_SIZE` to stay
    under Grok's effective handle/character limits; each batch runs its own
    x_search sweep and all raw picks are concatenated into a single list.
    """
    catalog = build_odds_catalog(games)
    if catalog:
        slate_clause = (
            "Below is the LIVE slate of games scheduled in the next 48 hours with "
            "current Vegas odds, provided as a PRICING REFERENCE only. When a pick "
            "matches one of these games, copy the matching American odds verbatim; "
            "otherwise leave odds null. Do NOT discard a pick just because its game "
            "is absent from this slate (the slate omits some leagues, e.g. KBO and "
            "tennis):\n\n"
            f"{catalog}\n\n"
        )
    else:
        slate_clause = (
            "No live odds slate is available; surface any concrete picks for games "
            "scheduled within the next 48 hours and leave odds null.\n\n"
        )

    batches = _chunk_accounts(TARGET_X_ACCOUNTS)
    print(
        f"🧠 Engaging Grok + X Search across {len(TARGET_X_ACCOUNTS)} accounts "
        f"in {len(batches)} batch(es) of up to {X_ACCOUNT_CHUNK_SIZE}..."
    )

    master_picks = []
    for i, batch in enumerate(batches, start=1):
        print(f"   🔎 Batch {i}/{len(batches)}: {batch}")
        batch_picks = _sweep_chunk(batch, slate_clause)
        print(f"   ↳ {len(batch_picks)} raw pick(s) from batch {i}.")
        master_picks.extend(batch_picks)

    print(f"🧾 Merged sweep total: {len(master_picks)} raw pick(s) across all batches.")
    return master_picks


_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def _norm(value):
    """Loose normalization so 'Lakers -3.5' and 'lakers  -3.5' collapse together."""
    return " ".join(str(value or "").lower().split())


def merge_picks(picks):
    """Merge picks on the same game/side, tagging multi-handicapper plays as consensus."""
    merged = {}
    for p in picks:
        key = (_norm(p.get("matchup")), _norm(p.get("side")))
        entry = merged.get(key)
        if entry is None:
            entry = {
                "sport": p.get("sport"),
                "matchup": p.get("matchup"),
                "side": p.get("side"),
                "odds": p.get("odds"),
                "handicappers": [],
                "confidence": p.get("confidence"),
                "game_day": _norm(p.get("game_day")) or "today",
                "start": p.get("start_time"),
                "reasoning": p.get("reasoning"),
            }
            merged[key] = entry

        handle = p.get("handicapper")
        if handle and handle not in entry["handicappers"]:
            entry["handicappers"].append(handle)

        # Backfill sport/odds/start if an earlier duplicate left them blank.
        if not entry.get("sport") and p.get("sport"):
            entry["sport"] = p.get("sport")
        if not entry.get("odds") and p.get("odds"):
            entry["odds"] = p.get("odds")
        if not entry.get("start") and p.get("start_time"):
            entry["start"] = p.get("start_time")

        # Keep the highest stated confidence across the duplicates.
        if _CONFIDENCE_RANK.get(_norm(p.get("confidence")), 0) > \
                _CONFIDENCE_RANK.get(_norm(entry.get("confidence")), 0):
            entry["confidence"] = p.get("confidence")

        # Prefer a concrete day tag if this duplicate supplies one.
        day = _norm(p.get("game_day"))
        if day in ("today", "tomorrow"):
            entry["game_day"] = day

    result = list(merged.values())
    for e in result:
        e["consensus_count"] = len(e["handicappers"])
        e["is_consensus"] = e["consensus_count"] >= 2

    # Consensus plays first, then by how many sharps are on them.
    result.sort(key=lambda e: (e["is_consensus"], e["consensus_count"]), reverse=True)
    return result


def _team_tokens(text):
    """Word set for fuzzy matchup matching, dropping connective noise."""
    stop = {"vs", "v", "at", "the", "and"}
    return {t for t in _norm(text).replace("@", " ").split() if t and t not in stop}


def _odds_for_side(side, game):
    """Pick the odds market (total / moneyline / spread) that matches the bet type."""
    s = _norm(side)
    total = game.get("total") or ""
    ml = game.get("moneyline") or ""
    spread = game.get("spread") or ""
    # Totals: over/under/total, or an O/U shorthand like "o8.5".
    if any(k in s for k in ("over", "under", "total")) or re.search(r"\b[ou]\s?\d", s):
        return total or ml or spread
    # Moneyline.
    if "moneyline" in s or "money line" in s or "ml" in s.split():
        return ml or spread or total
    # Spread / run line / puck line / handicap, or a signed number like "-1.5".
    if any(k in s for k in ("spread", "run line", "runline", "puck line",
                            "puckline", "handicap", " pk")) or re.search(r"[+-]\d", s):
        return spread or ml or total
    # Ambiguous — default to moneyline, then spread, then total.
    return ml or spread or total


def attach_odds(merged, games):
    """Backfill each pick's sport/odds from the live feed (ground truth) by matchup."""
    for e in merged:
        mtokens = _team_tokens(e.get("matchup"))
        best, best_score = None, 0
        for g in games:
            score = len(mtokens & _team_tokens(f"{g['away']} {g['home']}"))
            if score > best_score:
                best, best_score = g, score
        if best and best_score >= 2:
            if not e.get("sport"):
                e["sport"] = best["sport"]
            # Fill odds from the market that matches the actual bet type.
            if not e.get("odds"):
                e["odds"] = _odds_for_side(e.get("side"), best)
            # The odds feed is authoritative for the scheduled start time.
            if best.get("commence"):
                e["start"] = best["commence"]
    return merged


# ===================== GROK ENRICHMENT =====================
ENRICH_SYSTEM_PROMPT = (
    "You are a sharp sports betting analyst. Enrich each provided pick with "
    "current, real-time context pulled from X and the web. Be concise and factual."
)


def enrich_picks(merged, games):
    """Add per-pick Grok context to every surfaced pick (line move, injuries,
    sharp/public, confidence grade, take).

    One batched x_search call enriches the whole list. On any failure the picks
    pass through unchanged so the text still goes out.
    """
    if not merged:
        return merged

    catalog = build_odds_catalog(games)
    payload = [
        {"i": i, "sport": e.get("sport"), "matchup": e.get("matchup"),
         "side": e.get("side"), "odds": e.get("odds")}
        for i, e in enumerate(merged)
    ]
    prompt = (
        "For EACH pick below, search X and the web for current information and "
        "return enrichment. Live odds slate for reference:\n"
        + (catalog or "(none)") + "\n\n"
        "PICKS:\n" + json.dumps(payload, indent=2) + "\n\n"
        "Return a JSON array with one object per pick, in the SAME ORDER, each with: "
        "i (the integer index copied from the pick), "
        "confidence ('Low'/'Medium'/'High'), "
        "line_movement (short phrase, or null if unknown), "
        "injury (short phrase on relevant injury/lineup news, or null), "
        "sharp_public (short phrase on sharp-vs-public money, or null), "
        "take (a one or two sentence analyst take). "
        "Base everything on real information; use null when unknown. Do not invent. "
        "CRITICAL: if you have no real data for a pick, set take AND every other "
        "field to null — do NOT write placeholder text like 'no data available' or "
        "'no real-time information'. Only include substantive, factual enrichment. "
        "Output JSON only — no preamble, no markdown fence."
    )

    try:
        if USE_XAI_SDK:
            client = Client(api_key=XAI_API_KEY)
            chat = client.chat.create(model="grok-4.3", tools=[x_search()])
            chat.append(system(ENRICH_SYSTEM_PROMPT))
            chat.append(user(prompt))
            raw = chat.sample().content or ""
        else:
            client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
            resp = client.responses.create(
                model="grok-4.3",
                input=[
                    {"role": "system", "content": ENRICH_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                tools=[{"type": "x_search"}],
            )
            raw = resp.output_text
    except Exception as e:
        print(f"⚠️ Grok enrichment failed: {e}. Texting picks without enrichment.")
        return merged

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("⚠️ Enrichment returned non-JSON; texting picks without enrichment.")
        return merged

    by_i = {}
    if isinstance(data, list):
        for obj in data:
            if isinstance(obj, dict) and isinstance(obj.get("i"), int):
                by_i[obj["i"]] = obj

    enriched = 0
    for i, e in enumerate(merged):
        enr = by_i.get(i)
        if isinstance(enr, dict):
            e["enrichment"] = enr
            enriched += 1
    print(f"✨ Enriched {enriched}/{len(merged)} pick(s) with live Grok context.")
    return merged


def _sweep_unrestricted(games):
    """Fallback when the curated-handle sweep is dry: an OPEN (unrestricted)
    x_search for concrete picks. Returns pick dicts in _sweep_chunk's shape.

    The curated `allowed_x_handles` filter returns nothing when those specific
    accounts are quiet/dormant, so without this fallback the whole run exits
    silently and Henry never gets a text.
    """
    catalog = build_odds_catalog(games)
    slate = (
        "Live 48h slate (pricing reference only — copy odds verbatim when a pick "
        f"matches, else null):\n\n{catalog}\n\n" if catalog else ""
    )
    prompt = (
        "Search X for concrete sports betting picks posted by reputable handicappers "
        "in the last 24 hours, for games SCHEDULED WITHIN THE NEXT 48 HOURS. Focus on "
        f"these leagues/sports: {SPORT_QUERY}.\n\n{slate}"
        "Return a JSON array; each element: sport, matchup ('Away @ Home'), side "
        "(exact side/total/prop), odds (verbatim from the slate above or null), "
        "handicapper (the X handle that posted it, no @), confidence (low/medium/high "
        "or null), game_day ('today' or 'tomorrow'), start_time (ISO 8601 or null), "
        "reasoning (one short sentence). Only next-48h games; do not invent picks or "
        "odds. If none, return []. Output JSON only — no preamble, no markdown fence."
    )
    try:
        if USE_XAI_SDK:
            client = Client(api_key=XAI_API_KEY)
            chat = client.chat.create(model="grok-4.3", tools=[x_search()])
            chat.append(system("You are a precise sports betting consensus analyst."))
            chat.append(user(prompt))
            raw_text = chat.sample().content
        else:
            client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
            resp = client.responses.create(
                model="grok-4.3",
                input=[
                    {"role": "system", "content": "You are a precise sports betting consensus analyst."},
                    {"role": "user", "content": prompt},
                ],
                tools=[{"type": "x_search"}],
            )
            raw_text = resp.output_text
    except Exception as e:
        print(f"⚠️ Unrestricted fallback sweep failed: {e}.")
        return []
    try:
        picks = json.loads(raw_text)
    except json.JSONDecodeError:
        print("⚠️ Fallback returned non-JSON; treating as empty.")
        return []
    if not isinstance(picks, list):
        return []
    cleaned = []
    for p in picks:
        if isinstance(p, dict):
            side = p.get("side") or p.get("pick")
            if p.get("matchup") and side:
                p["side"] = side
                cleaned.append(p)
    return cleaned


# Grok's x_search is volatile minute-to-minute — a dry first sweep often fills
# on a retry — so re-run an empty sweep a few times before giving up.
SWEEP_ATTEMPTS = 3
SWEEP_RETRY_DELAY = 20  # seconds between attempts


def sweep_with_retry(games, attempts=SWEEP_ATTEMPTS, delay=SWEEP_RETRY_DELAY):
    """Run the X sweep up to `attempts` times; return the first non-empty result."""
    picks = []
    for attempt in range(1, attempts + 1):
        picks = fetch_x_picks(games)
        if picks:
            if attempt > 1:
                print(f"   ✅ Sweep hit on attempt {attempt}/{attempts}.")
            return picks
        if attempt < attempts:
            print(f"   ↻ Empty sweep {attempt}/{attempts}; retrying in {delay}s...")
            time.sleep(delay)
    print(f"   📭 All {attempts} curated sweep attempts empty — trying open X-search fallback...")
    fb = _sweep_unrestricted(games)
    if fb:
        print(f"   ✅ Fallback surfaced {len(fb)} pick(s) from open X search.")
    return fb


_CENTRAL_TZ = ZoneInfo("America/Chicago")
_PLACEHOLDER_HINTS = (
    "no real-time", "no real time", "no data", "not available",
    "no information", "no info", "unavailable", "n/a",
)


def _is_placeholder(text):
    """True when an enrichment take is empty or non-informative 'no data' filler."""
    t = str(text or "").strip().lower()
    return not t or any(h in t for h in _PLACEHOLDER_HINTS)


def _fmt_start(value):
    """Format an ISO-8601 start time as Central local time; '' if unparseable."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_CENTRAL_TZ).strftime("%a %b %-d, %-I:%M %p CT")
    except Exception:
        return ""


def _pick_when(e):
    """Best-effort scheduled start for a pick: odds-feed time -> Grok
    start_time -> today/tomorrow tag. Shared by the iMessage text body and
    the PDF table so both surfaces show the same game date/time."""
    start = _fmt_start(e.get("start"))
    if not start and e.get("start"):
        start = str(e["start"])
    if not start and e.get("game_day"):
        start = e["game_day"].capitalize()
    return start


def _confidence(e):
    """Confidence grade for a pick, set purely by how many curated X/Grok
    handicapper accounts back the SAME bet: 1 -> LOW, 2 -> MEDIUM, 3+ -> HIGH."""
    count = int(e.get("consensus_count") or len(e.get("handicappers") or []) or 1)
    grade = "HIGH" if count >= 3 else "MEDIUM" if count == 2 else "LOW"
    return grade, count


def format_picks_text(merged):
    """Format the merged picks as a single iMessage body — consensus plays first.

    iMessage renders emoji natively, so consensus plays get a literal
    🔥 HIGH LIKELIHOOD 🔥 banner (unlike the PDF, which needed a raster glyph).
    """
    def _line(e, star=False):
        sport = f"[{e.get('sport')}] " if e.get("sport") else ""
        matchup = e.get("matchup") or ""
        side = e.get("side") or ""
        odds = f" ({e['odds']})" if e.get("odds") else ""
        base = f"{sport}{matchup} — {side}{odds}".strip()
        head = f"🔥 {base} ({e['consensus_count']} sharps)" if star else f"• {base}"

        block = [head]

        # Scheduled start: odds-feed time → Grok start_time → today/tomorrow tag.
        start = _pick_when(e)
        if start:
            block.append(f"   🕒 {start}")

        # Confidence — always shown; graded by how many sharps back the bet.
        # Name the actual handicapper handle(s) instead of just a bare count.
        _grade, _cnt = _confidence(e)
        handles = e.get("handicappers") or []
        credit = ", ".join(f"@{h}" for h in handles) if handles else f"{_cnt} sharp{'' if _cnt == 1 else 's'}"
        block.append(f"   📊 Confidence: {_grade} · Backed by {credit}")

        # Enrichment — only rendered when it carries real signal.
        enr = e.get("enrichment") or {}
        take = enr.get("take")
        if _is_placeholder(take):
            take = None
        if take:
            block.append("   ↳ " + take)
        extras = []
        if enr.get("line_movement"):
            extras.append(f"Line: {enr['line_movement']}")
        if enr.get("injury"):
            extras.append(f"Inj: {enr['injury']}")
        if enr.get("sharp_public"):
            extras.append(str(enr["sharp_public"]))
        if extras:
            block.append("   ↳ " + " · ".join(extras))
        return "\n".join(block)

    consensus = [e for e in merged if e["is_consensus"]]
    others = [e for e in merged if not e["is_consensus"]]

    lines = [f"🔒 Ivy's Sharp Picks — {datetime.now():%b %-d}"]
    if consensus:
        lines.append("")
        lines.append("🔥 HIGH LIKELIHOOD 🔥")
        lines += [_line(e, star=True) for e in consensus]
    if others:
        lines.append("")
        if consensus:
            lines.append("More sharp picks:")
        lines += [_line(e) for e in others]
    return "\n".join(lines)


def format_picks_by_sport(merged):
    """Format picks as a text message grouped by sport with emoji separators.
    
    Replaces PDF generation with a clean text format that groups picks by sport,
    uses emojis to separate sections, and includes key details per pick.
    """
    # Group picks by sport
    by_sport = {}
    for pick in merged:
        sport = pick.get("sport", "Other")
        if sport not in by_sport:
            by_sport[sport] = []
        by_sport[sport].append(pick)
    
    # Sport emojis
    sport_emoji = {
        "MLB": "⚾",
        "NBA": "🏀",
        "NFL": "🏈",
        "NHL": "🏒",
        "World Cup": "⚽",
        "Soccer": "⚽",
        "Tennis": "🎾",
        "Golf": "⛳",
        "Other": "📊"
    }
    
    lines = [f"🔒 Ivy's Sharp Picks — {datetime.now():%b %-d, %I:%M %p}"]
    lines.append("")
    
    # Separate consensus and regular picks
    consensus_picks = [p for p in merged if p.get("is_consensus")]
    regular_picks = [p for p in merged if not p.get("is_consensus")]
    
    # Show consensus plays first
    if consensus_picks:
        lines.append("🔥 HIGH LIKELIHOOD 🔥 (Consensus Plays)")
        lines.append("")
        for pick in consensus_picks:
            sport = pick.get("sport", "Other")
            emoji = sport_emoji.get(sport, "📊")
            matchup = pick.get("matchup", "")
            side = pick.get("side", "")
            odds = f" ({pick['odds']})" if pick.get("odds") else ""
            handicappers = pick.get("handicappers", [])
            count = len(handicappers) if handicappers else 1
            
            line = f"{emoji} {matchup} — {side}{odds}"
            line += f"\n   🔥 {count} sharps agree"
            
            if handicappers:
                line += f": {', '.join(handicappers)}"
            
            lines.append(line)
            lines.append("")
    
    # Group regular picks by sport
    if regular_picks:
        if consensus_picks:
            lines.append("📌 Additional Picks by Sport:")
        else:
            lines.append("Today's Picks by Sport:")
        lines.append("")
        
        for sport in sorted(by_sport.keys()):
            sport_picks = [p for p in by_sport[sport] if not p.get("is_consensus")]
            if not sport_picks:
                continue
            
            emoji = sport_emoji.get(sport, "📊")
            lines.append(f"{emoji} {sport.upper()}")
            
            for pick in sport_picks:
                matchup = pick.get("matchup", "")
                side = pick.get("side", "")
                odds = f" ({pick['odds']})" if pick.get("odds") else ""
                handicapper = pick.get("handicapper") or "Sharp"
                confidence = pick.get("confidence", "Medium")
                
                line = f"  • {matchup}"
                line += f"\n    {side}{odds}"
                line += f" | {handicapper} ({confidence})"
                
                lines.append(line)
            
            lines.append("")
    
    # Footer with summary
    lines.append("—")
    lines.append(f"Total: {len(merged)} picks ({len(consensus_picks)} consensus)")
    lines.append("Check the dashboard: https://docs.google.com/spreadsheets/d/1vxdAfvLyu3o3N-suV1qxX6KWbYZyCiQvNcYdOxePoHQ/")
    
    return "\n".join(lines)


# ===================== DUPLICATE-REPORT SUPPRESSION =====================
def _report_signature(merged):
    """Stable content fingerprint of the picks, independent of run date/enrichment.

    Built from the substantive betting content only — sport, matchup, side, odds
    and how many sharps are on each — sorted so ordering never affects the hash.
    The daily date header and volatile enrichment prose are deliberately excluded
    so that an unchanged slate hashes identically across runs (a repeat), while a
    changed pick or a moved line (odds differ) hashes differently (net-new).
    """
    items = sorted(
        "|".join((
            _norm(e.get("sport")),
            _norm(e.get("matchup")),
            _norm(e.get("side")),
            _norm(e.get("odds")),
            str(e.get("consensus_count", 0)),
        ))
        for e in merged
    )
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()


def load_last_report():
    """Load the last-sent report state; empty dict if none/unreadable."""
    try:
        with open(LAST_REPORT_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"⚠️ Could not read last-report state ({e}); treating as first run.")
        return {}


def save_last_report(signature, message):
    """Persist the fingerprint + verbatim body of the report just sent."""
    try:
        with open(LAST_REPORT_PATH, "w") as f:
            json.dump(
                {
                    "signature": signature,
                    "message": message,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                },
                f,
                indent=2,
            )
    except Exception as e:
        print(f"⚠️ Could not persist last-report state: {e}")


def run(
    *,
    force: bool = False,
    send: bool = True,
    requester: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Sweep the sharp-picks pipeline once.

    force=True bypasses the duplicate-suppression gate (used for ad-hoc/
    on-demand requests, so "run picks now" always delivers even if the slate
    hasn't changed since the last scheduled report) — message suppression
    only, not pick identity: persistence always upserts through the same
    canonical picks_tracker.save_picks() either way. send=False runs the
    full sweep without texting anything or writing Google Sheets (dry-run).

    A non-blocking filelock prevents overlapping scheduled + ad-hoc
    executions. Result reconciliation (ivy_core.result_updater) runs once
    per lock-holding invocation, after the pipeline, on every real (send=True)
    run — even when this run found no new picks or was a duplicate report —
    so changed results are always synced. This is the only place reconciliation
    is invoked automatically; do not add a second launchd job for it.
    """
    _lock_path = os.path.join(PROJECT_ROOT, "data", "sharp_picks.lock")
    os.makedirs(os.path.dirname(_lock_path), exist_ok=True)
    _lock = FileLock(_lock_path, timeout=0)

    try:
        _lock.acquire()
    except Timeout:
        msg = "Another Sharp Picks execution is already running — skipped to prevent overlap."
        print(f"⏭️  {msg}")
        return {"result_type": "skipped", "reason": msg}

    try:
        result = _run_pipeline(force=force, send=send, requester=requester, request_id=request_id)
        if send:
            from ivy_core import result_updater
            try:
                result["result_reconciliation"] = result_updater.auto_update_results()
            except Exception as exc:
                print(f"⚠️ Result reconciliation failed: {exc}")
                result["result_reconciliation"] = {"status": "error", "reason": str(exc)}
        return result
    finally:
        _lock.release()


def _run_pipeline(
    *,
    force: bool = False,
    send: bool = True,
    requester: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Internal pipeline — called only when the run() filelock is held.
    
    Tracks source health and pipeline status explicitly. Only marks a run as
    SUCCESS when all required sources are healthy and minimum pick thresholds
    are met. Otherwise, reports the true status (AUTH_FAILURE, DEGRADED, etc.).
    """
    print("🚀 Starting 48-Hour X-Sourced Sports Picks Loop...")
    
    result = PipelineResult(status=PipelineStatus.SUCCESS)
    odds_source = result.add_source("The Odds API", is_required=False)
    grok_source = result.add_source("Grok X Search", is_required=True)
    
    # Fetch live odds (handle auth/upstream errors explicitly)
    try:
        games = fetch_live_odds()
        odds_source.mark_success(pick_count=len(games))
    except ProviderAuthenticationError as e:
        print(f"🔴 {e}")
        odds_source.mark_failure(e, status_code=e.status_code)
        result.status = PipelineStatus.AUTH_FAILURE
        result.admin_message = (
            f"Sharp Picks halted: Odds API authentication failed.\n"
            f"Status: HTTP {e.status_code}\n"
            f"Message: {e.message}\n"
            f"Admin action required: Verify ODDS_API_KEY is current and authorized."
        )
        print(result.admin_message)
        return result.to_dict()
    except RetryableProviderError as e:
        print(f"⚠️  {e} — will retry on next scheduled run")
        odds_source.mark_failure(e, status_code=e.status_code)
        result.status = PipelineStatus.DEGRADED
        games = []
    except ProviderUnavailableError as e:
        print(f"⚠️  {e} — temporarily unavailable")
        odds_source.mark_failure(e)
        result.status = PipelineStatus.DEGRADED
        games = []
    except Exception as e:
        print(f"🔴 Unexpected error fetching odds: {e}")
        odds_source.mark_failure(e)
        result.status = PipelineStatus.INTERNAL_ERROR
        return result.to_dict()
    
    # Sweep for picks (Grok/X search)
    try:
        picks = sweep_with_retry(games)
        grok_source.mark_success(pick_count=len(picks))
    except Exception as e:
        print(f"⚠️  Grok X Search failed: {e}")
        grok_source.mark_failure(e)
        picks = []
        # Grok is required, so downgrade status
        if result.status == PipelineStatus.SUCCESS:
            result.status = PipelineStatus.UPSTREAM_UNAVAILABLE

    if not picks:
        print("📭 No active picks pulled from X sweep.")
        # On an ad-hoc (forced) request, never fail silently
        if force and send:
            send_imessage(
                HENRY_PHONE,
                "🔒 Ivy's Sharp Picks: no bettable picks surfaced right now — the "
                "handicappers are quiet and the open sweep came up empty. I'll keep "
                "watching and send them the moment there's a play.",
            )
            print("📨 Sent 'no picks' notice to Henry (ad-hoc run).")
        result.status = PipelineStatus.NO_QUALIFYING_PICKS
        return result.to_dict()

    merged = merge_picks(picks)
    attach_odds(merged, games)
    enrich_picks(merged, games)
    
    # Filter picks by minimum quality threshold
    # A valid pick should have:
    #   - confidence level (not just 55% single-sharp noise)
    #   - At least 2 sharps for consensus, OR 1 sharp with medium+ confidence
    min_confidence_single = "medium"  # Only accept high-confidence single-sharp picks
    min_sharps_consensus = 2
    
    filtered_picks = []
    for p in merged:
        confidence = (p.get("enrichment", {}).get("confidence") or "").lower()
        is_consensus = p.get("is_consensus", False)
        sharp_count = p.get("consensus_count", 1)
        
        # Accept if: consensus (2+ sharps) OR single-sharp with medium/high confidence
        if is_consensus or (sharp_count == 1 and confidence in ("medium", "high")):
            filtered_picks.append(p)
    
    if not filtered_picks:
        print(f"⚠️  {len(merged)} pick(s) found but none meet minimum quality threshold.")
        print("   (Require: 2+ sharps for consensus OR 1 sharp with medium/high confidence)")
        result.picks_count = 0
        result.consensus_count = 0
        result.status = PipelineStatus.NO_QUALIFYING_PICKS
        return result.to_dict()
    
    consensus_n = sum(1 for p in filtered_picks if p["is_consensus"])
    print(f"🧮 {len(picks)} raw pick(s) → {len(merged)} unique → {len(filtered_picks)} qualifying ({consensus_n} consensus).")
    
    result.picks_count = len(filtered_picks)
    result.consensus_count = consensus_n
    
    # Only now save picks that passed validation
    save_picks(filtered_picks, report_date=datetime.now().strftime("%Y-%m-%d"))

    # Build the outbound body and its content fingerprint.
    signature = _report_signature(filtered_picks)

    # Duplicate suppression: only text Henry net-new information.
    if force:
        print("⚡ force=True — bypassing duplicate suppression (ad-hoc run).")
    last = load_last_report()
    if not force and last.get("signature") == signature:
        print("🔁 Picks unchanged since last report (same fingerprint) — skipping duplicate report.")
        result.status = PipelineStatus.SUCCESS
        return result.to_dict()

    # Generate text-only report
    print("📝 Generating text-only picks report...")
    report_text = format_picks_by_sport(filtered_picks)
    print(f"✅ Report formatted ({len(filtered_picks)} picks)")

    # Assign a report ID
    report_id = _outbox.make_report_id("sharp_picks")
    result.report_id = report_id
    content_summary = (
        f"{len(filtered_picks)} pick(s), {consensus_n} consensus — {datetime.now():%b %-d}"
    )
    print(f"📦 Report ID: {report_id}")

    if not send:
        print("🧪 send=False — dry run, not sending.")
        result.sent = False
        result.status = PipelineStatus.SUCCESS
        return result.to_dict()

    # Send text report directly
    delivered_text = send_imessage(HENRY_PHONE, report_text)
    
    if delivered_text:
        save_last_report(signature, report_text)
        print(f"✅ {len(filtered_picks)} pick(s) reported to Henry ({consensus_n} consensus).")
        result.sent = True
        result.status = PipelineStatus.SUCCESS
        result.message = f"Report {report_id} sent successfully."
        return result.to_dict()

    # Fallback if text send failed
    print("⚠️  Text delivery failed")
    print(f"Report ID {report_id} queued for retry")
    result.sent = False
    result.status = PipelineStatus.INTERNAL_ERROR
    result.message = f"Report {report_id} failed to send — queued for retry"
    return result.to_dict()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sports Bettor / Sharp Picks")
    parser.add_argument("--force", action="store_true", help="Bypass duplicate-report suppression")
    parser.add_argument("--send", action="store_true", help="Actually send the iMessage")
    parser.add_argument("--dry-run", action="store_true", help="Sweep but don't send (default)")
    parser.add_argument("--scheduled", action="store_true", help="Scheduled run (preserves suppression)")
    cli_args = parser.parse_args()

    # Back-compat: the launchd plist's shell script may still export
    # SPORTS_FORCE_SEND rather than pass --force.
    force = (cli_args.force or bool(os.environ.get("SPORTS_FORCE_SEND", "").strip())) and not cli_args.scheduled
    send = cli_args.send and not cli_args.dry_run

    result = run(force=force, send=send)
    print(json.dumps(result, indent=2))
