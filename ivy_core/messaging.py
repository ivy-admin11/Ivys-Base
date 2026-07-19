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

from utils.applescript import AppleScriptRunner
from ivy_core.report_fallback import AttachmentDeliveryReceipt

logger = logging.getLogger("ivy.messaging")

_runner = AppleScriptRunner()

# Messages.app is sandboxed and silently refuses (chat.db error 25, never sent)
# to attach AppleScript-supplied files from most of the home dir — including
# ~/openclaw-admin and ~/Downloads. It WILL read files under ~/Pictures, so we
# stage outbound attachments there before sending. Verified 2026-06-29.
_IMSG_ATTACH_STAGE = os.path.join(os.path.expanduser("~"), "Pictures", ".ivy_outbound")

# Maximum attachment attempts and inter-attempt delays (seconds).
_MAX_ATTEMPTS = 2
_RETRY_DELAYS = (3, 10)


def send_imessage(phone_number: str, message_text: str) -> bool:
    """Send a text-only iMessage. Returns True only on a confirmed SUCCESS receipt."""
    result = _runner.send_imessage_argv(phone_number, message_text)
    if result == "SUCCESS":
        return True
    logger.warning("send_imessage failed for %s: %s", phone_number, result)
    return False


def send_imessage_attachment(
    phone_number: str,
    file_path: str,
    caption: Optional[str] = None,
    *,
    report_id: Optional[str] = None,
) -> AttachmentDeliveryReceipt:
    """Send a file attachment (and optional caption) via iMessage.

    Returns an :class:`AttachmentDeliveryReceipt` describing the outcome.
    The receipt is truthy for ``submitted_unverified`` and
    ``verified_delivered``; falsy for ``failed``.

    Retry policy
    ------------
    - Up to ``_MAX_ATTEMPTS`` total attempts on explicit staging/AppleScript
      failures.
    - Delays: ~3 s before the second attempt; ~10 s before any future attempt.
    - A ``submitted_unverified`` result (AppleScript UI automation succeeded
      but delivery cannot be confirmed) is **not** retried — retrying would
      risk duplicate attachments.
    - A ``failed`` result on the final attempt triggers the caller's fallback.
    """
    file_path = os.path.abspath(file_path)
    report_id = report_id or str(uuid.uuid4())
    file_size = 0

    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        logger.warning("Attachment missing or empty: %s", file_path)
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
        logger.warning("Caption failed to send before attachment for %s", phone_number)

    staged = file_path
    try:
        os.makedirs(_IMSG_ATTACH_STAGE, exist_ok=True)
        staged = os.path.join(_IMSG_ATTACH_STAGE, os.path.basename(file_path))
        shutil.copyfile(file_path, staged)
        logger.info("Staged attachment for delivery: %s → %s", file_path, staged)
    except OSError as exc:
        logger.warning(
            "Could not stage attachment into ~/Pictures (%s); sending from source.", exc
        )
        staged = file_path

    last_result = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if attempt > 1:
            delay = _RETRY_DELAYS[attempt - 2] if attempt - 2 < len(_RETRY_DELAYS) else _RETRY_DELAYS[-1]
            logger.info("Attachment attempt %d/%d: waiting %ds before retry…", attempt, _MAX_ATTEMPTS, delay)
            time.sleep(delay)

        last_result = _runner.send_imessage_file_argv(phone_number, staged)

        if last_result == "SUCCESS":
            # AppleScript UI automation completed. We cannot independently
            # verify phone delivery from Python, so we mark as unverified.
            logger.info("send_imessage_attachment submitted (attempt %d): %s", attempt, phone_number)
            return AttachmentDeliveryReceipt.make_unverified(
                report_id=report_id,
                attachment_path=file_path,
                staged_path=staged,
                file_size_bytes=file_size,
                attempts=attempt,
                applescript_result="SUCCESS",
            )

        logger.warning(
            "send_imessage_attachment attempt %d/%d failed for %s: %s",
            attempt, _MAX_ATTEMPTS, phone_number, last_result,
        )

    # All attempts exhausted.
    return AttachmentDeliveryReceipt.make_failed(
        report_id=report_id,
        attachment_path=file_path,
        staged_path=staged,
        file_size_bytes=file_size,
        attempts=_MAX_ATTEMPTS,
        error_code="APPLESCRIPT_FAILED",
        error_detail=f"AppleScript returned: {last_result[:120] if last_result else 'no result'}",
        applescript_result=last_result[:120] if last_result else "",
    )
