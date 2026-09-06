"""The consolidated brief, and the isolation that makes one message safe.

Seven separate digests would have been seven chances a day to train Henry to
stop reading. Consolidating them means one dead RSS feed could cost him the
whole brief instead of one section, so isolation is not a nicety here — it is
the thing that makes consolidation safe.

The other rule under test: numbers come from the fetched payload or they are
not printed. A confabulated closing price reads exactly as confidently as a
real one.
"""
from __future__ import annotations

from datetime import datetime

from proactive_agents import daily_brief as db
from proactive_agents.daily_brief import Block, BlockResult


def ok(text="fine", key="x"):
    return Block(key, f"T {key}", db.MORNING, lambda: BlockResult(text, sources=["src"]))


def boom(key="bad", exc=RuntimeError("feed is down")):
    def _f():
        raise exc
    return Block(key, f"T {key}", db.MORNING, _f)


def unavailable(key="u", why="no feed"):
    return Block(key, f"T {key}", db.MORNING, lambda: BlockResult.unavailable(why))


class TestBlockIsolation:
    def test_one_raising_block_does_not_lose_the_others(self):
        """The whole argument for one message instead of seven."""
        body, detail = db.build_brief(db.MORNING, [ok(key="a"), boom(), ok(key="b")])
        assert "T a" in body and "T b" in body
        assert len(detail["blocks"]) == 3

    def test_a_raising_block_is_reported_not_hidden(self):
        body, detail = db.build_brief(db.MORNING, [boom()])
        assert "unavailable" in body
        assert detail["blocks"][0]["failed"] is True

    def test_the_exception_type_reaches_the_reader(self):
        body, _ = db.build_brief(db.MORNING, [boom(exc=TimeoutError("slow"))])
        assert "TimeoutError" in body

    def test_a_healthy_block_is_not_marked_failed(self):
        _, detail = db.build_brief(db.MORNING, [ok()])
        assert detail["blocks"][0]["failed"] is False

    def test_total_failure_says_so_once(self):
        """Distinguishing 'nothing happened' from 'nothing was fetched'."""
        body, _ = db.build_brief(db.MORNING, [boom("a"), boom("b")])
        assert body.count("Every section failed") == 1

    def test_partial_failure_does_not_claim_total_failure(self):
        body, _ = db.build_brief(db.MORNING, [ok(), boom()])
        assert "Every section failed" not in body


class TestSlots:
    def test_only_this_slot_runs(self):
        morning = Block("m", "M", db.MORNING, lambda: BlockResult("m"))
        evening = Block("e", "E", db.EVENING, lambda: BlockResult("e"))
        body, _ = db.build_brief(db.MORNING, [morning, evening])
        assert "M" in body and "E" not in body

    def test_a_disabled_block_is_skipped(self):
        off = Block("off", "OFF", db.MORNING, lambda: BlockResult("no"), enabled=False)
        _, detail = db.build_brief(db.MORNING, [off, ok()])
        assert [b["key"] for b in detail["blocks"]] == ["x"]

    def test_the_heading_names_the_slot(self):
        m, _ = db.build_brief(db.MORNING, [ok()], now=datetime(2026, 9, 6))
        e, _ = db.build_brief(db.EVENING, [], now=datetime(2026, 9, 6))
        assert m.startswith("Morning brief")
        assert e.startswith("Evening brief")

    def test_the_shipped_blocks_are_split_across_both_slots(self):
        assert db.blocks_for(db.MORNING) and db.blocks_for(db.EVENING)


class TestWeather:
    NWS = {
        "points": {"properties": {"forecast": "https://api.weather.gov/x/forecast"}},
        "forecast": {"properties": {"periods": [
            {"name": "Today", "temperature": 94, "temperatureUnit": "F",
             "shortForecast": "Sunny", "detailedForecast": "Hot and clear."},
            {"name": "Tonight", "temperature": 73, "temperatureUnit": "F",
             "shortForecast": "Clear"},
        ]}},
    }

    def _patch(self, monkeypatch, payloads):
        calls = iter(payloads)
        monkeypatch.setattr(db, "_get_json", lambda url: next(calls))

    def test_reports_the_figures_verbatim(self, monkeypatch):
        """The temperature is printed from the payload, not described."""
        self._patch(monkeypatch, [self.NWS["points"], self.NWS["forecast"]])
        r = db.fetch_weather()
        assert "94°F" in r.text and "Sunny" in r.text
        assert not r.failed

    def test_it_includes_tonight(self, monkeypatch):
        self._patch(monkeypatch, [self.NWS["points"], self.NWS["forecast"]])
        assert "Tonight: 73°F" in db.fetch_weather().text

    def test_it_names_its_source(self, monkeypatch):
        self._patch(monkeypatch, [self.NWS["points"], self.NWS["forecast"]])
        assert db.fetch_weather().sources == ["api.weather.gov"]

    def test_an_unreachable_api_is_unavailable_not_invented(self, monkeypatch):
        def die(url):
            raise ConnectionError("no route")
        monkeypatch.setattr(db, "_get_json", die)
        r = db.fetch_weather()
        assert r.failed and "ConnectionError" in r.text

    def test_an_empty_forecast_is_not_rendered_as_weather(self, monkeypatch):
        self._patch(monkeypatch, [self.NWS["points"],
                                  {"properties": {"periods": []}}])
        assert db.fetch_weather().failed


