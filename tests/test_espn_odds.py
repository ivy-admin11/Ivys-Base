"""Prices from ESPN's scoreboard.

Pricing was switched off on 2026-09-08 with a note saying it would return from
ESPN, "which carries odds in the same payload as the score". The replacement
was never built and boards shipped unpriced. This is it, plus the correction
that note needed: ESPN carries odds only while a game is STATUS_SCHEDULED and
drops them once it is final, so a price has to be captured before the game
starts and cannot be backfilled at grading time.

Nothing here touches the network.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ivy_core import espn_odds  # noqa: E402

MATCHUP = "Texas Rangers @ Seattle Mariners"


def market(**over):
    base = {
        "provider": "DraftKings",
        "away_team": "Texas Rangers", "home_team": "Seattle Mariners",
        "moneyline": {"away": "-104", "home": "-112"},
        "spread": {"home": ("+1.5", "-210"), "away": ("-1.5", "+170")},
        "total": {"over": ("o7", "-103"), "under": ("u7", "-117")},
    }
    base.update(over)
    return base


class TestPickingTheRightSide:
    def test_the_home_moneyline(self):
        assert espn_odds.price_for_side("mlb", MATCHUP, "Seattle Mariners ML", [market()]) == "-112"

    def test_the_away_moneyline(self):
        assert espn_odds.price_for_side("mlb", MATCHUP, "Texas Rangers ML", [market()]) == "-104"

    def test_the_over(self):
        assert espn_odds.price_for_side("mlb", MATCHUP, "Over 7", [market()]) == "-103"

    def test_the_under(self):
        assert espn_odds.price_for_side("mlb", MATCHUP, "Under 7", [market()]) == "-117"

    def test_a_spread_takes_its_own_half(self):
        assert espn_odds.price_for_side(
            "mlb", MATCHUP, "Seattle Mariners +1.5", [market()]) == "-210"

    def test_a_side_naming_neither_team_prices_nothing(self):
        """Better no price than the wrong half of a two-sided market."""
        assert espn_odds.price_for_side("mlb", MATCHUP, "Chicago Cubs ML", [market()]) == ""


class TestTheLineHasToMatch:
    """A price is the price OF a line.

    ESPN quoted -103 on a total of 8.5 while the pick read "Over 7". Returning
    that -103 prints a confident number for a bet nobody can place at it.
    """

    def test_a_disagreeing_total_prices_nothing(self):
        m = market(total={"over": ("o8.5", "-103"), "under": ("u8.5", "-117")})
        assert espn_odds.price_for_side("mlb", MATCHUP, "Over 7", [m]) == ""

    def test_an_agreeing_total_prices(self):
        m = market(total={"over": ("o8.5", "-103"), "under": ("u8.5", "-117")})
        assert espn_odds.price_for_side("mlb", MATCHUP, "Over 8.5", [m]) == "-103"

    def test_a_disagreeing_spread_prices_nothing(self):
        assert espn_odds.price_for_side("mlb", MATCHUP, "Seattle Mariners +2.5", [market()]) == ""

    def test_a_bare_over_takes_whatever_the_book_offers(self):
        """No number stated means no disagreement possible."""
        assert espn_odds.price_for_side("mlb", MATCHUP, "Over", [market()]) == "-103"


class TestWhatItRefusesToPrice:
    @pytest.mark.parametrize("side", [
        "Christian McCaffrey Over 4.5 Receptions",
        "Puka Nacua 60+ Receiving Yards",
        "David Peterson Under 4.5 Strikeouts",
        "Coby Mayo HR",
        "Auburn Tigers TT Over 34.5",
    ])
    def test_player_and_team_props(self, side):
        """The scoreboard carries full-game markets only. The game moneyline is
        not the price of a home-run prop, and printing it puts a number in the
        text that contradicts the bet."""
        assert espn_odds.price_for_side("mlb", MATCHUP, side, [market()]) == ""

    def test_an_empty_side(self):
        assert espn_odds.price_for_side("mlb", MATCHUP, "", [market()]) == ""

    def test_no_markets_at_all(self):
        assert espn_odds.price_for_side("mlb", MATCHUP, "Seattle Mariners ML", []) == ""

    def test_a_missing_price_is_not_invented(self):
        m = market(moneyline={"away": "", "home": ""})
        assert espn_odds.price_for_side("mlb", MATCHUP, "Seattle Mariners ML", [m]) == ""


class TestOnlyScheduledGamesCarryOdds:
    """The correction to the note this module was built from."""

    def _payload(self, status, with_odds=True):
        comp = {
            "competitors": [
                {"homeAway": "home", "team": {"displayName": "Seattle Mariners"}},
                {"homeAway": "away", "team": {"displayName": "Texas Rangers"}},
            ],
        }
        if with_odds:
            comp["odds"] = [{
                "provider": {"displayName": "DraftKings"},
                "moneyline": {"home": {"close": {"odds": "-112"}},
                              "away": {"close": {"odds": "-104"}}},
            }]
        return {"events": [{"status": {"type": {"name": status}}, "competitions": [comp]}]}

    def test_a_scheduled_game_is_priced(self, monkeypatch):
        monkeypatch.setattr(espn_odds, "_fetch", lambda sport, league: self._payload("STATUS_SCHEDULED"))
        out = espn_odds.fetch_markets("mlb")
        assert len(out) == 1
        assert out[0]["moneyline"]["home"] == "-112"

    def test_a_final_game_is_skipped(self, monkeypatch):
        """ESPN drops the odds block entirely once a game ends, which is why
        this cannot be a backfill run at grading time."""
        monkeypatch.setattr(espn_odds, "_fetch",
                            lambda sport, league: self._payload("STATUS_FINAL", with_odds=False))
        assert espn_odds.fetch_markets("mlb") == []

    def test_an_unmapped_sport_guesses_nothing(self, monkeypatch):
        """Same rule historical_scores follows, and the reason KBO is absent
        there: an unverified route returns the wrong league or nothing."""
        called = []
        monkeypatch.setattr(espn_odds, "_fetch", lambda sport, league: called.append(1) or {})
        assert espn_odds.fetch_markets("kbo") == []
        assert called == [], "an unmapped sport must not even be requested"


class TestAttachingToABoard:
    def test_it_fills_what_it_can_and_leaves_the_rest(self, monkeypatch):
        monkeypatch.setattr(espn_odds, "fetch_markets", lambda sport: [market()])
        picks = [
            {"sport": "MLB", "matchup": MATCHUP, "side": "Seattle Mariners ML"},
            {"sport": "MLB", "matchup": MATCHUP, "side": "Kyren Williams 2+ Receptions"},
        ]
        assert espn_odds.attach_odds(picks) == 1
        assert picks[0]["odds"] == "-112"
        assert not picks[1].get("odds")

    def test_an_existing_price_is_not_overwritten(self):
        picks = [{"sport": "MLB", "matchup": MATCHUP, "side": "Seattle Mariners ML",
                  "odds": "-110"}]
        espn_odds.attach_odds(picks)
        assert picks[0]["odds"] == "-110"

    def test_each_league_is_fetched_once_not_once_per_pick(self, monkeypatch):
        calls = []
        monkeypatch.setattr(espn_odds, "fetch_markets",
                            lambda sport: calls.append(sport) or [market()])
        picks = [{"sport": "MLB", "matchup": MATCHUP, "side": "Seattle Mariners ML"}
                 for _ in range(5)]
        espn_odds.attach_odds(picks)
        assert calls == ["mlb"]

    def test_a_failure_never_costs_the_board(self, monkeypatch):
        """A board with no prices is a smaller loss than no board."""
        def boom(sport):
            raise RuntimeError("ESPN is down")

        monkeypatch.setattr(espn_odds, "fetch_markets", boom)
        picks = [{"sport": "MLB", "matchup": MATCHUP, "side": "Seattle Mariners ML"}]
        assert espn_odds.attach_odds(picks) == 0
        assert picks[0].get("odds") in (None, "")

    def test_an_empty_board(self):
        assert espn_odds.attach_odds([]) == 0


class TestTheScheduleKnowsWhatHasBeenPlayed:
    """2026-09-11: with the slate down, two games that had gone final the
    night before were texted as "Today". The scoreboard knew; nothing asked."""

    @staticmethod
    def _payload():
        def event(away, home, date, name, state):
            return {"date": date, "status": {"type": {"name": name, "state": state}},
                    "competitions": [{"date": date, "competitors": [
                        {"homeAway": "home", "team": {"displayName": home}},
                        {"homeAway": "away", "team": {"displayName": away}}]}]}
        return {"events": [
            event("San Francisco 49ers", "Los Angeles Rams", "2026-09-11T00:35Z",
                  "STATUS_FINAL", "post"),
            event("Las Vegas Raiders", "Miami Dolphins", "2026-09-13T20:25Z",
                  "STATUS_SCHEDULED", "pre"),
        ]}

    def test_every_game_is_listed_with_its_state(self, monkeypatch):
        monkeypatch.setattr(espn_odds, "_fetch", lambda sport, league, params=None: self._payload())
        games = espn_odds.fetch_schedule("nfl")
        assert [(g["away_team"], g["home_team"], g["state"], g["start"]) for g in games] == [
            ("San Francisco 49ers", "Los Angeles Rams", "post", "2026-09-11T00:35Z"),
            ("Las Vegas Raiders", "Miami Dolphins", "pre", "2026-09-13T20:25Z"),
        ]

    def test_the_window_reaches_back_to_yesterday(self, monkeypatch):
        """A game that finished last night is filed under last night's date."""
        seen = {}
        monkeypatch.setattr(espn_odds, "_fetch",
                            lambda sport, league, params=None: seen.update(params or {}) or {"events": []})
        espn_odds.fetch_schedule("nfl", now=datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc))
        assert seen["dates"] == "20260910-20260913"
        assert seen["limit"] >= 100

    def test_college_football_asks_for_all_of_fbs(self, monkeypatch):
        """The default scoreboard is the Top 25; an unranked game would look unplayed."""
        seen = {}
        monkeypatch.setattr(espn_odds, "_fetch",
                            lambda sport, league, params=None: seen.update(params or {}) or {"events": []})
        espn_odds.fetch_schedule("ncaaf")
        assert seen["groups"] == 80

    def test_an_unmapped_sport_asks_nothing(self, monkeypatch):
        called = []
        monkeypatch.setattr(espn_odds, "_fetch", lambda *a, **k: called.append(1) or {})
        assert espn_odds.fetch_schedule("kbo") == []
        assert called == []

    def test_an_unreachable_scoreboard_lists_nothing(self, monkeypatch):
        monkeypatch.setattr(espn_odds, "_fetch", lambda *a, **k: None)
        assert espn_odds.fetch_schedule("nfl") == []


