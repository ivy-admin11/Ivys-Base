"""iMessage sending for job agents, routed through safe argv-based AppleScript.

Replaces the raw f-string ``osascript -e`` calls that used to live in the
untracked ``.ivy/ivy_core.py`` — recipient/message/attachment-path content is
now passed as process argv (see :mod:`utils.applescript`), never interpolated
into AppleScript source text.
"""

import logging
import os
import shutil
import time
import uuid
from typing import Optional

from config import IMESSAGE_SEND_TIMEOUT_SECONDS
from utils.applescript import AppleScriptRunner
from ivy_core import attachment_verify
from ivy_core.report_fallback import AttachmentDeliveryReceipt

logger = logging.getLogger("ivy.messaging")

_runner = AppleScriptRunner(timeout=IMESSAGE_SEND_TIMEOUT_SECONDS)

# Messages.app is sandboxed and silently refuses (chat.db error 25, never sent)
# to attach AppleScript-supplied files from most of the home dir — including
# ~/openclaw-admin and ~/Downloads. It WILL read files under ~/Pictures, so we
# stage outbound attachments there before sending. Verified 2026-06-29.
_IMSG_ATTACH_STAGE = os.path.join(os.path.expanduser("~"), "Pictures", ".ivy_outbound")

# Delivery methods, tried in order until chat.db confirms one worked:
#   "scripting" — Messages' own `send <file> to participant … of account`
#                 verb (utils.applescript). Headless: no keystrokes, works
#                 with the screen locked or the display asleep. Verified in
#                 chat.db 2026-09-01 (transfer_state=5 within ~3 s).
#   "paste"     — clipboard + Cmd-V + Return into the Messages compose field.
#                 Fallback only. Delivered exactly once in the month before
#                 2026-09-01 (chat.db shows one outgoing PDF, FILE_5766.pdf,
#                 against a dozen "SUCCESS" logs): it needs an unlocked,
#                 focused session, and it returns SUCCESS even when the
#                 keystrokes went nowhere. Skipped while the screen is locked.
# Order is a module constant so it can be flipped from evidence, not guesswork.
_DELIVERY_METHODS = ("scripting", "paste")

# Seconds subtracted from the send timestamp when querying chat.db, to absorb
# clock granularity between Python and Messages.
_VERIFY_LOOKBACK_S = 2.0


def send_imessage(phone_number: str, message_text: str) -> bool:
    """Send a text-only iMessage. Returns True only on a confirmed SUCCESS receipt."""
    result = _runner.send_imessage_argv(phone_number, message_text)
    if result == "SUCCESS":
        return True
    logger.warning("send_imessage failed category=%s", _runner.last_error_category or "unknown")
    return False


def _stage_for_messages(file_path: str) -> str:
    """Copy the file under ~/Pictures (see _IMSG_ATTACH_STAGE). Returns the
    path Messages should be pointed at — the source path if staging fails.

    The staged basename gets a uuid prefix: two jobs can legitimately produce
    `report.pdf` at the same moment, and a shared staged name could make
    Messages attach one recipient's report to another conversation. The unique
    name doubles as the chat.db lookup key in attachment_verify, so
    verification can never match a different job's attachment either.
    """
    try:
        os.makedirs(_IMSG_ATTACH_STAGE, mode=0o700, exist_ok=True)
        os.chmod(_IMSG_ATTACH_STAGE, 0o700)
        staged = os.path.join(
            _IMSG_ATTACH_STAGE,
            f"{uuid.uuid4().hex}-{os.path.basename(file_path)}",
        )
        shutil.copyfile(file_path, staged)
        os.chmod(staged, 0o600)
        logger.info("Staged attachment for delivery")
        return staged
    except OSError as exc:
        logger.warning(
            "Could not stage attachment; sending from source error=%s",
            type(exc).__name__,
        )
        return file_path


def _run_method(method: str, phone_number: str, staged: str) -> str:
    if method == "paste":
        return _runner.send_imessage_file_argv(phone_number, staged)
    if method == "scripting":
        return _runner.send_imessage_file_scripting_argv(phone_number, staged)
    raise ValueError(f"unknown delivery method {method!r}")