class TestMarketClose:
    CSV = ("Symbol,Date,Time,Open,High,Low,Close,Volume\n"
           "^SPX,2026-09-05,22:00:00,5500.00,5560.00,5490.00,5555.00,0\n")

    def _resp(self, text="", status_ok=True):
        class R:
            def __init__(self): self.text = text
            def raise_for_status(self):
                if not status_ok:
                    raise RuntimeError("503")
        return R()

    def test_close_and_move_come_from_the_payload(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._resp(self.CSV))
        r = db.fetch_market_close()
        assert "5,555.00" in r.text
        assert "+1.00%" in r.text, "1.00% is (5555-5500)/5500 — computed, not guessed"

    def test_a_failing_symbol_is_marked_unavailable_not_omitted(self, monkeypatch):
        """A missing index must not silently vanish from the list."""
        import requests
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: self._resp(self.CSV, status_ok=False))
        r = db.fetch_market_close()
        assert r.failed
        assert "unavailable" in r.text

    def test_every_configured_index_appears(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._resp(self.CSV))
        text = db.fetch_market_close().text
        for label, _ in db.MARKET_SYMBOLS:
            assert label in text

    def test_a_zero_open_does_not_divide_by_zero(self, monkeypatch):
        import requests
        csv = self.CSV.replace("5500.00,5560.00,5490.00", "0.00,5560.00,5490.00")
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._resp(csv))
        assert "+0.00%" in db.fetch_market_close().text


class TestFeeds:
    class FakeFeed:
        def __init__(self, titles):
            self.entries = [{"title": t} for t in titles]

    def _patch(self, monkeypatch, mapping):
        import feedparser
        monkeypatch.setattr(
            feedparser, "parse",
            lambda url, agent=None: self.FakeFeed(mapping.get(url, [])))

    def test_headlines_are_passed_through_unaltered(self, monkeypatch):
        """Nothing summarises these, so nothing can invent one."""
        feeds = [("A", "http://a")]
        self._patch(monkeypatch, {"http://a": ["Real headline here"]})
        r = db._feed_items(feeds)
        assert "Real headline here" in r.text

    def test_duplicates_across_feeds_appear_once(self, monkeypatch):
        feeds = [("A", "http://a"), ("B", "http://b")]
        self._patch(monkeypatch, {"http://a": ["Same story"], "http://b": ["Same story"]})
        assert db._feed_items(feeds).text.count("Same story") == 1

    def test_it_respects_the_item_limit(self, monkeypatch):
        feeds = [("A", "http://a")]
        self._patch(monkeypatch, {"http://a": [f"h{i}" for i in range(20)]})
        assert len(db._feed_items(feeds, limit=3).text.splitlines()) == 3

    def test_a_dead_feed_is_named_but_the_others_still_report(self, monkeypatch):
        feeds = [("Dead", "http://dead"), ("Live", "http://live")]
        self._patch(monkeypatch, {"http://live": ["Working headline"]})
        r = db._feed_items(feeds)
        assert "Working headline" in r.text
        assert "Dead" in r.text and not r.failed

    def test_all_feeds_dead_is_a_failure(self, monkeypatch):
        self._patch(monkeypatch, {})
        r = db._feed_items([("A", "http://a")])
        assert r.failed

    def test_sources_record_which_feeds_answered(self, monkeypatch):
        feeds = [("A", "http://a"), ("B", "http://b")]
        self._patch(monkeypatch, {"http://a": ["x"]})
        assert db._feed_items(feeds).sources == ["A"]


class TestDetailForReplies:
    def test_detail_carries_sources_so_WHY_can_answer(self):
        _, detail = db.build_brief(db.MORNING, [ok()])
        assert detail["blocks"][0]["sources"] == ["src"]

    def test_detail_records_the_slot_and_time(self):
        _, detail = db.build_brief(db.MORNING, [ok()], now=datetime(2026, 9, 6, 7))
        assert detail["slot"] == db.MORNING
        assert detail["generated"].startswith("2026-09-06")


