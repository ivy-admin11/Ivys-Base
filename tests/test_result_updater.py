"""Tests for the code that decides whether a pick won.

result_updater had no tests at all. It is the module that writes W/L/P back to
the Sheets dashboard, so a fault here does not crash anything -- it quietly
produces a wrong record, which is worse than an outage because nothing looks
unusual. Two live bugs were found while writing these and are pinned below.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from ivy_core.result_updater import (
    _extract_moneyline_result,
    _extract_over_under_result,
    _match_teams_in_matchup,
    _normalize_team,
    _score_for,
    parse_score,
)


def game(home: str, away: str, home_score, away_score, *, reverse: bool = False) -> dict:
    """A completed game as The Odds API reports it.

    reverse=True emits the scores away-first, which the API does not promise
    against and which the grader used to read positionally.
    """
    entries = [
        {"name": home, "score": home_score},
        {"name": away, "score": away_score},
    ]
    return {
        "home_team": home,
        "away_team": away,
        "completed": True,
        "scores": list(reversed(entries)) if reverse else entries,
    }


class TestParseScore:
    @pytest.mark.parametrize(
        "raw,expected",
        [(7, 7), ("7", 7), (7.0, 7), ("7.5", Decimal("7.5")), (0, 0), ("0", 0)],
    )
    def test_parses_every_shape_the_api_sends(self, raw, expected):
        assert parse_score(raw) == Decimal(str(expected))

    @pytest.mark.parametrize("raw", [None, "", "TBD", "postponed", {}, []])
    def test_unparseable_is_none_not_zero(self, raw):
        # Returning 0 here would grade an unplayed game as a 0-0 push.
        assert parse_score(raw) is None

    def test_zero_is_a_real_score_not_a_missing_one(self):
        assert parse_score(0) is not None
        assert parse_score(0) == Decimal(0)


class TestMatchupMatching:
    def test_multi_word_teams_match(self):
        """Regression: the matchup kept its spaces while team names lost theirs.

        Every multi-word team -- which is nearly all of them -- failed to match,
        so those picks were never graded at all.
        """
        assert _match_teams_in_matchup(
            "Kansas City Chiefs @ Denver Broncos",
            "Denver Broncos",
            "Kansas City Chiefs",
        )

    def test_single_word_teams_still_match(self):
        assert _match_teams_in_matchup("Miami @ Texas", "Texas", "Miami")

    def test_apostrophes_and_case_are_ignored(self):
        assert _match_teams_in_matchup("Ohio State vs Texas A&M", "Texas A&M", "Ohio State")

    def test_one_team_present_is_not_a_match(self):
        # Guards against grading a pick against the wrong game.
        assert not _match_teams_in_matchup("Texas @ Alabama", "Georgia", "Texas")

    def test_neither_team_present(self):
        assert not _match_teams_in_matchup("Texas @ Alabama", "Duke", "Kansas")


class TestScoreLookup:
    def test_finds_by_name_regardless_of_order(self):
        scores = [{"name": "Away Team", "score": "3"}, {"name": "Home Team", "score": "10"}]
        assert _score_for(scores, "Home Team", 0) == Decimal(10)
        assert _score_for(scores, "Away Team", 1) == Decimal(3)

    def test_falls_back_to_position_when_names_are_missing(self):
        scores = [{"score": "10"}, {"score": "3"}]
        assert _score_for(scores, "Home Team", 0) == Decimal(10)

    def test_missing_index_is_none(self):
        assert _score_for([], "Home Team", 0) is None


class TestMoneyline:
    def test_home_win(self):
        assert _extract_moneyline_result(game("Rays", "Royals", 5, 2), "Rays ML") == "W"

    def test_home_loss(self):
        assert _extract_moneyline_result(game("Rays", "Royals", 1, 4), "Rays ML") == "L"

    def test_away_win(self):
        assert _extract_moneyline_result(game("Rays", "Royals", 1, 4), "Royals ML") == "W"

    def test_tie_is_a_push(self):
        assert _extract_moneyline_result(game("Rays", "Royals", 3, 3), "Rays ML") == "P"

    def test_away_first_scores_do_not_invert_the_result(self):
        """Regression: scores were read positionally.

        The API does not guarantee home-first ordering. Reading by index turned
        every win into a loss whenever it flipped.
        """
        g = game("Rays", "Royals", 5, 2, reverse=True)
        assert _extract_moneyline_result(g, "Rays ML") == "W"
        assert _extract_moneyline_result(g, "Royals ML") == "L"

    def test_multi_word_team_grades(self):
        g = game("Kansas City Chiefs", "Denver Broncos", 31, 17)
        assert _extract_moneyline_result(g, "Kansas City Chiefs ML") == "W"

    def test_unplayed_game_is_ungraded(self):
        assert _extract_moneyline_result(game("Rays", "Royals", None, None), "Rays ML") is None

    def test_unknown_team_is_ungraded_not_guessed(self):
        assert _extract_moneyline_result(game("Rays", "Royals", 5, 2), "Yankees ML") is None


class TestOverUnder:
    @pytest.mark.parametrize(
        "side,home,away,expected",
        [
            ("Over 9.5", 6, 5, "W"),
            ("Over 9.5", 4, 5, "L"),
            ("Under 9.5", 4, 5, "W"),
            ("Under 9.5", 6, 5, "L"),
            ("Over 45", 24, 21, "P"),
            ("Under 45", 24, 21, "P"),
        ],
    )
    def test_totals(self, side, home, away, expected):
        assert _extract_over_under_result(game("A", "B", home, away), side) == expected

    def test_total_is_order_independent(self):
        g = game("A", "B", 6, 5, reverse=True)
        assert _extract_over_under_result(g, "Over 9.5") == "W"

    def test_side_without_a_number_is_ungraded(self):
        assert _extract_over_under_result(game("A", "B", 6, 5), "Over") is None

    def test_unplayed_game_is_ungraded(self):
        assert _extract_over_under_result(game("A", "B", None, 5), "Over 9.5") is None


def test_normalize_team_is_stable():
    assert _normalize_team("Kansas City Chiefs") == "kansascitychiefs"
    assert _normalize_team("St. Louis") == _normalize_team("st. louis")
    assert _normalize_team("") == ""
    assert _normalize_team(None) == ""


class TestGradingFallsBackToESPN:
    """Grading had one source, and it was the one that had run out.

    The Odds API /scores endpoint bills 2 credits per sport per call and the
    account has sat at its monthly ceiling since July, so auto_update_results
    fetched 0 completed games on every run and graded nothing. Ivy noticed the
    consequence and warned that "4 picks are about to become ungradeable",
    offering repair_dashboard.py --apply — which marks them unverifiable. That
    would have thrown away results that were still there to be had, because
    ESPN carries the same finals for free.

    The window is what makes this permanent rather than late: the Odds API
    scores window caps at 3 days, so a pick that goes ungraded long enough can
    never be graded at all.
    """

    def _pending_row(self, pid=1, sport="MLB", matchup="Texas Rangers @ Seattle Mariners",
                     side="Seattle Mariners ML", game_day=None, report_date="2026-09-09"):
        return (pid, sport, matchup, side, game_day, 1, "@someone", report_date)

    def test_espn_is_tried_when_the_paid_feed_returns_nothing(self, monkeypatch, tmp_path):
        import ivy_core.result_updater as ru

        db = tmp_path / "picks.db"
        self._seed(db, ru, monkeypatch)
        monkeypatch.setattr(ru, "get_completed_games", lambda **k: [])

        called = {}

        def fake_backfill(pick, cache):
            called["pick"] = pick
            return "W", "rangers 2 vs mariners 3"

        monkeypatch.setattr(ru, "backfill_pick", fake_backfill)
        monkeypatch.setattr(ru, "update_pick_result", lambda *a, **k: True)
        out = ru.auto_update_results()

        assert called, "an empty paid feed must not end the run"
        assert out["updated"] == 1

    def test_the_report_date_reaches_the_espn_lookup(self, monkeypatch, tmp_path):
        """game_day is routinely not a date — "today" in 79 of the first 88
        picks — so it is stored null and the day comes from report_date. Without
        it the lookup has no day to ask about and grades nothing, silently."""
        import ivy_core.result_updater as ru

        db = tmp_path / "picks.db"
        self._seed(db, ru, monkeypatch)
        monkeypatch.setattr(ru, "get_completed_games", lambda **k: [])

        seen = {}
        monkeypatch.setattr(ru, "backfill_pick",
                            lambda pick, cache: seen.update(pick) or (None, None))
        ru.auto_update_results()
        assert seen.get("report_date") == "2026-09-09"

    def test_an_espn_failure_never_crashes_the_run(self, monkeypatch, tmp_path):
        import ivy_core.result_updater as ru

        db = tmp_path / "picks.db"
        self._seed(db, ru, monkeypatch)
        monkeypatch.setattr(ru, "get_completed_games", lambda **k: [])

        def boom(pick, cache):
            raise RuntimeError("ESPN is down")

        monkeypatch.setattr(ru, "backfill_pick", boom)
        out = ru.auto_update_results()
        assert out["updated"] == 0, "a dead fallback leaves picks pending, not crashed"

    def test_the_scoreboard_cache_is_shared_across_picks(self, monkeypatch, tmp_path):
        """One scoreboard fetch per sport-day, not one per pick."""
        import ivy_core.result_updater as ru

        db = tmp_path / "picks.db"
        self._seed(db, ru, monkeypatch, count=4)
        monkeypatch.setattr(ru, "get_completed_games", lambda **k: [])

        caches = []
        monkeypatch.setattr(ru, "backfill_pick",
                            lambda pick, cache: caches.append(id(cache)) or (None, None))
        ru.auto_update_results()
        assert len(set(caches)) == 1, "every pick must share one cache"

    @staticmethod
    def _seed(db, ru, monkeypatch, count=1):
        import sqlite3
        con = sqlite3.connect(db)
        con.executescript(
            "CREATE TABLE picks (id INTEGER PRIMARY KEY, sport TEXT, matchup TEXT,"
            " side TEXT, game_day TEXT, sharp_count INT, handicapper TEXT,"
            " report_date TEXT, created_at TEXT);"
            "CREATE TABLE results (pick_id INT, result TEXT);"
        )
        for i in range(count):
            con.execute(
                "INSERT INTO picks (sport, matchup, side, game_day, sharp_count,"
                " handicapper, report_date, created_at) VALUES"
                " ('MLB','Texas Rangers @ Seattle Mariners','Seattle Mariners ML',"
                " NULL, 1, '@someone', '2026-09-09', '2026-09-09')")
        con.commit()
        con.close()
        monkeypatch.setattr(ru, "PICKS_DB", db)
