"""Text-first delivery and the interactive reply commands.

The bug these lock down: reports used to go out as a PDF attachment only, and
``submitted_unverified`` (AppleScript accepted it, chat.db never confirmed it)
counted as success — so the text fallback never ran and the report vanished.
Nothing here touches Messages.app, chat.db, or a real subprocess.
"""

import pytest

from ivy_core import outbox, text_delivery
from ivy_core.report_fallback import (
    build_detail,
    build_happy_hour_report,
    build_meal_report,
)


@pytest.fixture
def box(tmp_path, monkeypatch):
    """Point the outbox at a scratch directory."""
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path / "outbox")
    return tmp_path


@pytest.fixture
def sent():
    return []


def _sender(sent, ok=True):
    def send(phone, body):
        sent.append((phone, body))
        return ok
    return send


# ---------------------------------------------------------------------------
# deliver_report
# ---------------------------------------------------------------------------

def test_text_is_sent_and_pdf_is_archived_not_pushed(box, sent):
    pdf = box / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    result = text_delivery.deliver_report(
        "+15555550100",
        job_name="sharp_picks",
        body="Pick one\n\nPick two",
        detail=build_detail(title="T", items=[{"headline": "h", "detail": "d"}], shown=1),
        pdf_path=str(pdf),
        sender=_sender(sent),
    )

    assert result.delivered
    assert result.status == text_delivery.STATUS_DELIVERED
    assert sent, "nothing was texted"
    assert "Pick one" in "\n".join(b for _, b in sent)
    # The PDF exists in the outbox but was never handed to Messages.
    assert result.pdf_archived
    assert outbox.get_outbox_pdf_path(result.report_id) is not None
    assert outbox.load_report_meta(result.report_id)["status"] == "text_delivered"


def test_partial_send_is_not_reported_as_delivered(box, sent):
    calls = {"n": 0}

    def flaky(phone, body):
        calls["n"] += 1
        sent.append((phone, body))
        return calls["n"] == 1  # second bubble fails

    body = "\n\n".join(f"Item {i} " + "x" * 400 for i in range(6))
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body=body, sender=flaky,
    )

    assert result.bubbles_total > 1
    assert not result.delivered
    assert result.status == text_delivery.STATUS_PARTIAL
    assert bool(result) is False


def test_total_failure_is_status_failed(box, sent):
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="anything",
        sender=_sender(sent, ok=False),
    )
    assert result.status == text_delivery.STATUS_FAILED
    assert not result.delivered


def test_footer_carries_the_reply_commands(box, sent):
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="Short body",
        commands=("MORE", "WHY <n>", "PDF"), sender=_sender(sent),
    )
    last = sent[-1][1]
    assert "MORE" in last and "WHY <n>" in last and "PDF" in last
    assert result.report_id in last


def test_delivery_survives_a_missing_pdf(box, sent):
    """A broken PDF must cost the archive, never the message."""
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="The picks",
        pdf_path=None, sender=_sender(sent),
    )
    assert result.delivered
    assert result.pdf_archived is False
    assert outbox.get_outbox_pdf_path(result.report_id) is None


def test_detail_payload_round_trips(box, sent):
    detail = build_detail(
        title="Sharp Picks",
        items=[{"headline": f"{i}. pick", "detail": f"why {i}"} for i in range(1, 8)],
        shown=3,
    )
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="body",
        detail=detail, sender=_sender(sent),
    )
    loaded = outbox.load_detail(result.report_id)
    assert loaded["shown"] == 3
    assert len(loaded["items"]) == 7
    assert loaded["items"][6]["detail"] == "why 7"
    assert loaded["job_name"] == "sharp_picks"


def test_detail_files_do_not_pollute_report_lookups(box, sent):
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="body",
        detail=build_detail(title="t", items=[{"headline": "h", "detail": "d"}], shown=1),
        sender=_sender(sent),
    )
    assert outbox.find_newest("sharp_picks") == result.report_id
    assert outbox.find_newest("sharp_picks", with_detail=True) == result.report_id
    assert outbox.find_newest("happy_hour") is None


# ---------------------------------------------------------------------------
# Job digests
# ---------------------------------------------------------------------------

def test_happy_hour_digest_holds_back_the_tail():
    data = {
        "venues": [{"name": f"Bar {i}", "region": "Frisco, TX"} for i in range(6)],
        "specials": [
            {"venue": f"Bar {i}", "detail": f"deal {i}", "days_hours": "3-6"}
            for i in range(6)
        ],
    }
    body, detail = build_happy_hour_report(data)
    assert "reply MORE" in body
    assert len(detail["items"]) == 6
    assert detail["shown"] == 3


def test_meal_digest_keeps_every_recipe_reachable():
    data = {"recipes": [
        {"recipe_name": f"Dish {i}", "cuisine_origin": "Venezuelan",
         "prep_time_minutes": 10, "cooking_time_minutes": 20,
         "ingredients": ["arepa flour", "chicken"], "toddler_adaptations": ["shred it"]}
        for i in range(5)
    ]}
    body, detail = build_meal_report(data)
    assert "Dish 0" in body
    assert len(detail["items"]) == 5
    assert "arepa flour" in detail["items"][4]["detail"]


