"""Pre-game prices from ESPN's scoreboard, free and keyless.

Why this exists
---------------
Pricing was switched off on 2026-09-08 because the paid Odds API dependency
kept breaking, and the note left behind said prices would "come back from
ESPN's scoreboard, which carries odds in the same payload as the score — so
this is a switch rather than a deletion". The switch was never built. Boards
have shipped with no prices at all since.

The correction that note needs
------------------------------
ESPN does NOT carry odds in the same payload as the score. It carries them
only while a game is STATUS_SCHEDULED and drops them once the game is final:

    STATUS_FINAL       odds absent    Tampa Bay Rays at Atlanta Braves
    STATUS_SCHEDULED   odds present   Texas Rangers at Seattle Mariners

So this cannot be a backfill run at grading time, which is what "backfill"
implied. A price has to be captured while the game is still ahead of us, at
the moment the pick is swept. That is the only window it exists in, and a
price recorded after the fact would have to come from somewhere else.

What it will not do
-------------------
Player props. ESPN's scoreboard carries full-game markets only, so a pick like
"Christian McCaffrey Over 4.5 Receptions" gets no price rather than the game
moneyline standing in for it. That rule predates this module and is the right
one: a number that contradicts the bet is worse than no number.

Nothing here raises. A pricing failure must never cost Henry the picks.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import requests

from ivy_core.historical_scores import (
    ESPN_HEADERS,
    ESPN_HOSTS,
    HTTP_TIMEOUT_S,
    SPORT_ROUTES,
)

logger = logging.getLogger("ivy.espn_odds")

# ESPN publishes several books; the first entry is its own priority order.
# Whichever it leads with is the one used, and the name travels with the price
# so a board can say where its numbers came from.
_SCHEDULED = "STATUS_SCHEDULED"

_WORD_RE = re.compile(r"[a-z0-9]+")
# Tokens that appear in so many team names they cannot identify one.
_STOPWORDS = {"the", "fc", "sc", "afc", "cf", "club", "city", "united", "state"}


def _tokens(text: str) -> set:
    return {w for w in _WORD_RE.findall(str(text or "").lower()) if w not in _STOPWORDS}


def _fetch(sport: str, league: str) -> Optional[dict]:
    """Scoreboard JSON, trying each host in turn. None on total failure."""
    path = f"{sport}/{league}/scoreboard"
    for host in ESPN_HOSTS:
        try:
            r = requests.get(f"{host}/{path}", headers=ESPN_HEADERS, timeout=HTTP_TIMEOUT_S)
            if r.status_code == 200:
                return r.json()
            logger.debug("ESPN %s/%s returned HTTP %s", host, path, r.status_code)
        except Exception as exc:  # network, timeout, bad JSON
            logger.debug("ESPN %s/%s failed: %s", host, path, exc)
    logger.info("No ESPN host served %s", path)
    return None


def _close(node: Any, key: str = "odds") -> str:
    """Pull a closing value out of ESPN's {home: {close: {odds: ...}}} nesting."""
    try:
        value = (node or {}).get("close", {}).get(key)
    except AttributeError:
        return ""
    return str(value).strip() if value not in (None, "") else ""


def _markets_from(competition: dict) -> Dict[str, Any]:
    """Normalise one competition's odds block into flat, side-addressable prices."""
    books = competition.get("odds") or []
    if not books:
        return {}
    book = books[0]
    competitors = competition.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})

    def team_name(c):
        t = c.get("team") or {}
        return t.get("displayName") or t.get("name") or ""

    ml = book.get("moneyline") or {}
    ps = book.get("pointSpread") or {}
    tot = book.get("total") or {}

    return {
        "provider": ((book.get("provider") or {}).get("displayName") or "").strip(),
        "home_team": team_name(home),
        "away_team": team_name(away),
        "moneyline": {"home": _close(ml.get("home")), "away": _close(ml.get("away"))},
        "spread": {
            "home": (_close(ps.get("home"), "line"), _close(ps.get("home"))),
            "away": (_close(ps.get("away"), "line"), _close(ps.get("away"))),
        },
        "total": {
            "over": (_close(tot.get("over"), "line"), _close(tot.get("over"))),
            "under": (_close(tot.get("under"), "line"), _close(tot.get("under"))),
        },
    }


def fetch_markets(sport_label: str) -> List[Dict[str, Any]]:
    """Priced, not-yet-started games for one Ivy sport label.

    An unmapped sport returns nothing rather than guessing a route — the same
    rule historical_scores follows, and the reason KBO is absent there.
    """
    route = SPORT_ROUTES.get(str(sport_label or "").lower())
    if not route:
        return []
    payload = _fetch(route[0], route[1])
    if not payload:
        return []

    out = []
    for event in payload.get("events") or []:
        for competition in event.get("competitions") or []:
            status = ((event.get("status") or {}).get("type") or {}).get("name")
            if status != _SCHEDULED:
                # Final games carry no odds block at all; skipping them here
                # keeps that fact visible rather than silently yielding {}.
                continue
            markets = _markets_from(competition)
            if markets:
                out.append(markets)
    return out


