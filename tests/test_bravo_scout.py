"""Bravo Scout: drama ranking, TVmaze schedule shaping, recommendation
grounding, and the no-fabrication contract. No network calls, no texts."""

from datetime import date, timedelta

import pytest

from proactive_agents import bravo_scout as bs


# ---------------------------------------------------------------------------
# drama ranking
# ---------------------------------------------------------------------------

def test_drama_score_ranks_scandal_over_routine_news():
    assert bs.drama_score("Jax files restraining order against Brittany") > \
           bs.drama_score("Bravo renews Below Deck for season 12")
    assert bs.drama_score("Star quits after explosive reunion feud") > \
           bs.drama_score("First look at the new season trailer")


def test_drama_score_is_additive_and_case_insensitive():
    # "fired" (5) + "feud" (3) beats either alone.
    both = bs.drama_score("FIRED amid cast FEUD")
    assert both > bs.drama_score("fired")
    assert both > bs.drama_score("feud")
    assert bs.drama_score("nothing notable happened") == 0


def test_headlines_come_back_juiciest_first(monkeypatch):
    entries = [
        {"title": "Bravo renews Summer House for another season", "summary": ""},
        {"title": "Vanderpump Rules star arrested after explosive feud", "summary": ""},
        {"title": "Below Deck teases first look at new charter", "summary": ""},
    ]

    class Feed:
        def __init__(self, e):
            self.entries = e

    monkeypatch.setattr(bs, "NEWS_FEEDS", [("Test", "http://example.invalid/feed")])
    monkeypatch.setattr(bs.feedparser, "parse", lambda url, agent=None: Feed(entries))

    items = bs.fetch_entertainment_feeds()
    assert [i["headline"][:12] for i in items][0].startswith("Vanderpump")
    assert items[0]["drama"] > items[-1]["drama"]


def test_feed_failure_never_breaks_the_brief(monkeypatch):
    def boom(url, agent=None):
        raise OSError("feed down")

    monkeypatch.setattr(bs, "NEWS_FEEDS", [("Broken", "http://example.invalid/feed")])
    monkeypatch.setattr(bs.feedparser, "parse", boom)
    assert bs.fetch_entertainment_feeds() == []


# ---------------------------------------------------------------------------
# reddit pacing
# ---------------------------------------------------------------------------

def test_social_stops_after_first_feed_that_satisfies_the_target(monkeypatch):
    hits = []

    class Feed:
        status = 200
        entries = [{"title": f"drama post {i}"} for i in range(25)]

    def fake_parse(url, agent=None):
        hits.append(url)
        return Feed()

    monkeypatch.setattr(bs.feedparser, "parse", fake_parse)
    monkeypatch.setattr(bs.time, "sleep", lambda s: pytest.fail("must not sleep on the happy path"))

    posts = bs.fetch_social_sentiment()
    assert len(hits) == 1, "one satisfying subreddit should end the sweep"
    assert len(posts) == bs._SOCIAL_PER_FEED


def test_rate_limited_feed_is_skipped_and_the_next_one_tried(monkeypatch):
    seen = []

    class Limited:
        status = 429
        entries = []

    class Fine:
        status = 200
        entries = [{"title": "real post"}]

    def fake_parse(url, agent=None):
        seen.append(url)
        return Limited() if len(seen) == 1 else Fine()

    monkeypatch.setattr(bs.feedparser, "parse", fake_parse)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)

    posts = bs.fetch_social_sentiment()
    assert len(seen) > 1, "a 429 must not end the sweep"
    assert any(p["title"] == "real post" for p in posts)


# ---------------------------------------------------------------------------
# TVmaze shaping
# ---------------------------------------------------------------------------

def test_pretty_date_formats_and_passes_through_junk():
    assert bs._pretty_date("2026-09-03") == "Thu 9/3"   # 2026-09-03 is a Thursday
    assert bs._pretty_date("2026-12-25") == "Fri 12/25"
    assert bs._pretty_date(None) is None
    assert bs._pretty_date("not-a-date") == "not-a-date"


def test_watchlist_entries_carry_a_friendly_date(monkeypatch):
    def fake_get(path, params):
        return {
            "name": "The Real Housewives of Orange County",
            "status": "Running",
            "network": {"name": "Bravo"},
            "_embedded": {"nextepisode": {"airdate": "2026-09-03", "name": "Ring of Truth",
                                          "season": 20, "number": 9}},
        }

    monkeypatch.setattr(bs, "SCHEDULE_WATCHLIST", ["RHOC"])
    monkeypatch.setattr(bs, "_tvmaze_get", fake_get)
    entry = bs.fetch_watchlist_schedule()[0]
    assert entry["when"] == "Thu 9/3"
    assert entry["network"] == "Bravo"
    assert entry["next_episode"] == "Ring of Truth"