def test_empty_reports_still_produce_a_message():
    body, detail = build_happy_hour_report({"specials": [], "venues": []})
    assert body.strip()
    assert detail["items"] == []
    body, detail = build_meal_report({"recipes": []})
    assert body.strip()
    assert detail["items"] == []


# ---------------------------------------------------------------------------
# Interactive reply commands (MORE / WHY <n> / PDF)
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded(box, monkeypatch):
    """A delivered picks report with 7 picks, 3 of them shown."""
    import main

    monkeypatch.setattr(main._outbox, "OUTBOX_DIR", outbox.OUTBOX_DIR)
    detail = build_detail(
        title="Sharp Picks",
        items=[{"headline": f"{i}. Game {i}", "detail": f"Reasoning for {i}"}
               for i in range(1, 8)],
        shown=3,
        more_intro="The rest:",
    )
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="digest",
        detail=detail, sender=lambda p, b: True,
    )
    return main, result.report_id


def test_more_sends_only_the_held_back_items(seeded):
    main, _report_id = seeded
    reply = main.handle_report_command("MORE", "+15555550100")
    assert reply is not None
    text = "\n".join(reply)
    assert "4. Game 4" in text and "7. Game 7" in text
    assert "1. Game 1" not in text, "MORE must not repeat what was already sent"


def test_more_is_case_and_target_insensitive(seeded):
    main, _ = seeded
    for cmd in ("more", "More picks", "MORE SHARP PICKS"):
        assert main.handle_report_command(cmd, "+15555550100") is not None


def test_why_returns_that_items_reasoning(seeded):
    main, _ = seeded
    reply = main.handle_report_command("WHY 5", "+15555550100")
    assert "Reasoning for 5" in "\n".join(reply)


def test_why_out_of_range_is_a_helpful_answer_not_a_crash(seeded):
    main, _ = seeded
    reply = main.handle_report_command("WHY 99", "+15555550100")
    assert "no #99" in "\n".join(reply).lower()


def test_pdf_request_explains_when_the_report_was_text_only(seeded):
    main, report_id = seeded
    reply = main.handle_report_command("PDF", "+15555550100")
    text = "\n".join(reply)
    assert report_id in text
    assert "MORE" in text, "a text-only report should point back at MORE"


def test_pdf_reports_unverified_sends_honestly(box, monkeypatch):
    """The exact state that used to be logged as success."""
    import main

    monkeypatch.setattr(main._outbox, "OUTBOX_DIR", outbox.OUTBOX_DIR)
    pdf = box / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = text_delivery.deliver_report(
        "+15555550100", job_name="sharp_picks", body="digest",
        pdf_path=str(pdf), sender=lambda p, b: True,
    )

    from ivy_core.report_fallback import AttachmentDeliveryReceipt

    monkeypatch.setattr(
        main, "send_imessage_attachment",
        lambda phone, path, **k: AttachmentDeliveryReceipt.make_unverified(
            report_id=result.report_id, attachment_path=path, staged_path=path,
            file_size_bytes=12, attempts=1, applescript_result="SUCCESS",
        ),
    )

    reply = "\n".join(main.handle_report_command("PDF", "+15555550100"))
    assert "couldn't confirm" in reply
    assert "✅" not in reply, "an unconfirmed send must not be reported as delivered"


def test_non_commands_fall_through_to_the_llm(seeded):
    main, _ = seeded
    for text in ("why did you pick the Yankees?", "more info about tomorrow", "hey ivy"):
        assert main.handle_report_command(text, "+15555550100") is None


def test_commands_with_no_report_say_so(box, monkeypatch):
    import main

    monkeypatch.setattr(main._outbox, "OUTBOX_DIR", outbox.OUTBOX_DIR)
    reply = "\n".join(main.handle_report_command("MORE", "+15555550100"))
    assert "recently" in reply.lower() or "nothing" in reply.lower()


# ---------------------------------------------------------------------------
# Conversational report commands
#
# Sep 3, from the actual thread: "More" was answered by DeepSeek with "what
# would you like more of?", and "More of the 3pm picks you sent me" made it
# re-run the entire sweep and text a brand-new report. Both belong here.
# ---------------------------------------------------------------------------

@pytest.fixture
def two_reports(box, monkeypatch):
    """A 9am and a 3pm picks report, both with held-back items."""
    import main

    monkeypatch.setattr(main._outbox, "OUTBOX_DIR", outbox.OUTBOX_DIR)
    made = {}
    for rid, label, n in (("SP-20260903-0900", "9am", 4), ("SP-20260903-1500", "3pm", 10)):
        detail = build_detail(
            title=f"Sharp Picks {label}",
            items=[{"headline": f"{i}. {label} game {i}", "detail": f"{label} reasoning {i}"}
                   for i in range(1, n + 1)],
            shown=3,
        )
        text_delivery.deliver_report(
            "+15555550100", job_name="sharp_picks", body=f"{label} digest",
            report_id=rid, detail=detail, sender=lambda p, b: True,
        )
        made[label] = rid
    return main, made


