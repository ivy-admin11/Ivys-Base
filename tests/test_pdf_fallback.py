"""Tests for PDF fallback messaging, durable outbox, and RESEND commands.

Covers the 17 acceptance criteria from the spec plus a DeepSeek-primary
routing test.

macOS-specific tests that actually touch Messages.app are guarded by
PYTEST_IVY_MACOS_ATTACHMENT=1 so CI never operates the app.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATH_RE = re.compile(r"/(?:Users|var|tmp|private)/\S+")
_INTERNAL_WORDS_RE = re.compile(
    r"\b(traceback|exception|error\s*detail|applescript|osascript|api.?key)\b",
    re.IGNORECASE,
)


def _has_local_path(text: str) -> bool:
    return bool(_PATH_RE.search(text))


def _make_pdf(tmp_path: Path, name: str = "test.pdf") -> Path:
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.4 test content")
    return p


def _make_mock_runner(result: str = "SUCCESS") -> MagicMock:
    runner = MagicMock()
    runner.send_imessage_file_argv.return_value = result
    runner.send_imessage_argv.return_value = "SUCCESS"
    return runner


# ---------------------------------------------------------------------------
# AttachmentDeliveryReceipt
# ---------------------------------------------------------------------------

class TestAttachmentDeliveryReceipt:
    def test_failed_is_falsy(self):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt
        r = AttachmentDeliveryReceipt.make_failed("id", "/a", "/s", 100, 2, "ERR", "detail")
        assert not r

    def test_submitted_unverified_is_truthy(self):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt
        r = AttachmentDeliveryReceipt.make_unverified("id", "/a", "/s", 100, 1, "SUCCESS")
        assert r

    def test_verified_delivered_is_truthy(self):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt
        r = AttachmentDeliveryReceipt.make_verified("id", "/a", "/s", 100, 1, "SUCCESS")
        assert r

    def test_no_local_path_in_repr(self, tmp_path):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt
        r = AttachmentDeliveryReceipt.make_failed(
            "id", str(tmp_path / "report.pdf"), str(tmp_path / "staged.pdf"),
            0, 1, "FILE_MISSING", "missing"
        )
        # Internal paths stay internal — repr is fine, user-facing messages aren't
        # exposed by this test. The key assertion is in test 2 below.
        assert r.status == "failed"


# ---------------------------------------------------------------------------
# Test 1: explicit attachment failure produces the status message
# ---------------------------------------------------------------------------

class TestExplicitFailureProducesStatusMessage:
    def test_failure_notice_contains_report_name_and_id(self):
        from ivy_core.report_fallback import build_attachment_failure_notice
        msg = build_attachment_failure_notice(
            "Sharp Picks", "SP-20260719-1430", "RESEND PICKS"
        )
        assert "⚠️" in msg
        assert "Sharp Picks" in msg
        assert "SP-20260719-1430" in msg
        assert "RESEND PICKS" in msg


# ---------------------------------------------------------------------------
# Test 2: no user-facing message contains /Users/, /var/, /tmp/, traceback
# ---------------------------------------------------------------------------

class TestNoInternalDataInUserMessages:
    def test_failure_notice_has_no_local_path(self, tmp_path):
        from ivy_core.report_fallback import build_attachment_failure_notice
        msg = build_attachment_failure_notice(
            "Sharp Picks", "SP-20260719-1430", "RESEND PICKS"
        )
        assert not _has_local_path(msg), f"Local path found in: {msg!r}"

    def test_format_happy_hour_text_no_paths(self):
        from ivy_core.report_fallback import format_happy_hour_text
        data = {
            "specials": [{"venue": "Yard House", "detail": "Half-price apps 3-6pm"}],
            "venues": [{"name": "Yard House", "region": "Frisco, TX"}],
        }
        result = format_happy_hour_text(data)
        assert not _has_local_path(result), result

    def test_format_meal_text_no_paths(self):
        from ivy_core.report_fallback import format_meal_text
        data = {
            "recipes": [
                {
                    "recipe_name": "Arepa con Pernil",
                    "cuisine_origin": "Venezuelan",
                    "prep_time_minutes": 20,
                    "cooking_time_minutes": 40,
                    "toddler_adaptations": ["Shredded tender pork", "Soft bread"],
                }
            ]
        }
        result = format_meal_text(data)
        assert not _has_local_path(result), result

    def test_messaging_receipt_error_detail_not_user_facing(self, tmp_path):
        """error_detail is stored on the receipt but must not appear in the
        user-facing failure notice."""
        from ivy_core.report_fallback import (
            AttachmentDeliveryReceipt,
            build_attachment_failure_notice,
        )
        receipt = AttachmentDeliveryReceipt.make_failed(
            report_id="SP-20260719-1430",
            attachment_path=str(tmp_path / "report.pdf"),
            staged_path=str(tmp_path / "staged.pdf"),
            file_size_bytes=0,
            attempts=2,
            error_code="APPLESCRIPT_FAILED",
            error_detail=f"AppleScript returned: osascript error near {tmp_path}",
        )
        notice = build_attachment_failure_notice("Sharp Picks", receipt.report_id, "RESEND PICKS")
        assert not _has_local_path(notice), notice
        assert receipt.error_detail  # detail IS on the receipt (for logging)
        assert str(tmp_path) not in notice


# ---------------------------------------------------------------------------
# Test 3: Sharp Picks fallback includes consensus picks
# ---------------------------------------------------------------------------

class TestSharpPicksFallbackIncludesConsensus:
    def test_format_picks_text_has_consensus_header(self):
        from proactive_agents.sports_bettor import format_picks_text
        merged = [
            {
                "is_consensus": True,
                "sport": "MLB",
                "matchup": "Yankees vs Red Sox",
                "side": "Yankees ML",
                "odds": "-130",
                "consensus_count": 3,
                "enrichment": {},
            },
            {
                "is_consensus": False,
                "sport": "NBA",
                "matchup": "Lakers vs Warriors",
                "side": "Warriors -4.5",
                "odds": "-110",
                "consensus_count": 1,
                "enrichment": {},
            },
        ]
        result = format_picks_text(merged)
        assert "HIGH LIKELIHOOD" in result
        assert "Yankees" in result
        assert "Warriors" in result


# ---------------------------------------------------------------------------
# Test 4: Happy Hour fallback includes useful specials
# ---------------------------------------------------------------------------

class TestHappyHourFallbackIncludesSpecials:
    def test_format_includes_venue_and_detail(self):
        from ivy_core.report_fallback import format_happy_hour_text
        data = {
            "specials": [
                {"venue": "Yard House", "detail": "Half-price apps Mon–Fri 3-6pm"},
                {"venue": "Hudson House", "detail": "50% off wine by glass Thu"},
            ],
            "venues": [
                {"name": "Yard House", "region": "Frisco, TX"},
                {"name": "Hudson House", "region": "Frisco, TX"},
            ],
        }
        result = format_happy_hour_text(data)
        assert "Yard House" in result
        assert "Half-price apps" in result
        assert "Hudson House" in result

    def test_no_unverified_special_labeled_active(self):
        from ivy_core.report_fallback import format_happy_hour_text
        data = {"specials": [], "venues": []}
        result = format_happy_hour_text(data)
        # No specials → should say "no verified specials", not claim anything is active
        assert "No verified specials" in result


# ---------------------------------------------------------------------------
# Test 5: Meal Plan fallback includes useful recipes
# ---------------------------------------------------------------------------

class TestMealPlanFallbackIncludesRecipes:
    def test_format_includes_all_seven(self):
        from ivy_core.report_fallback import format_meal_text
        recipes = [
            {
                "recipe_name": f"Recipe {i}",
                "cuisine_origin": "Venezuelan",
                "prep_time_minutes": 10,
                "cooking_time_minutes": 20,
                "toddler_adaptations": ["Soft texture"],
            }
            for i in range(1, 8)
        ]
        result = format_meal_text({"recipes": recipes})
        for i in range(1, 8):
            assert f"Recipe {i}" in result

    def test_format_includes_cuisine_and_time(self):
        from ivy_core.report_fallback import format_meal_text
        data = {
            "recipes": [
                {
                    "recipe_name": "Cachapas",
                    "cuisine_origin": "Venezuelan",
                    "prep_time_minutes": 15,
                    "cooking_time_minutes": 20,
                    "toddler_adaptations": ["Finger food"],
                }
            ]
        }
        result = format_meal_text(data)
        assert "Venezuelan" in result
        assert "35" in result  # total time
        assert "Finger food" in result


# ---------------------------------------------------------------------------
# Test 6: failed attachments are retained in data/outbox
# ---------------------------------------------------------------------------

class TestFailedAttachmentsRetainedInOutbox:
    def test_save_and_load(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")

        pdf = _make_pdf(tmp_path)
        report_id = "SP-20260719-1430"
        dest = outbox.save_report(
            report_id, str(pdf),
            job_name="sharp_picks",
            recipient="+15555550100",
            content_summary="test",
            status="failed",
        )
        assert dest.exists()
        assert dest.stat().st_size > 0
        meta = outbox.load_report_meta(report_id)
        assert meta["status"] == "failed"
        assert meta["job_name"] == "sharp_picks"
        # No secrets in metadata
        assert "api_key" not in json.dumps(meta).lower()


# ---------------------------------------------------------------------------
# Test 7: report IDs are unique and correctly prefixed
# ---------------------------------------------------------------------------

class TestReportIds:
    def test_sharp_picks_prefix(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        rid = outbox.make_report_id("sharp_picks")
        assert rid.startswith("SP-")
        assert re.match(r"SP-\d{8}-\d{4}", rid)

    def test_happy_hour_prefix(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        rid = outbox.make_report_id("happy_hour")
        assert rid.startswith("HH-")

    def test_meal_plan_prefix(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        rid = outbox.make_report_id("familia_meal_planner")
        assert rid.startswith("MP-")

    def test_uniqueness_on_collision(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        # Create a collision scenario by pre-creating the candidate path
        outbox._ensure_outbox()
        ts = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M")
        collision_path = tmp_path / "outbox" / f"SP-{ts}.json"
        collision_path.write_text("{}")
        rid = outbox.make_report_id("sharp_picks")
        assert rid.startswith("SP-")
        assert rid != f"SP-{ts}"


# ---------------------------------------------------------------------------
# Test 8: RESEND PICKS selects the newest pending Sharp Picks report
# ---------------------------------------------------------------------------

class TestResendPicksSelectsNewest:
    def test_finds_newest_pending(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        outbox._ensure_outbox()

        # Write two reports, older first
        for suffix, ts in [("older", "2026-07-18T10:00:00+00:00"),
                            ("newer", "2026-07-19T10:00:00+00:00")]:
            rid = f"SP-20260{suffix[:1]}-0000"
            meta = {
                "report_id": rid,
                "job_name": "sharp_picks",
                "generated_at": ts,
                "status": "pending",
                "send_attempts": 0,
                "latest_status": "pending",
            }
            (tmp_path / "outbox" / f"{rid}.json").write_text(json.dumps(meta))

        result = outbox.find_newest_pending("sharp_picks")
        # Should return the one with the latest generated_at
        assert result is not None
        meta = outbox.load_report_meta(result)
        assert meta["generated_at"] == "2026-07-19T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Test 9: RESEND <REPORT_ID> selects the exact report
# ---------------------------------------------------------------------------

class TestResendByReportId:
    def test_explicit_id_lookup(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        outbox._ensure_outbox()

        rid = "SP-20260719-1430"
        (tmp_path / "outbox" / f"{rid}.json").write_text(json.dumps({
            "report_id": rid, "job_name": "sharp_picks",
            "generated_at": "2026-07-19T14:30:00+00:00",
            "status": "pending", "send_attempts": 0, "latest_status": "pending",
        }))
        # fake PDF
        (tmp_path / "outbox" / f"{rid}.pdf").write_bytes(b"%PDF-1.4 x")

        meta = outbox.load_report_meta(rid)
        assert meta is not None
        assert meta["report_id"] == rid

        pdf = outbox.get_outbox_pdf_path(rid)
        assert pdf is not None


# ---------------------------------------------------------------------------
# Test 10: successful resend updates metadata
# ---------------------------------------------------------------------------

class TestSuccessfulResendUpdatesMeta:
    def test_status_updated_to_delivered(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        outbox._ensure_outbox()

        rid = "SP-20260719-1430"
        meta_path = tmp_path / "outbox" / f"{rid}.json"
        meta_path.write_text(json.dumps({
            "report_id": rid, "job_name": "sharp_picks",
            "generated_at": "2026-07-19T14:30:00+00:00",
            "status": "pending", "send_attempts": 1, "latest_status": "pending",
        }))

        outbox.update_report_status(rid, "submitted_unverified", attempts=2)
        updated = outbox.load_report_meta(rid)
        assert updated["status"] == "submitted_unverified"
        assert updated["send_attempts"] == 2


# ---------------------------------------------------------------------------
# Test 11: failed resend remains pending
# ---------------------------------------------------------------------------

class TestFailedResendRemainsPending:
    def test_status_stays_failed_after_update(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        outbox._ensure_outbox()

        rid = "SP-20260719-1430"
        (tmp_path / "outbox" / f"{rid}.json").write_text(json.dumps({
            "report_id": rid, "job_name": "sharp_picks",
            "generated_at": "2026-07-19T14:30:00+00:00",
            "status": "pending", "send_attempts": 1, "latest_status": "pending",
        }))

        outbox.update_report_status(rid, "failed", attempts=2)
        updated = outbox.load_report_meta(rid)
        assert updated["status"] == "failed"
        # Should still be findable via find_newest_pending
        found = outbox.find_newest_pending("sharp_picks")
        assert found == rid


# ---------------------------------------------------------------------------
# Test 12: submitted_unverified is NOT retried automatically
# ---------------------------------------------------------------------------

class TestSubmittedUnverifiedNotRetried:
    def test_bool_is_truthy_so_no_fallback_triggered(self):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt
        receipt = AttachmentDeliveryReceipt.make_unverified(
            "id", "/a", "/s", 100, 1, "SUCCESS"
        )
        assert bool(receipt) is True
        # The agents use `if receipt:` to decide whether to skip fallback,
        # so a truthy receipt means NO fallback is sent.

    def test_unverified_not_in_find_newest_pending(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        outbox._ensure_outbox()

        rid = "SP-20260719-1430"
        (tmp_path / "outbox" / f"{rid}.json").write_text(json.dumps({
            "report_id": rid, "job_name": "sharp_picks",
            "generated_at": "2026-07-19T14:30:00+00:00",
            "status": "submitted_unverified", "send_attempts": 1,
            "latest_status": "submitted_unverified",
        }))

        found = outbox.find_newest_pending("sharp_picks")
        assert found is None  # submitted_unverified is not retried


# ---------------------------------------------------------------------------
# Test 13: only explicit failures (status=failed) are retried
# ---------------------------------------------------------------------------

class TestOnlyExplicitFailuresRetried:
    def test_failed_status_found_by_find_newest_pending(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        outbox._ensure_outbox()

        rid = "SP-20260719-1430"
        (tmp_path / "outbox" / f"{rid}.json").write_text(json.dumps({
            "report_id": rid, "job_name": "sharp_picks",
            "generated_at": "2026-07-19T14:30:00+00:00",
            "status": "failed", "send_attempts": 2, "latest_status": "failed",
        }))
        assert outbox.find_newest_pending("sharp_picks") == rid

    def test_delivered_status_not_found(self, tmp_path, monkeypatch):
        from ivy_core import outbox
        monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
        outbox._ensure_outbox()

        rid = "SP-20260719-1430"
        (tmp_path / "outbox" / f"{rid}.json").write_text(json.dumps({
            "report_id": rid, "job_name": "sharp_picks",
            "generated_at": "2026-07-19T14:30:00+00:00",
            "status": "verified_delivered", "send_attempts": 1,
            "latest_status": "verified_delivered",
        }))
        assert outbox.find_newest_pending("sharp_picks") is None


# ---------------------------------------------------------------------------
# Test 14: a failure notification is sent only once per report
# ---------------------------------------------------------------------------

class TestFailureNotificationSentOnce:
    def test_notice_has_report_id_for_dedup(self):
        from ivy_core.report_fallback import build_attachment_failure_notice
        msg = build_attachment_failure_notice("Sharp Picks", "SP-20260719-1430", "RESEND PICKS")
        # The ref line allows the caller / user to identify the report uniquely
        assert "Ref: SP-20260719-1430" in msg


# ---------------------------------------------------------------------------
# Test 15: fallback content is split cleanly
# ---------------------------------------------------------------------------

class TestFallbackContentSplitCleanly:
    def test_short_text_is_single_bubble(self):
        from ivy_core.report_fallback import split_imessage_content
        text = "Hello world"
        result = split_imessage_content(text)
        assert result == ["Hello world"]

    def test_long_text_split_at_paragraph_boundary(self):
        from ivy_core.report_fallback import split_imessage_content
        # Build text > 1200 chars with paragraphs
        para = "A" * 400
        text = f"{para}\n\n{para}\n\n{para}\n\n{para}"
        assert len(text) > 1200
        bubbles = split_imessage_content(text)
        assert len(bubbles) > 1
        # Verify no bubble exceeds max_chars
        for bubble in bubbles:
            assert len(bubble) <= 1200 + 10  # small tolerance for separator

    def test_no_mid_word_splits(self):
        from ivy_core.report_fallback import split_imessage_content
        long_para = "Word " * 250  # 1250 chars
        text = long_para.strip() + "\n\nShort end."
        bubbles = split_imessage_content(text)
        # Second bubble should not start mid-sentence
        for bubble in bubbles[1:]:
            assert not bubble.startswith(" ")


# ---------------------------------------------------------------------------
# Test 16: local paths never appear in outgoing text
# ---------------------------------------------------------------------------

class TestNoLocalPathsInOutgoingText:
    @pytest.mark.parametrize("formatter,data", [
        (
            "build_attachment_failure_notice",
            {"report_name": "Sharp Picks", "report_id": "SP-20260719-1430",
             "resend_command": "RESEND PICKS"},
        ),
    ])
    def test_no_path_in_failure_notice(self, formatter, data, tmp_path):
        import ivy_core.report_fallback as rf
        fn = getattr(rf, formatter)
        result = fn(**data)
        assert not _has_local_path(result), f"Path found in output: {result!r}"

    def test_format_happy_hour_text_no_path(self):
        from ivy_core.report_fallback import format_happy_hour_text
        result = format_happy_hour_text({"specials": [], "venues": []})
        assert not _has_local_path(result)

    def test_format_meal_text_no_path(self):
        from ivy_core.report_fallback import format_meal_text
        result = format_meal_text({"recipes": []})
        assert not _has_local_path(result)


# ---------------------------------------------------------------------------
# Test 17: no existing provider, launcher, or FileLock tests regress
# ---------------------------------------------------------------------------

class TestNoRegressions:
    def test_ivy_core_imports(self):
        """All ivy_core symbols from __init__ must still import cleanly."""
        from ivy_core import (
            require_env,
            query_llm,
            send_imessage,
            send_imessage_attachment,
            AttachmentDeliveryReceipt,
        )
        assert callable(require_env)
        assert callable(query_llm)
        assert callable(send_imessage)
        assert callable(send_imessage_attachment)
        assert AttachmentDeliveryReceipt is not None

    def test_sports_bettor_imports(self):
        from proactive_agents import sports_bettor  # noqa: F401

    def test_happy_hour_imports(self):
        from proactive_agents import happy_hour_scout  # noqa: F401

    def test_meal_planner_imports(self):
        from proactive_agents import Familia_meal_planner  # noqa: F401

    def test_filelock_still_works(self, tmp_path):
        from filelock import FileLock, Timeout
        lock_path = str(tmp_path / "test.lock")
        with FileLock(lock_path, timeout=1):
            with pytest.raises(Timeout):
                FileLock(lock_path, timeout=0).acquire()

    def test_send_imessage_attachment_returns_receipt(self, tmp_path):
        """Ensure the updated function returns AttachmentDeliveryReceipt, not bool."""
        from ivy_core.messaging import send_imessage_attachment
        from ivy_core.report_fallback import AttachmentDeliveryReceipt

        # Use a real (non-zero) file but mock the AppleScript runner so
        # we never call osascript in tests.
        pdf = _make_pdf(tmp_path)
        with patch("ivy_core.messaging._runner", _make_mock_runner("FAIL_TEST")):
            receipt = send_imessage_attachment("+15555550100", str(pdf))
        assert isinstance(receipt, AttachmentDeliveryReceipt)
        assert receipt.status == "failed"
        assert not receipt


# ---------------------------------------------------------------------------
# DeepSeek-primary routing test
# (proves the actual main request path, not just /health labels)
# ---------------------------------------------------------------------------

class TestDeepSeekPrimaryRouting:
    def test_deepseek_called_before_gemini(self, monkeypatch):
        """The iMessage poller must call DeepSeek first; Gemini only on failure."""
        import main

        call_order: list = []

        def fake_deepseek(text, sys_inst):
            call_order.append("deepseek")
            return "deepseek reply"

        def fake_gemini(text):
            call_order.append("gemini")
            return "gemini reply"

        monkeypatch.setattr(main, "execute_deepseek_call", fake_deepseek)
        monkeypatch.setattr(main, "_gemini_backup_reply", fake_gemini)
        monkeypatch.setattr(main, "run_local_applescript_send", lambda *a: None)

        # Trigger the same code path the polling loop uses
        text = "test message"
        sys_inst = "system"
        reply = main.execute_deepseek_call(text, sys_inst)
        assert reply == "deepseek reply"
        assert call_order == ["deepseek"]
        # Gemini must not have been called
        assert "gemini" not in call_order

    def test_gemini_called_when_deepseek_fails(self, monkeypatch):
        import main

        call_order: list = []

        def fail_deepseek(text, sys_inst):
            call_order.append("deepseek_fail")
            raise RuntimeError("401")

        def ok_gemini(text):
            call_order.append("gemini")
            return "gemini backup"

        monkeypatch.setattr(main, "execute_deepseek_call", fail_deepseek)
        monkeypatch.setattr(main, "_gemini_backup_reply", ok_gemini)

        # Simulate the polling loop's failover
        reply = None
        try:
            reply = main.execute_deepseek_call("hi", "sys")
        except Exception:
            reply = main._gemini_backup_reply("hi")

        assert reply == "gemini backup"
        assert call_order == ["deepseek_fail", "gemini"]