def send_imessage_attachment(
    phone_number: str,
    file_path: str,
    caption: Optional[str] = None,
    *,
    report_id: Optional[str] = None,
    methods: Optional[tuple] = None,
) -> AttachmentDeliveryReceipt:
    """Send a file attachment (and optional caption) via iMessage, and confirm
    it in chat.db before claiming success.

    Returns an :class:`AttachmentDeliveryReceipt`:

    - ``verified_delivered``  — chat.db shows the upload finished, no error.
    - ``submitted_unverified`` — AppleScript accepted the send but chat.db is
      unreadable from this process AND the gateway could not be reached, or
      the upload was still in flight at the deadline. Not retried (a retry
      could duplicate the attachment).
    - ``failed`` — every method either errored, left no message row, or left
      one Messages marked as failed. The caller's text fallback runs.

    Each method is tried at most once; a later method only runs after
    chat.db proves the earlier one did not deliver.
    """
    file_path = os.path.abspath(file_path)
    report_id = report_id or str(uuid.uuid4())

    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        logger.warning("Attachment missing or empty")
        return AttachmentDeliveryReceipt.make_failed(
            report_id=report_id,
            attachment_path=file_path,
            staged_path="",
            file_size_bytes=0,
            attempts=0,
            error_code="FILE_MISSING_OR_EMPTY",
            error_detail=f"File not found or zero-size: {os.path.basename(file_path)}",
        )

    file_size = os.path.getsize(file_path)

    if caption and not send_imessage(phone_number, caption):
        logger.warning("Caption failed to send before attachment")

    staged = _stage_for_messages(file_path)
    filename = os.path.basename(staged)

    locked = attachment_verify.screen_is_locked()
    order = list(methods or _DELIVERY_METHODS)
    if locked and "paste" in order:
        logger.info("Screen is locked — skipping keystroke-based paste delivery")
        order.remove("paste")
    if not order:
        order = ["scripting"]

    attempts = 0
    last_result = ""
    last_error = "no delivery method ran"
    for method in order:
        attempts += 1
        since_ts = time.time() - _VERIFY_LOOKBACK_S
        last_result = _run_method(method, phone_number, staged)
        if last_result != "SUCCESS":
            last_error = f"{method}: {last_result[:120]}"
            logger.warning(
                "send_imessage_attachment failed method=%s attempt=%d category=%s",
                method, attempts, _runner.last_error_category or "unknown",
            )
            continue

        outcome, details = attachment_verify.wait_for_attachment_outcome(
            filename, since_ts, handle=phone_number,
        )
        logger.info(
            "send_imessage_attachment method=%s attempt=%d outcome=%s source=%s",
            method, attempts, outcome, details.get("source"),
        )
        if outcome == "delivered":
            return AttachmentDeliveryReceipt.make_verified(
                report_id=report_id,
                attachment_path=file_path,
                staged_path=staged,
                file_size_bytes=file_size,
                attempts=attempts,
                applescript_result="SUCCESS",
            )
        if outcome in ("unknown", "pending"):
            # Can't prove it failed — don't risk a duplicate by trying again.
            return AttachmentDeliveryReceipt.make_unverified(
                report_id=report_id,
                attachment_path=file_path,
                staged_path=staged,
                file_size_bytes=file_size,
                attempts=attempts,
                applescript_result="SUCCESS",
            )
        row = details.get("row") or {}
        last_error = (
            f"{method}: chat.db {outcome}"
            + (f" (error={row.get('error')}, transfer_state={row.get('transfer_state')})" if row else "")
        )
        logger.warning("send_imessage_attachment did not deliver method=%s detail=%s", method, last_error)

    return AttachmentDeliveryReceipt.make_failed(
        report_id=report_id,
        attachment_path=file_path,
        staged_path=staged,
        file_size_bytes=file_size,
        attempts=attempts,
        error_code="ATTACHMENT_NOT_DELIVERED",
        error_detail=last_error[:200],
        applescript_result=last_result[:120] if last_result else "",
    )