class TestRun:
    def test_dry_run_sends_nothing(self, monkeypatch, capsys):
        def must_not_send(*a, **k):
            raise AssertionError("dry run must not deliver")
        monkeypatch.setattr(db, "deliver_report", must_not_send)
        out = db.run(db.MORNING, send=False, blocks=[ok()])
        assert out["status"] == "dry-run"

    def test_send_reports_delivery_failure_honestly(self, monkeypatch):
        class Result:
            delivered, report_id = False, "BR-1"
        monkeypatch.setattr(db, "deliver_report", lambda *a, **k: Result())
        monkeypatch.setattr(db, "require_env", lambda k: "+15555550100")
        assert db.run(db.MORNING, send=True, blocks=[ok()])["status"] == "failed"

    def test_the_summary_counts_working_sections(self, monkeypatch):
        captured = {}

        class Result:
            delivered, report_id = True, "BR-1"

        def fake(phone, **kw):
            captured.update(kw)
            return Result()
        monkeypatch.setattr(db, "deliver_report", fake)
        monkeypatch.setattr(db, "require_env", lambda k: "+15555550100")
        db.run(db.MORNING, send=True, blocks=[ok("a"), boom("b")])
        assert captured["content_summary"] == "1/2 section(s)"


class TestReadwiseReview:
    """The Daily Review block.

    Readwise's own spaced-repetition pick, which is why this block has no
    tuning: it takes no parameters. The one thing it must get right is
    attribution — the highlights endpoint the rest of the codebase used has
    no title field, so every highlight was labelled "Saved Article", which
    looks like attribution while being none.
    """

    PAYLOAD = {
        "review_id": 1,
        "review_completed": False,
        "highlights": [
            {"text": "The cost of a thing is the amount of life exchanged for it.",
             "title": "Walden", "author": "Henry David Thoreau"},
            {"text": "Second highlight.", "title": "Another Book", "author": "Someone"},
            {"text": "Third — beyond the limit.", "title": "Third", "author": "X"},
        ],
    }

    def _patch(self, monkeypatch, payload, ok=True, key="tok"):
        import requests

        class R:
            def json(self): return payload
            def raise_for_status(self):
                if not ok:
                    raise RuntimeError("401")
        monkeypatch.setenv("READWISE_API_KEY", key)
        monkeypatch.setattr(requests, "get", lambda *a, **k: R())

    def test_the_highlight_is_quoted_verbatim(self, monkeypatch):
        self._patch(monkeypatch, self.PAYLOAD)
        r = db.fetch_readwise_review()
        assert "amount of life exchanged for it" in r.text
        assert not r.failed

    def test_it_says_where_the_highlight_came_from(self, monkeypatch):
        """The defect this replaces: every highlight read 'Saved Article'."""
        self._patch(monkeypatch, self.PAYLOAD)
        text = db.fetch_readwise_review().text
        assert "Walden" in text and "Thoreau" in text
        assert "Saved Article" not in text

    def test_it_shows_at_most_two(self, monkeypatch):
        self._patch(monkeypatch, self.PAYLOAD)
        assert "beyond the limit" not in db.fetch_readwise_review().text

    def test_a_long_highlight_is_truncated_visibly(self, monkeypatch):
        self._patch(monkeypatch, {"highlights": [{"text": "x" * 400, "title": "T"}]})
        text = db.fetch_readwise_review().text
        assert "…" in text and len(text) < 400

    def test_a_missing_author_does_not_print_a_dangling_dash(self, monkeypatch):
        self._patch(monkeypatch, {"highlights": [{"text": "Quote.", "title": "Book"}]})
        assert "— \n" not in db.fetch_readwise_review().text

    def test_no_key_is_unavailable_not_an_exception(self, monkeypatch):
        monkeypatch.setenv("READWISE_API_KEY", "")
        r = db.fetch_readwise_review()
        assert r.failed and "READWISE_API_KEY" in r.text

    def test_an_api_error_is_unavailable(self, monkeypatch):
        self._patch(monkeypatch, {}, ok=False)
        assert db.fetch_readwise_review().failed

    def test_an_empty_review_says_so(self, monkeypatch):
        self._patch(monkeypatch, {"highlights": []})
        r = db.fetch_readwise_review()
        assert r.failed and "no highlights" in r.text

    def test_it_names_its_source(self, monkeypatch):
        self._patch(monkeypatch, self.PAYLOAD)
        assert db.fetch_readwise_review().sources == ["readwise.io/api/v2/review"]

    def test_it_is_in_the_morning_brief(self):
        assert "readwise" in [b.key for b in db.blocks_for(db.MORNING)]