def test_bare_more_is_handled_without_an_llm(two_reports):
    main, made = two_reports
    reply = main.handle_report_command("More", "+15555550100")
    assert reply is not None, "'More' must never reach the LLM"
    assert "3pm game 4" in "\n".join(reply), "a bare MORE means the most recent report"


def test_more_of_the_3pm_picks_you_sent_me(two_reports):
    """Verbatim from the thread — this re-ran the whole job."""
    main, made = two_reports
    reply = main.handle_report_command("More of the 3pm picks you sent me", "+15555550100")
    assert reply is not None, "this must not fall through to run_job"
    text = "\n".join(reply)
    assert "3pm game 4" in text
    assert "9am" not in text, "the clock reference must pick the 3pm report"


def test_clock_reference_selects_the_earlier_report(two_reports):
    main, made = two_reports
    reply = "\n".join(main.handle_report_command("more of the 9am picks", "+15555550100"))
    assert "9am game 4" in reply
    assert "3pm" not in reply


def test_clock_reference_with_no_matching_report_says_so(two_reports):
    main, made = two_reports
    reply = "\n".join(main.handle_report_command("more of the 6am picks", "+15555550100"))
    assert "6am" in reply
    assert "SP-20260903-1500" in reply, "it should offer the report it does have"


@pytest.mark.parametrize("phrasing", [
    "More",
    "more",
    "MORE PICKS",
    "more picks please",
    "the rest",
    "rest of them",
    "show me the rest",
    "More of the 3pm picks you sent me",
    "more of those",
])
def test_conversational_more_phrasings_are_all_handled(two_reports, phrasing):
    main, _ = two_reports
    assert main.handle_report_command(phrasing, "+15555550100") is not None, phrasing


@pytest.mark.parametrize("phrasing", [
    "why did you pick the Yankees?",
    "more info about tomorrow's weather please",
    "can you tell me more about how the sweep works",
    "hey ivy",
    "why is the sky blue",
    "run sports picks",
    "more chicken in the meal plan next week please thanks",
])
def test_conversation_still_reaches_the_llm(two_reports, phrasing):
    main, _ = two_reports
    assert main.handle_report_command(phrasing, "+15555550100") is None, phrasing


def test_why_accepts_conversational_forms(two_reports):
    main, _ = two_reports
    for phrasing in ("WHY 2", "why 2", "why #2", "why 2 picks", "why #2 of the 3pm picks"):
        reply = main.handle_report_command(phrasing, "+15555550100")
        assert reply is not None, phrasing
        assert "reasoning 2" in "\n".join(reply), phrasing


def test_pdf_accepts_conversational_forms(two_reports):
    main, _ = two_reports
    for phrasing in ("PDF", "pdf please", "send me the pdf", "RESEND PICKS"):
        assert main.handle_report_command(phrasing, "+15555550100") is not None, phrasing


# ---------------------------------------------------------------------------
# run_job re-run guard — the last line of defence for phrasings the matcher
# misses. On Sep 3 a question about the 3pm report triggered a whole new sweep.
# ---------------------------------------------------------------------------

def test_backward_looking_question_does_not_rerun_the_job(two_reports):
    main, _ = two_reports
    reply = main._execute_tool_call(
        "run_job", {"job_name": "sharp_picks"},
        inbound_text="can you show me the other picks from the report you sent me",
    )
    assert "won't re-run" in reply
    assert "SP-20260903-1500" in reply
    assert "MORE" in reply


def test_explicit_run_request_still_runs(two_reports, monkeypatch):
    main, _ = two_reports
    called = []
    monkeypatch.setitem(main.TOOL_HANDLERS, "run_job",
                        lambda job_name: called.append(job_name) or "started")

    for phrasing in ("run sports picks", "run picks again", "send me a new set of picks",
                     "refresh the picks you sent me"):
        called.clear()
        main._execute_tool_call("run_job", {"job_name": "sharp_picks"}, inbound_text=phrasing)
        assert called == ["sharp_picks"], phrasing


def test_guard_is_inert_without_the_inbound_text(two_reports, monkeypatch):
    """The endpoint path passes no text; it must never be blocked."""
    main, _ = two_reports
    called = []
    monkeypatch.setitem(main.TOOL_HANDLERS, "run_job",
                        lambda job_name: called.append(job_name) or "started")
    main._execute_tool_call("run_job", {"job_name": "sharp_picks"})
    assert called == ["sharp_picks"]


def test_guard_only_touches_run_job(two_reports, monkeypatch):
    main, _ = two_reports
    monkeypatch.setitem(main.TOOL_HANDLERS, "check_apple_calendar", lambda timeframe: "ok")
    out = main._execute_tool_call(
        "check_apple_calendar", {"timeframe": "today"},
        inbound_text="what was on the calendar you sent me earlier",
    )
    assert out == "ok"
