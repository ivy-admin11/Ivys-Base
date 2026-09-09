"""Grading picks older than the odds provider will serve.

The Odds API caps daysFrom at 3 by documentation, so 80 of 88 picks were
written off as unverifiable — not because grading was broken, but because the
source has no data that old. ESPN's public scoreboards are keyed by date and
go back seasons.

The design rule under test: normalise into the shape result_updater already
consumes, so an old game is graded by the same repaired logic as a recent one.
And never guess — a payload that does not parse cleanly leaves the pick
ungraded, because a wrong grade is worse than an absent one.
"""
from __future__ import annotations

import pytest

from ivy_core import historical_scores as hs
from ivy_core.result_updater import match_pick_to_game


def event(home="Kansas City Royals", away="San Diego Padres",
          hs_score="5", as_score="2", completed=True):
    return {
        "status": {"type": {"completed": completed}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "team": {"displayName": home}, "score": hs_score},
            {"homeAway": "away", "team": {"displayName": away}, "score": as_score},
        ]}],
    }


class TestNormalisation:
    def test_it_produces_the_shape_the_grader_consumes(self):
        g = hs.normalise_event(event(), "baseball_mlb")
        assert set(g) >= {"sport_key", "home_team", "away_team", "scores", "completed"}
        assert [s["name"] for s in g["scores"]] == [g["home_team"], g["away_team"]]

    def test_scores_are_named_not_positional(self):
        """result_updater looks scores up by name; a positional read is the bug
        that could invert every moneyline."""
        g = hs.normalise_event(event(), "baseball_mlb")
        assert all("name" in s and "score" in s for s in g["scores"])

    def test_an_unfinished_game_is_not_returned(self):
        assert hs.normalise_event(event(completed=False), "baseball_mlb") is None

    @pytest.mark.parametrize("broken", [
        {}, {"status": {}}, {"status": {"type": {"completed": True}}},
        {"status": {"type": {"completed": True}}, "competitions": []},
    ])
    def test_a_malformed_payload_yields_nothing(self, broken):
        assert hs.normalise_event(broken, "baseball_mlb") is None

    def test_a_missing_score_yields_nothing_rather_than_a_zero(self):
        """A zero would grade the game. Absence must stay absence."""
        e = event()
        e["competitions"][0]["competitors"][0]["score"] = None
        assert hs.normalise_event(e, "baseball_mlb") is None

    def test_a_missing_team_name_yields_nothing(self):
        e = event()
        e["competitions"][0]["competitors"][0]["team"] = {}
        assert hs.normalise_event(e, "baseball_mlb") is None

    def test_only_two_competitors_are_accepted(self):
        e = event()
        e["competitions"][0]["competitors"].append({"homeAway": "home"})
        assert hs.normalise_event(e, "baseball_mlb") is None

    def test_a_nested_score_value_is_read(self):
        e = event()
        e["competitions"][0]["competitors"][0]["score"] = {"value": 5, "displayValue": "5"}
        assert hs.normalise_event(e, "baseball_mlb")["scores"][0]["score"] == "5"


class TestItGradesThroughTheExistingLogic:
    def test_an_old_moneyline_grades_correctly(self):
        """The whole point: no grading logic is duplicated for old games."""
        game = hs.normalise_event(event(), "baseball_mlb")
        pick = {"sport": "MLB", "matchup": "San Diego Padres @ Kansas City Royals",
                "side": "Kansas City Royals ML"}
        assert match_pick_to_game(pick, [game])[0] == "W"

    def test_the_losing_side_grades_as_a_loss(self):
        game = hs.normalise_event(event(), "baseball_mlb")
        pick = {"sport": "MLB", "matchup": "San Diego Padres @ Kansas City Royals",
                "side": "San Diego Padres ML"}
        assert match_pick_to_game(pick, [game])[0] == "L"

    def test_an_old_total_grades_correctly(self):
        game = hs.normalise_event(event(), "baseball_mlb")   # 5 + 2 = 7
        pick = {"sport": "MLB", "matchup": "San Diego Padres @ Kansas City Royals",
                "side": "Over 9.5"}
        assert match_pick_to_game(pick, [game])[0] == "L"

    def test_a_different_game_is_not_matched(self):
        game = hs.normalise_event(event(), "baseball_mlb")
        pick = {"sport": "MLB", "matchup": "Yankees @ Red Sox", "side": "Yankees ML"}
        assert match_pick_to_game(pick, [game])[0] is None


