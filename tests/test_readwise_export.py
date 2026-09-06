"""fetch_readwise_highlights, and the attribution that never worked.

The tool called /api/v2/highlights/ — the LIST endpoint — and then read
item["title"] off each row. That endpoint's rows carry a book_id and no title,
so the .get("title", "Saved Article") default fired every single time: every
highlight reached the model labelled "Saved Article". It never raised, and the
output looked plausible, which is why it lasted.

/api/v2/export/ groups highlights under their book with title, author and
category, in one call, and under the 240/min cap rather than the LIST
endpoint's 20/min.
"""
from __future__ import annotations

import pytest

import main


EXPORT_PAYLOAD = {
    "count": 2,
    "nextPageCursor": None,
    "results": [
        {
            "user_book_id": 1,
            "title": "Thinking, Fast and Slow",
            "author": "Daniel Kahneman",
            "category": "books",
            "highlights": [
                {"id": 10, "text": "Nothing in life is as important as you think it is.",
                 "note": "anchoring"},
                {"id": 11, "text": "A reliable way to make people believe in falsehoods.",
                 "note": ""},
            ],
        },
        {
            "user_book_id": 2,
            "title": "An Article",
            "author": "",
            "category": "articles",
            "highlights": [{"id": 12, "text": "Article highlight.", "note": ""}],
        },
    ],
}


@pytest.fixture
def readwise(monkeypatch):
    def _install(payload=EXPORT_PAYLOAD, status=200):
        captured = {}

        class R:
            status_code = status
            def json(self): return payload

        def fake_get(url, headers=None, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params or {}
            captured["headers"] = headers or {}
            return R()

        monkeypatch.setenv("READWISE_API_KEY", "tok")
        monkeypatch.setattr(main.requests, "get", fake_get)
        return captured
    return _install


class TestItUsesTheRightEndpoint:
    def test_it_calls_export_not_the_list_endpoint(self, readwise):
        captured = readwise()
        main.fetch_readwise_highlights()
        assert captured["url"].endswith("/api/v2/export/")

    def test_it_bounds_the_request_by_date(self, readwise):
        """Unbounded, /export/ returns the entire account."""
        captured = readwise()
        main.fetch_readwise_highlights()
        assert "updatedAfter" in captured["params"]

    def test_it_authenticates_with_a_token(self, readwise):
        captured = readwise()
        main.fetch_readwise_highlights()
        assert captured["headers"]["Authorization"].startswith("Token ")


class TestAttribution:
    def test_the_real_title_reaches_the_model(self, readwise):
        """The regression: this used to read 'Saved Article' every time."""
        readwise()
        out = main.fetch_readwise_highlights()
        assert "Thinking, Fast and Slow" in out
        assert "Saved Article" not in out

    def test_the_author_is_included(self, readwise):
        readwise()
        assert "Daniel Kahneman" in main.fetch_readwise_highlights()

    def test_a_missing_author_does_not_leave_a_dangling_dash(self, readwise):
        readwise()
        out = main.fetch_readwise_highlights()
        assert "'An Article'" in out
        assert "An Article — '" not in out

    def test_notes_survive(self, readwise):
        readwise()
        assert "anchoring" in main.fetch_readwise_highlights()

    def test_every_book_contributes(self, readwise):
        readwise()
        out = main.fetch_readwise_highlights()
        assert "Article highlight." in out


class TestEdges:
    def test_no_key_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setenv("READWISE_API_KEY", "")
        assert "READWISE_API_KEY missing" in main.fetch_readwise_highlights()

    def test_a_non_200_is_reported(self, readwise):
        readwise(status=500)
        assert "Status Code: 500" in main.fetch_readwise_highlights()

    def test_an_empty_account_says_so(self, readwise):
        readwise(payload={"results": []})
        assert "No Readwise highlights" in main.fetch_readwise_highlights()

    def test_a_book_with_no_highlights_is_skipped(self, readwise):
        readwise(payload={"results": [{"title": "Empty", "highlights": []}]})
        assert "No Readwise highlights" in main.fetch_readwise_highlights()

    def test_blank_highlight_text_is_skipped(self, readwise):
        readwise(payload={"results": [
            {"title": "T", "author": "A", "highlights": [{"text": "   "}, {"text": "real"}]}
        ]})
        out = main.fetch_readwise_highlights()
        assert out.count("- From") == 1

    def test_the_limit_is_respected(self, readwise):
        many = {"results": [{"title": "T", "author": "A",
                             "highlights": [{"text": f"h{i}"} for i in range(200)]}]}
        readwise(payload=many)
        out = main.fetch_readwise_highlights()
        assert out.count("- From") <= main.READWISE_HIGHLIGHTS_LIMIT

    def test_a_network_failure_is_returned_not_raised(self, monkeypatch):
        monkeypatch.setenv("READWISE_API_KEY", "tok")

        def boom(*a, **k):
            raise ConnectionError("no route")
        monkeypatch.setattr(main.requests, "get", boom)
        assert "Pipeline Error" in main.fetch_readwise_highlights()