def test_between_seasons_show_is_kept_with_no_date(monkeypatch):
    monkeypatch.setattr(bs, "SCHEDULE_WATCHLIST", ["Summer House"])
    monkeypatch.setattr(bs, "_tvmaze_get", lambda p, q: {
        "name": "Summer House", "status": "Running", "network": {"name": "Bravo"}, "_embedded": {},
    })
    entry = bs.fetch_watchlist_schedule()[0]
    assert entry["next_airdate"] is None and entry["when"] is None


def test_tvmaze_outage_does_not_raise(monkeypatch):
    def boom(path, params):
        raise OSError("tvmaze down")

    monkeypatch.setattr(bs, "SCHEDULE_WATCHLIST", ["RHOC"])
    monkeypatch.setattr(bs, "_tvmaze_get", boom)
    assert bs.fetch_watchlist_schedule() == []
    assert bs.fetch_upcoming_reality(days=1) == []


def test_upcoming_keeps_only_reality_and_flags_premieres(monkeypatch):
    payload = [
        {"show": {"name": "Some Drama", "type": "Scripted", "network": {"name": "ABC"}},
         "name": "Ep", "season": 1, "number": 1},
        {"show": {"name": "New Reality Thing", "type": "Reality", "network": {"name": "Bravo"}},
         "name": "Pilot", "season": 1, "number": 1},
        {"show": {"name": "Ongoing Reality", "type": "Reality", "network": {"name": "TLC"}},
         "name": "Ep 7", "season": 3, "number": 7},
    ]
    monkeypatch.setattr(bs, "_tvmaze_get", lambda p, q: payload)
    items = bs.fetch_upcoming_reality(days=1)
    names = {i["show"] for i in items}
    assert names == {"New Reality Thing", "Ongoing Reality"}, "scripted must be filtered out"
    assert next(i for i in items if i["show"] == "New Reality Thing")["is_premiere"] is True
    assert next(i for i in items if i["show"] == "Ongoing Reality")["is_premiere"] is False
    assert all(i["when"] for i in items)


# ---------------------------------------------------------------------------
# recommendations are grounded, not invented
# ---------------------------------------------------------------------------

def _upcoming(name, network="TLC", premiere=False, day_offset=0):
    d = (date.today() + timedelta(days=day_offset)).isoformat()
    return {"show": name, "network": network, "date": d, "when": bs._pretty_date(d),
            "episode": "Ep", "season": 1, "number": 1 if premiere else 5,
            "is_premiere": premiere}


def test_recommendations_exclude_shows_she_already_watches():
    upcoming = [_upcoming("Below Deck", "Bravo"), _upcoming("Some Other Show")]
    pool = bs.build_similar_pool(upcoming, ["Below Deck", "Summer House"])
    assert [p["show"] for p in pool] == ["Some Other Show"]


def test_recommendations_lead_with_premieres():
    upcoming = [
        _upcoming("Mid-Season Show", day_offset=0),
        _upcoming("Brand New Show", premiere=True, day_offset=3),
    ]
    pool = bs.build_similar_pool(upcoming, [])
    assert pool[0]["show"] == "Brand New Show"


def test_recommendation_pool_is_deduped_and_capped():
    upcoming = [_upcoming(f"Show {i}", day_offset=i % 7) for i in range(40)]
    upcoming += [_upcoming("Show 0", day_offset=2)]
    pool = bs.build_similar_pool(upcoming, [])
    assert len(pool) <= bs.MAX_SIMILAR_CANDIDATES
    assert len({p["show"] for p in pool}) == len(pool)


def test_payload_upcoming_prioritises_premieres_then_bravo_family():
    upcoming = [
        _upcoming("Food Show", "Food Network", day_offset=0),
        _upcoming("Bravo Show", "Bravo", day_offset=4),
        _upcoming("Premiere Elsewhere", "History", premiere=True, day_offset=6),
    ]
    ranked = [i["show"] for i in bs._payload_upcoming(upcoming)]
    assert ranked[0] == "Premiere Elsewhere"
    assert ranked.index("Bravo Show") < ranked.index("Food Show")


# ---------------------------------------------------------------------------
# prompt contract
# ---------------------------------------------------------------------------

def test_prompt_separates_dated_from_undated_and_forbids_invention():
    watchlist = [
        {"show": "RHOC", "network": "Bravo", "next_airdate": "2026-09-03", "when": "Thu 9/3",
         "next_episode": "Ring of Truth", "season": 20, "number": 9, "status": "Running"},
        {"show": "Summer House", "network": "Bravo", "next_airdate": None, "when": None,
         "next_episode": None, "season": None, "number": None, "status": "Running"},
    ]
    prompt = bs._build_prompt([], [], watchlist, [], [])
    assert "NO DATE ANNOUNCED (mention at most two)" in prompt
    assert "Never invent a title" in prompt
    assert "Never print a raw YYYY-MM-DD date" in prompt
    # The undated show is offered as a bare name, not as a record with null fields
    # the model might read as a real airdate.
    assert '"Summer House"' in prompt.split("NO DATE ANNOUNCED")[1].split("DATA —")[0]