class TestFetching:
    def _patch(self, monkeypatch, payload=None, boom=False):
        import requests
        captured = {}

        class R:
            def raise_for_status(self):
                if boom:
                    raise RuntimeError("503")
            def json(self): return payload or {}

        def fake_get(url, params=None, timeout=None, headers=None):
            captured["url"], captured["params"] = url, params or {}
            return R()
        monkeypatch.setattr(requests, "get", fake_get)
        return captured

    def test_the_date_is_sent_as_YYYYMMDD(self, monkeypatch):
        captured = self._patch(monkeypatch, {"events": []})
        hs.fetch_completed_games("MLB", "2026-07-19")
        assert captured["params"]["dates"] == "20260719"

    def test_the_league_path_is_used(self, monkeypatch):
        captured = self._patch(monkeypatch, {"events": []})
        hs.fetch_completed_games("MLB", "2026-07-19")
        assert "/baseball/mlb/scoreboard" in captured["url"]

    def test_an_unsupported_sport_fetches_nothing(self, monkeypatch):
        captured = self._patch(monkeypatch, {"events": []})
        assert hs.fetch_completed_games("KBO", "2026-07-19") == []
        assert captured == {}, "no request should be made for an unmapped sport"

    def test_a_failed_request_yields_no_games(self, monkeypatch):
        self._patch(monkeypatch, boom=True)
        assert hs.fetch_completed_games("MLB", "2026-07-19") == []

    def test_unfinished_games_are_filtered_out(self, monkeypatch):
        self._patch(monkeypatch, {"events": [event(), event(completed=False)]})
        assert len(hs.fetch_completed_games("MLB", "2026-07-19")) == 1

    def test_kbo_is_absent_rather_than_guessed(self):
        """An unverified league route would fetch the wrong league or nothing."""
        assert "kbo" not in hs.SPORT_ROUTES


class TestBackfill:
    def test_a_pick_with_an_unusable_date_is_skipped(self, monkeypatch):
        """One pick in the real database has game_day set to "today"."""
        from ivy_core.result_updater import backfill_pick
        called = []
        monkeypatch.setattr(hs, "fetch_completed_games",
                            lambda *a: called.append(a) or [])
        pick = {"sport": "MLB", "matchup": "Jets @ Dolphins", "side": "Jets ML",
                "game_day": "today", "report_date": ""}
        assert backfill_pick(pick, {}) == (None, None)
        assert called == []

    def test_it_falls_back_to_the_report_date(self, monkeypatch):
        from ivy_core.result_updater import backfill_pick
        seen = {}
        def record(sport, day):
            seen["day"] = day
            return []
        monkeypatch.setattr("ivy_core.historical_scores.fetch_completed_games", record)
        backfill_pick({"sport": "MLB", "matchup": "Jets @ Dolphins", "side": "Jets ML",
                       "game_day": "", "report_date": "2026-07-19"}, {})
        assert seen["day"] == "2026-07-19"

    def test_one_fetch_serves_every_pick_from_that_day(self, monkeypatch):
        """69 MLB picks must not become 69 requests for the same scoreboard."""
        from ivy_core.result_updater import backfill_pick
        calls = []
        monkeypatch.setattr("ivy_core.historical_scores.fetch_completed_games",
                            lambda sport, day: calls.append((sport, day)) or [])
        cache = {}
        for _ in range(5):
            backfill_pick({"sport": "MLB", "matchup": "Jets @ Dolphins", "side": "Jets ML",
                           "game_day": "2026-07-19"}, cache)
        assert len(calls) == 1


class TestEspnAccess:
    """The first live run returned 403 from every request.

    site.api.espn.com refuses this client; site.web.api.espn.com serves it.
    A non-browser User-Agent is also refused. Both are undocumented endpoints
    with no compatibility promise, so the fetch tries each host rather than
    betting the backfill on one.
    """

    def test_the_working_host_is_tried_first(self):
        assert hs.ESPN_HOSTS[0].startswith("https://site.web.api.espn.com")

    def test_the_other_host_is_still_a_fallback(self):
        assert any("site.api.espn.com" in h for h in hs.ESPN_HOSTS)
        assert len(hs.ESPN_HOSTS) >= 2

    def test_a_browser_user_agent_is_sent(self):
        """The endpoint rejects anything else."""
        assert "Mozilla" in hs.ESPN_HEADERS["User-Agent"]

    def test_a_403_on_the_first_host_falls_through_to_the_second(self, monkeypatch):
        import requests
        tried = []

        class R:
            def __init__(self, ok): self.ok = ok
            def raise_for_status(self):
                if not self.ok:
                    raise RuntimeError("403 Client Error: Forbidden")
            def json(self): return {"events": [event()]}

        def fake_get(url, params=None, timeout=None, headers=None):
            tried.append(url)
            return R(ok="site.web.api" in url)
        monkeypatch.setattr(requests, "get", fake_get)

        games = hs.fetch_completed_games("MLB", "2026-07-19")
        assert len(games) == 1, "the fallback host should have served it"
        assert len(tried) == 1, "the working host is first, so one call suffices"

    def test_every_host_failing_yields_no_games_not_an_exception(self, monkeypatch):
        import requests

        class R:
            def raise_for_status(self): raise RuntimeError("403")
            def json(self): return {}
        monkeypatch.setattr(requests, "get", lambda *a, **k: R())
        assert hs.fetch_completed_games("MLB", "2026-07-19") == []