class TestFindingTheGame:
    RAMS = {"away_team": "San Francisco 49ers", "home_team": "Los Angeles Rams",
            "start": "2026-09-11T00:35Z", "state": "post"}
    G1 = {"away_team": "Texas Rangers", "home_team": "Seattle Mariners",
          "start": "2026-09-10T20:10Z", "state": "post"}
    G2 = {"away_team": "Texas Rangers", "home_team": "Seattle Mariners",
          "start": "2026-09-12T02:10Z", "state": "pre"}
    G3 = {"away_team": "Texas Rangers", "home_team": "Seattle Mariners",
          "start": "2026-09-13T02:10Z", "state": "pre"}

    def test_abbreviations_find_the_game(self):
        assert espn_odds.find_game("SF 49ers @ LA Rams", [self.RAMS]) is self.RAMS

    def test_one_shared_token_is_not_a_match(self):
        assert espn_odds.find_game("Dallas Cowboys @ Rams", [self.RAMS]) is None

    def test_a_series_prefers_the_game_still_ahead_and_the_sooner_of_those(self):
        assert espn_odds.find_game("Rangers @ Mariners", [self.G3, self.G1, self.G2]) is self.G2

    def test_when_every_meeting_is_over_the_played_one_is_returned(self):
        assert espn_odds.find_game("Rangers @ Mariners", [self.G1]) is self.G1

    def test_an_empty_matchup_finds_nothing(self):
        assert espn_odds.find_game("", [self.RAMS]) is None