def test_prompt_labels_the_pool_as_shows_she_does_not_watch():
    pool = [{"show": "New Thing", "network": "TLC", "date": "2026-09-04",
             "when": "Fri 9/4", "is_premiere": True}]
    prompt = bs._build_prompt([], [], [], [], pool)
    assert "she does NOT watch these" in prompt
    assert "New Thing" in prompt


# ---------------------------------------------------------------------------
# assembly: sections, fallback, delivery
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_sources(monkeypatch):
    monkeypatch.setattr(bs, "fetch_entertainment_feeds",
                        lambda: [{"source": "Us Weekly", "headline": "Star quits in feud", "drama": 8}])
    monkeypatch.setattr(bs, "fetch_social_sentiment",
                        lambda: [{"source": "r/x", "title": "buzz", "drama": 1}])
    monkeypatch.setattr(bs, "fetch_watchlist_schedule", lambda: [
        {"show": "RHOC", "network": "Bravo", "next_airdate": "2026-09-03", "when": "Thu 9/3",
         "next_episode": "Ring of Truth", "season": 20, "number": 9, "status": "Running"},
    ])
    monkeypatch.setattr(bs, "fetch_upcoming_reality",
                        lambda *a, **k: [_upcoming("New Thing", "TLC", premiere=True)])


def test_llm_failure_falls_back_to_a_real_sectioned_digest(stub_sources, monkeypatch):
    monkeypatch.setattr(bs, "query_llm", lambda prompt: bs._BRAIN_ERROR)
    sent = []
    monkeypatch.setattr(bs, "send_imessage", lambda phone, text: sent.append(text) or True)

    result = bs.execute_brief(send_alert=True)

    assert result["status"] == "digest_fallback"
    assert result["llm_used"] is False
    body = "\n".join(sent)
    assert "Star quits in feud" in body
    assert "RHOC: Thu 9/3 on Bravo" in body, "fallback must use the friendly date"
    assert "New Thing" in body
    assert "2026-09-03" not in body


def test_quiet_morning_says_so_instead_of_padding(monkeypatch):
    for name in ("fetch_entertainment_feeds", "fetch_social_sentiment",
                 "fetch_watchlist_schedule"):
        monkeypatch.setattr(bs, name, lambda: [])
    monkeypatch.setattr(bs, "fetch_upcoming_reality", lambda *a, **k: [])
    monkeypatch.setattr(bs, "query_llm", lambda p: pytest.fail("no LLM call on a quiet morning"))
    sent = []
    monkeypatch.setattr(bs, "send_imessage", lambda phone, text: sent.append(text) or True)

    result = bs.execute_brief(send_alert=True)
    assert result["status"] == "quiet"
    assert "Quiet news morning" in sent[0]


def test_long_brief_is_split_into_bubbles_and_all_are_sent(stub_sources, monkeypatch):
    long_brief = "\n\n".join(f"Section {i}: " + ("tea " * 120) for i in range(4))
    monkeypatch.setattr(bs, "query_llm", lambda prompt: long_brief)
    sent = []
    monkeypatch.setattr(bs, "send_imessage", lambda phone, text: sent.append(text) or True)

    result = bs.execute_brief(send_alert=True)

    assert result["bubbles"] > 1
    assert len(sent) == result["bubbles"]
    assert all(len(b) <= bs.BUBBLE_MAX_CHARS for b in sent)
    assert result["sent"] is True


def test_dry_run_never_sends(stub_sources, monkeypatch):
    monkeypatch.setattr(bs, "query_llm", lambda prompt: "brief body")
    monkeypatch.setattr(bs, "send_imessage", lambda *a, **k: pytest.fail("dry run must not send"))
    result = bs.execute_brief(send_alert=False)
    assert result["sent"] is False
    assert result["status"] == "ok"


def test_failed_send_is_reported_not_swallowed(stub_sources, monkeypatch):
    monkeypatch.setattr(bs, "query_llm", lambda prompt: "brief body")
    monkeypatch.setattr(bs, "send_imessage", lambda *a, **k: False)
    result = bs.execute_brief(send_alert=True)
    assert result["status"] == "send_failed"
    assert result["sent"] is False


def test_run_signature_matches_the_other_agents():
    import inspect

    sig = inspect.signature(bs.run)
    for name in ("force", "send", "requester", "request_id"):
        assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY
