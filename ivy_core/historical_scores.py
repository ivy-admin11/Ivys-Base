"""Scores for games older than the odds provider will serve.

The Odds API's scores endpoint documents `daysFrom` as "integers from 1 to 3".
That is a hard ceiling, not a setting: no configuration reaches a game from
July. It is why 80 of 88 picks were written off as unverifiable.

ESPN publishes public scoreboard endpoints keyed by date, with no API key,
covering past seasons. This module fetches from there and normalises the
result into exactly the shape ivy_core.result_updater already expects — the
same `home_team` / `away_team` / `scores[{name, score}]` / `sport_key` dict the
odds provider returns. That is deliberate: the grading logic in that module
has been fixed and tested, and none of it needs to change or be duplicated to
grade an older game.

Two limits worth stating plainly rather than discovering later:

**A scoreboard has team scores and no player statistics.** Player props — home
runs, strikeouts, receptions — cannot be graded from this source at any age.
Roughly half the outstanding backlog is props, and they stay unverifiable.

**These endpoints are undocumented.** ESPN publishes them, many tools depend
on them, and they have been stable for years, but they carry no compatibility
promise. A parse failure here must therefore leave a pick unverifiable, never
guess at a result — a wrong grade is worse than an absent one.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("ivy.historical_scores")

# site.api.espn.com answers 403 Forbidden to this client; site.web.api is the
# host that serves it. Both are tried, in that order, because which one works
# has changed before and an undocumented endpoint gives no notice.
ESPN_HOSTS = (
    "https://site.web.api.espn.com/apis/site/v2/sports",
    "https://site.api.espn.com/apis/site/v2/sports",
)

# A non-browser User-Agent is refused. This is a plain browser string, not an
# attempt to look like a person: the endpoint simply rejects anything else.
ESPN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
HTTP_TIMEOUT_S = 20

# Ivy's sport label -> (ESPN sport, ESPN league), and the sport_key
# result_updater.match_pick_to_game matches on. A sport absent from this map is
# simply not backfillable; nothing is guessed for it.
SPORT_ROUTES: Dict[str, tuple] = {
    "mlb": ("baseball", "mlb", "baseball_mlb"),
    "nba": ("basketball", "nba", "basketball_nba"),
    "nfl": ("football", "nfl", "americanfootball_nfl"),
    "nhl": ("hockey", "nhl", "icehockey_nhl"),
    "ncaaf": ("football", "college-football", "americanfootball_ncaaf"),
    "epl": ("soccer", "eng.1", "soccer_epl"),
    "world cup": ("soccer", "fifa.world", "soccer_fifa_wc"),
    # KBO is deliberately absent: no route was verified for it, and an
    # unverified route would produce either nothing or the wrong league.
}


def supported_sports() -> List[str]:
    return sorted(SPORT_ROUTES)


def _competitor_score(comp: dict) -> Optional[str]:
    raw = comp.get("score")
    if isinstance(raw, dict):          # some feeds nest it
        raw = raw.get("value", raw.get("displayValue"))
    return None if raw is None else str(raw)


def normalise_event(event: dict, sport_key: str) -> Optional[dict]:
    """One ESPN event -> the game dict result_updater consumes, or None.

    Returns None for anything not clearly a completed game with two named
    competitors and two scores. Being strict here is what keeps a half-parsed
    payload from becoming a confident wrong grade.
    """
    try:
        status = event.get("status") or {}
        if not (status.get("type") or {}).get("completed"):
            return None
        competitions = event.get("competitions") or []
        if not competitions:
            return None
        competitors = competitions[0].get("competitors") or []
        if len(competitors) != 2:
            return None

        home = away = None
        for c in competitors:
            side = (c.get("homeAway") or "").lower()
            name = ((c.get("team") or {}).get("displayName") or "").strip()
            score = _competitor_score(c)
            if not name or score is None:
                return None
            if side == "home":
                home = (name, score)
            elif side == "away":
                away = (name, score)
        if not home or not away:
            return None

        return {
            "sport_key": sport_key,
            "completed": True,
            "home_team": home[0],
            "away_team": away[0],
            # Named entries, matching the odds provider's shape. result_updater
            # looks these up by name rather than position.
            "scores": [
                {"name": home[0], "score": home[1]},
                {"name": away[0], "score": away[1]},
            ],
        }
    except Exception as exc:
        logger.warning("Could not normalise an ESPN event: %s", exc)
        return None


def fetch_completed_games(sport: str, date: str) -> List[dict]:
    """Completed games for one sport on one YYYY-MM-DD date.

    Returns [] for an unsupported sport, an unreachable endpoint or an
    unparseable payload — every one of which leaves picks ungraded rather than
    guessed.
    """
    route = SPORT_ROUTES.get((sport or "").strip().lower())
    if not route:
        return []
    espn_sport, league, sport_key = route

    import requests

    events, last_error = None, None
    for base in ESPN_HOSTS:
        url = f"{base}/{espn_sport}/{league}/scoreboard"
        try:
            resp = requests.get(
                url,
                params={"dates": date.replace("-", "")},
                timeout=HTTP_TIMEOUT_S,
                headers=ESPN_HEADERS,
            )
            resp.raise_for_status()
            events = resp.json().get("events") or []
            break
        except Exception as exc:
            last_error = exc
            continue

    if events is None:
        logger.warning("ESPN %s/%s on %s unreachable: %s",
                       espn_sport, league, date, last_error)
        return []

    games = [g for g in (normalise_event(e, sport_key) for e in events) if g]
    logger.info("ESPN %s %s: %d completed game(s)", sport, date, len(games))
    return games
