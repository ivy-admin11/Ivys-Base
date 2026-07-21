"""ivy_core.picks_tracker: canonical identity, idempotent persistence,
migration/backfill/dedup, conflict recording, and statistics.

Every test uses the tmp_path-backed PICKS_DB from conftest's
isolated_picks_db fixture — never the real data/picks.db.
"""

import sqlite3

from ivy_core import picks_tracker as pt


def _pick(**overrides):
    base = {
        "sport": "NFL",
        "matchup": "Kansas City Chiefs @ Buffalo Bills",
        "side": "Kansas City Chiefs -2.5",
        "odds": "-110",
        "handicappers": ["sharp1", "sharp2"],
        "confidence": "high",
        "game_day": "today",
        "start": "2026-08-01T20:00:00Z",
        "reasoning": "test reasoning",
    }
    base.update(overrides)
    return base


def test_repeating_identical_pick_produces_one_canonical_pick_and_one_result_row():
    pt.save_picks([_pick()], report_date="2026-07-21")
    pt.save_picks([_pick()], report_date="2026-07-21")

    conn = sqlite3.connect(pt.PICKS_DB)
    pick_rows = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
    result_rows = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    conn.close()

    assert pick_rows == 1
    assert result_rows == 1


def test_changed_odds_and_backers_do_not_create_another_pick():
    pt.save_picks([_pick(odds="-110", handicappers=["sharp1"])], report_date="2026-07-21")
    summary = pt.save_picks(
        [_pick(odds="-105", handicappers=["sharp1", "sharp2", "sharp3"])],
        report_date="2026-07-21",
    )

    assert summary == {"inserted": 0, "updated": 1, "total": 1}

    rows = pt.get_canonical_snapshot_rows()
    assert len(rows) == 1
    # Highest sharp count observed is retained.
    assert rows[0]["sharp_count"] == 3
    # Odds were already set on first insert — COALESCE keeps the original.
    assert rows[0]["odds"] == -110.0


def test_same_matchup_and_side_on_a_different_event_remains_distinct():
    pt.save_picks([_pick(start="2026-08-01T20:00:00Z")], report_date="2026-07-21")
    pt.save_picks([_pick(start="2026-08-08T20:00:00Z")], report_date="2026-07-28")

    rows = pt.get_canonical_snapshot_rows()
    assert len(rows) == 2


def test_report_date_and_game_day_fallback_when_no_start_time():
    pick_a = _pick(start=None, game_day="today")
    pick_b = _pick(start=None, game_day="tomorrow")
    key_a = pt.compute_pick_key({**pick_a, "report_date": "2026-07-21"})
    key_b = pt.compute_pick_key({**pick_b, "report_date": "2026-07-21"})
    assert key_a != key_b


def test_provider_event_id_takes_precedence_over_matchup_text():
    key_1 = pt.compute_pick_key({"provider_event_id": "evt123", "side": "Chiefs -2.5", "sport": "NFL"})
    key_2 = pt.compute_pick_key({
        "provider_event_id": "evt123", "side": "Chiefs -2.5", "sport": "NFL",
        "matchup": "totally different matchup text",
    })
    assert key_1 == key_2


def test_historical_duplicates_are_marked_not_deleted():
    """Simulate legacy pre-migration rows (no pick_key) with a duplicate,
    then trigger the migration via _init_db and confirm both rows survive."""
    conn = sqlite3.connect(pt.PICKS_DB)
    pt._create_base_tables(conn)
    for _ in range(2):
        conn.execute(
            "INSERT INTO picks (sport, matchup, side, report_date, sharp_count) "
            "VALUES ('NFL', 'A @ B', 'A -3', '2026-07-01', 1)"
        )
        pick_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO results (pick_id) VALUES (?)", (pick_id,))
    conn.commit()
    conn.close()

    pt._init_db()

    conn = sqlite3.connect(pt.PICKS_DB)
    total_rows = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
    canonical_rows = conn.execute(f"SELECT COUNT(*) FROM picks WHERE {pt._CANONICAL_FILTER}").fetchone()[0]
    conn.close()

    assert total_rows == 2, "no row should be deleted by migration"
    assert canonical_rows == 1, "exactly one row per pick_key should remain canonical"


def test_existing_resolved_outcome_is_carried_to_canonical_during_migration():
    conn = sqlite3.connect(pt.PICKS_DB)
    pt._create_base_tables(conn)
    conn.execute(
        "INSERT INTO picks (sport, matchup, side, report_date, sharp_count) "
        "VALUES ('NFL', 'A @ B', 'A -3', '2026-07-01', 1)"
    )
    canonical_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO results (pick_id) VALUES (?)", (canonical_id,))

    conn.execute(
        "INSERT INTO picks (sport, matchup, side, report_date, sharp_count) "
        "VALUES ('NFL', 'A @ B', 'A -3', '2026-07-01', 1)"
    )
    dup_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO results (pick_id, result, final_score) VALUES (?, 'W', '10-3')", (dup_id,)
    )
    conn.commit()
    conn.close()

    pt._init_db()

    conn = sqlite3.connect(pt.PICKS_DB)
    canonical_result = conn.execute(
        "SELECT result, final_score FROM results WHERE pick_id = ?", (canonical_id,)
    ).fetchone()
    dup_result = conn.execute(
        "SELECT result, final_score FROM results WHERE pick_id = ?", (dup_id,)
    ).fetchone()
    conn.close()

    assert canonical_result == ("W", "10-3"), "resolved outcome must be carried to the canonical row"
    assert dup_result == ("W", "10-3"), "the original audit row must be preserved untouched"


def test_conflicting_historical_outcomes_are_recorded_not_silently_resolved():
    conn = sqlite3.connect(pt.PICKS_DB)
    pt._create_base_tables(conn)
    ids = []
    for _ in range(2):
        conn.execute(
            "INSERT INTO picks (sport, matchup, side, report_date, sharp_count) "
            "VALUES ('NFL', 'A @ B', 'A -3', '2026-07-01', 1)"
        )
        ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute("INSERT INTO results (pick_id, result) VALUES (?, 'W')", (ids[0],))
    conn.execute("INSERT INTO results (pick_id, result) VALUES (?, 'L')", (ids[1],))
    conn.commit()
    conn.close()

    pt._init_db()

    conflicts = pt.get_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["canonical_id"] == ids[0]

    # The canonical row's own result must be left exactly as it was — not
    # silently overwritten by the conflicting duplicate's outcome.
    conn = sqlite3.connect(pt.PICKS_DB)
    canonical_result = conn.execute(
        "SELECT result FROM results WHERE pick_id = ?", (ids[0],)
    ).fetchone()[0]
    conn.close()
    assert canonical_result == "W"


def test_statistics_exclude_duplicate_audit_rows():
    conn = sqlite3.connect(pt.PICKS_DB)
    pt._create_base_tables(conn)
    conn.execute(
        "INSERT INTO picks (sport, matchup, side, report_date, sharp_count) "
        "VALUES ('NFL', 'A @ B', 'A -3', '2026-07-01', 1)"
    )
    canonical_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO results (pick_id, result) VALUES (?, 'W')", (canonical_id,))

    conn.execute(
        "INSERT INTO picks (sport, matchup, side, report_date, sharp_count) "
        "VALUES ('NFL', 'A @ B', 'A -3', '2026-07-01', 1)"
    )
    dup_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO results (pick_id, result) VALUES (?, 'L')", (dup_id,))
    conn.commit()
    conn.close()

    pt._init_db()  # migration marks dup_id as duplicate_of=canonical_id

    overall = pt.get_stats_overall()
    # Only the canonical row's own result ('W') should be counted — the
    # duplicate's 'L' result row must not double-count into statistics.
    assert overall["wins"] == 1
    assert overall["losses"] == 0
    assert overall["resolved"] == 1


def test_ratio_calculations_handle_zero_and_nonzero_denominators():
    zero = pt._record_from_counts(0, 0, 0, 5)
    assert zero["win_ratio"] == 0.0
    assert zero["decisive_hit_rate"] == 0.0
    assert zero["record"] == "0-0-0"

    nonzero = pt._record_from_counts(3, 1, 1, 2)
    assert nonzero["resolved"] == 5
    assert nonzero["win_ratio"] == 3 / 5
    assert nonzero["loss_ratio"] == 1 / 5
    assert nonzero["push_ratio"] == 1 / 5
    assert nonzero["decisive_hit_rate"] == 3 / 4
    assert nonzero["record"] == "3-1-1"


def test_get_stats_by_sport_groups_correctly():
    pt.save_picks([_pick(sport="NFL", matchup="A @ B", side="A -3")], report_date="2026-07-21")
    pt.save_picks([_pick(sport="NBA", matchup="C @ D", side="C -3")], report_date="2026-07-21")

    by_sport = pt.get_stats_by_sport()
    assert set(by_sport.keys()) == {"NFL", "NBA"}
    assert by_sport["NFL"]["pending"] == 1
    assert by_sport["NBA"]["pending"] == 1