def _is_player_prop(side: str) -> bool:
    """A bet on a person's stat line, which the scoreboard never prices."""
    s = str(side or "").lower()
    hints = (
        "receptions", "receiving", "rushing", "passing", "yards", "touchdown",
        "td", "strikeouts", "hits", "rbi", "home run", "hr", "points", "rebounds",
        "assists", "saves", "goals", "shots", "blocks", "steals",
    )
    return any(h in s for h in hints)


def _side_family(side: str) -> str:
    s = str(side or "").lower()
    if re.search(r"\b(over|under)\b", s) or re.match(r"^\s*[ou]\s?\d", s):
        return "total"
    if re.search(r"[+-]\d", s):
        return "spread"
    if re.search(r"\b(ml|moneyline)\b", s):
        return "moneyline"
    return "moneyline"


_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _stated_line(text: str) -> Optional[float]:
    """The number the bet is on: 8.5 from "Over 8.5", -1.5 from "SEA -1.5"."""
    match = _NUMBER_RE.search(str(text or "").replace("o", " ").replace("u", " ")
                              if str(text or "").strip()[:1].lower() in "ou" else str(text or ""))
    return float(match.group()) if match else None


def _lines_agree(side: str, book_line: str) -> bool:
    """True when the pick and the book are on the same number.

    A price is the price OF a line. ESPN quoted -103 for a total of 8.5 while
    the pick read "Over 7", and returning that -103 would have printed a
    confident number for a bet nobody could place at it. When either side
    states no number this passes — a bare "Over" is priced by whatever the
    book is offering — but two numbers that disagree is a hard no.
    """
    want, got = _stated_line(side), _stated_line(book_line)
    if want is None or got is None:
        return True
    return abs(abs(want) - abs(got)) < 1e-9


def price_for_side(sport_label: str, matchup: str, side: str,
                   markets: List[Dict[str, Any]]) -> str:
    """The single price for the side actually taken, or "" when unknown.

    "" is a real answer here. A price for the other half of the market, or a
    game line standing in for a prop, is a number that contradicts the bet.
    """
    if not side or _is_player_prop(side):
        return ""

    want = _tokens(matchup) or _tokens(side)
    best, best_score = None, 0
    for m in markets:
        score = len(want & _tokens(f"{m['away_team']} {m['home_team']}"))
        if score > best_score:
            best, best_score = m, score
    if not best or best_score < 2:
        return ""

    family = _side_family(side)
    side_tokens = _tokens(side)
    is_home = len(side_tokens & _tokens(best["home_team"])) >= 1
    is_away = len(side_tokens & _tokens(best["away_team"])) >= 1

    if family == "total":
        leg = "over" if re.search(r"\b(over|o)\b", side.lower()) else "under"
        line, odds = best["total"].get(leg, ("", ""))
        if not _lines_agree(side, line):
            return ""
        return odds or ""

    if family == "spread":
        if is_home == is_away:
            return ""  # names both teams or neither; cannot pick a half
        line, odds = best["spread"]["home" if is_home else "away"]
        if not _lines_agree(side, line):
            return ""
        return odds or ""

    if is_home == is_away:
        return ""
    return best["moneyline"]["home" if is_home else "away"] or ""


def attach_odds(picks: List[Dict[str, Any]]) -> int:
    """Fill in ``odds`` for every pick this can price. Returns how many.

    Groups by sport so each league's scoreboard is fetched once, not once per
    pick. Never raises.
    """
    if not picks:
        return 0

    by_sport: Dict[str, List[Dict[str, Any]]] = {}
    for p in picks:
        by_sport.setdefault(str(p.get("sport") or "").lower(), []).append(p)

    filled = 0
    for sport_label, group in by_sport.items():
        try:
            markets = fetch_markets(sport_label)
        except Exception as exc:
            logger.warning("ESPN odds unavailable for %s: %s", sport_label, exc)
            continue
        if not markets:
            continue
        for p in group:
            if p.get("odds"):
                continue
            try:
                price = price_for_side(sport_label, p.get("matchup"), p.get("side"), markets)
            except Exception as exc:
                logger.debug("ESPN pricing failed for %r: %s", p.get("side"), exc)
                continue
            if price:
                p["odds"] = price
                filled += 1
    if filled:
        logger.info("ESPN priced %d of %d pick(s)", filled, len(picks))
    return filled
