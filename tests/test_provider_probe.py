"""The health probe was the reason the failover brain was never available.

Gemini's free tier allows 20 generate_content calls a day for gemini-2.5-flash.
_probe_gemini called generate_content on every probe, behind a 60-second cache,
with a monitor polling /health continuously — up to 1440 generate calls a day
against a ceiling of 20. So the failover was exhausted by breakfast, every day,
and /health duly reported it dead. The probe was not observing the outage. It
was the outage.

The fix is that a health probe asks "is this key valid", and listing models
answers exactly that for free. These tests exist to stop anyone reaching for
generate_content in here again.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ALLOW_INSECURE_ADMIN_SECRET", "true")

import main  # noqa: E402


@pytest.fixture
def gemini(monkeypatch):
    """Stands in for the google-genai client and records what the probe does.

    generate_content is present and counted precisely so a regression that
    reaches for it is a test failure rather than a silent daily outage.
    """

    class Recorder:
        def __init__(self):
            self.listed = 0
            self.generated = 0
            self.list_raises = None

        # --- client.models.* ---
        def list(self):
            self.listed += 1
            if self.list_raises:
                raise self.list_raises
            return iter([object()])

        def generate_content(self, *a, **k):
            self.generated += 1
            return object()

        # the client exposes these under .models
        @property
        def models(self):
            return self

    rec = Recorder()
    monkeypatch.setattr(main, "gemini_client", rec)
    monkeypatch.setattr(main, "_build_gemini_client", lambda: rec)
    monkeypatch.setenv("GEMINI_API_KEY", "k" * 39)
    return rec


class TestTheProbeDoesNotSpendTheBudgetItIsChecking:
    def test_a_healthy_probe_never_calls_generate_content(self, gemini):
        """20 a day is plenty for a failover and nothing for a health check."""
        out = main._probe_gemini()
        assert gemini.generated == 0, "the probe must not consume generation quota"
        assert gemini.listed == 1
        assert out["authenticated"] is True
        assert out["status"] == "ready"

    def test_an_invalid_key_is_still_caught(self, gemini):
        """Cheap must not mean blind — a bad key has to fail the probe."""
        gemini.list_raises = Exception("400 API_KEY_INVALID: API key not valid")
        out = main._probe_gemini()
        assert out["authenticated"] is False
        assert out["status"] == "degraded"

    def test_a_throttled_probe_is_not_called_an_auth_failure(self, gemini):
        """429 means the key authenticated and then hit a limit. Reporting that
        as 'credentials rejected' is what sent Henry after a key rotation."""
        gemini.list_raises = Exception("429 RESOURCE_EXHAUSTED: quota")
        out = main._probe_gemini()
        assert out["authenticated"] is True
        assert out["status"] == "rate_limited"

    def test_an_unset_key_reports_unconfigured_not_broken(self, monkeypatch, gemini):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        out = main._probe_gemini()
        assert out["status"] == "unconfigured"
        assert gemini.listed == 0

    def test_an_unexpected_error_does_not_masquerade_as_healthy(self, gemini):
        gemini.list_raises = Exception("connection reset by peer")
        out = main._probe_gemini()
        assert out["authenticated"] is False
        assert out["status"] == "error"

    def test_it_still_declares_itself_the_failover(self, gemini):
        """The dual-brain contract is read off this field by /ready."""
        assert main._probe_gemini()["role"] == "failover"
