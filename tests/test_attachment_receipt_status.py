"""Regression tests for the false-success PDF attachment bug.

``send_imessage_attachment()`` can return a receipt object even when
delivery failed. Callers must check the receipt's explicit ``status``
against the canonical success set instead of relying on truthiness — a
malformed/duck-typed receipt, a non-standard status string, an exception,
or a missing (``None``) receipt must all be treated as attachment failure
and trigger the full text fallback, never the "Full plan/report attached
(PDF)." message.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# AttachmentDeliveryReceipt.is_delivery_confirmed — the canonical check
# ---------------------------------------------------------------------------

class TestIsDeliveryConfirmed:
    def test_verified_delivered_is_confirmed(self):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt
        r = AttachmentDeliveryReceipt.make_verified("id", "/a", "/s", 100, 1, "SUCCESS")
        assert AttachmentDeliveryReceipt.is_delivery_confirmed(r)

    def test_submitted_unverified_is_confirmed(self):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt
        r = AttachmentDeliveryReceipt.make_unverified("id", "/a", "/s", 100, 1, "SUCCESS")
        assert AttachmentDeliveryReceipt.is_delivery_confirmed(r)

    def test_failed_is_not_confirmed(self):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt
        r = AttachmentDeliveryReceipt.make_failed("id", "/a", "/s", 100, 2, "ERR", "detail")
        assert not AttachmentDeliveryReceipt.is_delivery_confirmed(r)

    def test_failed_but_truthy_receipt_object_is_not_confirmed(self):
        """A malformed/duck-typed receipt whose __bool__ lies (returns True
        for a failed status) must still be rejected by the explicit check."""
        from ivy_core.report_fallback import AttachmentDeliveryReceipt

        class LyingReceipt:
            status = "failed"
            attempts = 2

            def __bool__(self):
                return True

        lying = LyingReceipt()
        assert bool(lying) is True  # sanity: it IS truthy
        assert not AttachmentDeliveryReceipt.is_delivery_confirmed(lying)

    @pytest.mark.parametrize(
        "status", ["queued", "timed_out", "rejected", "unavailable", "unknown", "", "FAILED"]
    )
    def test_non_success_statuses_are_not_confirmed(self, status):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt

        class FakeReceipt:
            def __init__(self, status):
                self.status = status
                self.attempts = 1

        assert not AttachmentDeliveryReceipt.is_delivery_confirmed(FakeReceipt(status))

    def test_none_receipt_is_not_confirmed(self):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt
        assert not AttachmentDeliveryReceipt.is_delivery_confirmed(None)

    def test_object_missing_status_attribute_is_not_confirmed(self):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt
        assert not AttachmentDeliveryReceipt.is_delivery_confirmed(object())


# ---------------------------------------------------------------------------
# Shared test scaffolding for the meal planner / happy hour agents
# ---------------------------------------------------------------------------

def _fake_meal_data():
    return {
        "status": "success",
        "recipe_count": 2,
        "recipes": [
            {
                "recipe_name": "Arepa con Pernil",
                "cuisine_origin": "Venezuelan",
                "prep_time_minutes": 20,
                "cooking_time_minutes": 40,
                "toddler_adaptations": ["Shredded tender pork", "Soft bread"],
            },
            {
                "recipe_name": "Fusion Burger",
                "cuisine_origin": "American",
                "prep_time_minutes": 10,
                "cooking_time_minutes": 15,
                "toddler_adaptations": ["No spice"],
            },
        ],
        "generated_at": "2026-07-19T00:00:00",
    }


def _run_meal_cycle(monkeypatch, receipt_factory, send_imessage_results=None):
    """Run execute_meal_plan_cycle with the attachment path stubbed out.

    ``receipt_factory`` is a zero-arg callable invoked for each recipient's
    send_imessage_attachment call; it may return a receipt, raise, or
    return None.
    """
    from proactive_agents import Familia_meal_planner as mp

    monkeypatch.setattr(mp, "check_48h_gate", lambda force=False: True)
    monkeypatch.setattr(mp, "generate_family_meal_plan", lambda: _fake_meal_data())
    monkeypatch.setattr(mp, "format_meal_plan_pdf", lambda meal_data: "/tmp/fake_meal_plan.pdf")
    monkeypatch.setattr(mp, "load_state", lambda: {
        "last_run_date": "2020-01-01T00:00:00+00:00", "recipe_count": 0, "execution_history": []
    })
    monkeypatch.setattr(mp, "save_state", lambda state: None)
    monkeypatch.setattr(mp, "ALERT_RECIPIENTS", {"testuser": "+15555550100"})

    monkeypatch.setattr(mp._outbox, "make_report_id", lambda job: "MP-TEST-0001")
    monkeypatch.setattr(mp._outbox, "save_report", lambda *a, **k: None)
    monkeypatch.setattr(mp._outbox, "update_report_status", lambda *a, **k: None)

    monkeypatch.setattr(mp, "send_imessage_attachment", lambda phone, path, report_id=None: receipt_factory())

    sent_texts = []

    if send_imessage_results is not None:
        results_iter = iter(send_imessage_results)

        def fake_send_imessage(phone, text):
            sent_texts.append(text)
            try:
                return next(results_iter)
            except StopIteration:
                return True
    else:
        def fake_send_imessage(phone, text):
            sent_texts.append(text)
            return True

    monkeypatch.setattr(mp, "send_imessage", fake_send_imessage)

    result = mp.execute_meal_plan_cycle(send_alert=True, force=True)
    return result, sent_texts


# ---------------------------------------------------------------------------
# Requirement 10 test matrix
# ---------------------------------------------------------------------------

class TestSuccessfulReceipt:
    def test_confirmed_delivery_sends_attached_summary_only(self, monkeypatch):
        from ivy_core.report_fallback import AttachmentDeliveryReceipt

        def make_receipt():
            return AttachmentDeliveryReceipt.make_unverified(
                "MP-TEST-0001", "/tmp/x.pdf", "/tmp/x.pdf", 100, 1, "SUCCESS"
            )

        result, sent_texts = _run_meal_cycle(monkeypatch, make_receipt)

        assert result["attachment_confirmed"]["testuser"] is True
        assert result["fallback_attempted"]["testuser"] is False
        assert result["notification_text_sent"]["testuser"] is True
        assert result["alert_sent"] is True
        assert any("Full plan attached (PDF)" in t for t in sent_texts)
        # No fallback bubbles were sent
        assert len(sent_texts) == 1


class TestFailedButTruthyReceipt:
    def test_lying_bool_does_not_fool_the_status_check(self, monkeypatch):
        class LyingReceipt:
            status = "failed"
            attempts = 2

            def __bool__(self):
                return True

        result, sent_texts = _run_meal_cycle(monkeypatch, lambda: LyingReceipt())

        assert result["attachment_confirmed"]["testuser"] is False
        assert result["fallback_attempted"]["testuser"] is True
        assert not any("attached (PDF)" in t for t in sent_texts)


class TestQueuedReceipt:
    def test_queued_status_triggers_fallback(self, monkeypatch):
        class QueuedReceipt:
            status = "queued"
            attempts = 1

        result, sent_texts = _run_meal_cycle(monkeypatch, lambda: QueuedReceipt())

        assert result["attachment_confirmed"]["testuser"] is False
        assert result["fallback_attempted"]["testuser"] is True
        assert result["fallback_fully_sent"]["testuser"] is True
        assert not any("attached (PDF)" in t for t in sent_texts)


class TestNoneReceipt:
    def test_none_receipt_triggers_fallback(self, monkeypatch):
        result, sent_texts = _run_meal_cycle(monkeypatch, lambda: None)

        assert result["attachment_confirmed"]["testuser"] is False
        assert result["fallback_attempted"]["testuser"] is True
        assert result["fallback_fully_sent"]["testuser"] is True
        assert not any("attached (PDF)" in t for t in sent_texts)


class TestExceptionDuringAttachmentSend:
    def test_exception_is_caught_and_treated_as_failure(self, monkeypatch):
        def raiser():
            raise RuntimeError("osascript exploded")

        result, sent_texts = _run_meal_cycle(monkeypatch, raiser)

        assert result["attachment_confirmed"]["testuser"] is False
        assert result["fallback_attempted"]["testuser"] is True
        assert result["fallback_fully_sent"]["testuser"] is True
        assert not any("attached (PDF)" in t for t in sent_texts)
        # Fallback notice + at least one fallback text bubble were sent
        assert len(sent_texts) >= 2


class TestMultipartTextFallback:
    def test_full_fallback_text_is_split_and_sent_in_order(self, monkeypatch):
        result, sent_texts = _run_meal_cycle(monkeypatch, lambda: None)

        # First bubble is the failure notice, remaining are the meal-plan
        # fallback text (built from format_meal_text/split_imessage_content).
        assert "couldn't be sent" in sent_texts[0]
        assert any("Arepa con Pernil" in t for t in sent_texts[1:])
        assert any("Fusion Burger" in t for t in sent_texts[1:])
        assert result["fallback_fully_sent"]["testuser"] is True


class TestPartialFallbackMessageFailure:
    def test_one_failed_bubble_marks_fallback_incomplete(self, monkeypatch):
        # notice succeeds, the single fallback bubble fails.
        result, sent_texts = _run_meal_cycle(
            monkeypatch, lambda: None, send_imessage_results=[True, False]
        )

        assert result["fallback_attempted"]["testuser"] is True
        assert result["fallback_fully_sent"]["testuser"] is False
        assert result["alert_sent"] is False
        assert not any("attached (PDF)" in t for t in sent_texts)


class TestNoFalseAttachedMessageOnFailure:
    def test_failure_never_claims_pdf_attached(self, monkeypatch):
        class FakeFailed:
            status = "failed"
            attempts = 2

        result, sent_texts = _run_meal_cycle(monkeypatch, lambda: FakeFailed())

        assert not any("Full plan attached (PDF)" in t for t in sent_texts)
        assert result["attachment_confirmed"]["testuser"] is False