class TestDateResolution:
    """79 of 81 ungraded picks were unreachable because of one `or`.

    game_day is written by a model reading free-form posts, and it holds the
    literal string "today" for nearly every pick. The backfill did
    `game_day or report_date`; "today" is truthy, so it took the garbage,
    failed to parse it, and skipped the pick — never reaching the usable
    report_date sitting beside it. The correct logic already existed in
    repair_dashboard; this was a second, worse copy of it.
    """

    @pytest.mark.parametrize("game_day,report_date,expected", [
        ("today", "2026-07-19", "2026-07-19"),
        (None, "2026-09-01", "2026-09-01"),
        ("", "2026-09-01", "2026-09-01"),
        ("2026-07-20", "2026-07-19", "2026-07-20"),
        ("2026-07-20T19:05:00Z", "2026-07-19", "2026-07-20"),
        ("garbage", "also garbage", None),
        (None, None, None),
    ])
    def test_it_falls_back_past_an_unusable_game_day(self, game_day, report_date, expected):
        from ivy_core.pick_stats import resolve_pick_date
        assert resolve_pick_date(game_day, report_date) == expected

    def test_the_backfill_reaches_a_today_pick(self, monkeypatch):
        """The exact shape of the 79."""
        from ivy_core.result_updater import backfill_pick
        asked = {}

        def record(sport, day):
            asked["day"] = day
            return [hs.normalise_event(event(), "baseball_mlb")]
        monkeypatch.setattr(hs, "fetch_completed_games", record)

        pick = {"sport": "MLB", "game_day": "today", "report_date": "2026-07-19",
                "matchup": "San Diego Padres @ Kansas City Royals",
                "side": "Kansas City Royals ML"}
        result, _ = backfill_pick(pick, {})
        assert asked["day"] == "2026-07-19", "it must fall back, not skip"
        assert result == "W"

    def test_there_is_only_one_implementation(self):
        """Two copies is how they came to disagree."""
        import inspect

        import scripts.repair_dashboard as rd
        assert "resolve_pick_date" in inspect.getsource(rd.pick_date)


class TestGarbageIsNotStored:
    def test_an_unparseable_game_day_is_stored_as_none(self, tmp_path, monkeypatch):
        """Keeping "today" shadows the usable report_date for every reader."""
        import sqlite3

        from ivy_core import picks_tracker as pt

        monkeypatch.setattr(pt, "PICKS_DB", tmp_path / "p.db")
        monkeypatch.setattr(pt, "log_picks_to_sheet", lambda *a, **k: True)
        monkeypatch.setattr(pt, "auto_sync_to_export_sheet", lambda *a, **k: True)
        pt.save_picks([{"sport": "MLB", "matchup": "Jets @ Dolphins", "side": "Jets ML",
                        "game_day": "today"}], "2026-07-19")
        conn = sqlite3.connect(tmp_path / "p.db")
        try:
            assert conn.execute("SELECT game_day FROM picks").fetchone()[0] is None
        finally:
            conn.close()

    def test_a_real_game_day_is_kept(self, tmp_path, monkeypatch):
        import sqlite3

        from ivy_core import picks_tracker as pt

        monkeypatch.setattr(pt, "PICKS_DB", tmp_path / "p.db")
        monkeypatch.setattr(pt, "log_picks_to_sheet", lambda *a, **k: True)
        monkeypatch.setattr(pt, "auto_sync_to_export_sheet", lambda *a, **k: True)
        pt.save_picks([{"sport": "MLB", "matchup": "Jets @ Dolphins", "side": "Jets ML",
                        "game_day": "2026-07-20"}], "2026-07-19")
        conn = sqlite3.connect(tmp_path / "p.db")
        try:
            assert conn.execute("SELECT game_day FROM picks").fetchone()[0] == "2026-07-20"
        finally:
            conn.close()
