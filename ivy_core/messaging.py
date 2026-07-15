"""iMessage sending for job agents, routed through safe argv-based AppleScript.

Replaces the raw f-string ``osascript -e`` calls that used to live in the
untracked ``.ivy/ivy_core.py`` — recipient/message/attachment-path content is
now passed as process argv (see :mod:`utils.applescript`), never interpolated
into AppleScript source text.
"""

import logging
import os
import shutil
from typing import Optional

from utils.applescript import AppleScriptRunner

logger = logging.getLogger("ivy.messaging")

_runner = AppleScriptRunner()

# Messages.app is sandboxed and silently refuses (chat.db error 25, never sent)
# to attach AppleScript-supplied files from most of the home dir — including
# ~/openclaw-admin and ~/Downloads. It WILL read files under ~/Pictures, so we
# stage outbound attachments there before sending. Verified 2026-06-29.
_IMSG_ATTACH_STAGE = os.path.join(os.path.expanduser("~"), "Pictures", ".ivy_outbound")


def send_imessage(phone_number: str, message_text: str) -> bool:
    """Send a text-only iMessage. Returns True only on a confirmed SUCCESS receipt."""
    result = _runner.send_imessage_argv(phone_number, message_text)
    if result == "SUCCESS":
        return True
    logger.warning("send_imessage failed for %s: %s", phone_number, result)
    return False


def send_imessage_attachment(
    phone_number: str, file_path: str, caption: Optional[str] = None
) -> bool:
    """Send a file attachment (and optional caption) via iMessage.

    Sends the caption first (if provided), then the attachment, so the text
    lands above the image/PDF in the thread. Returns True only if the
    attachment itself was confirmed sent — a failed caption doesn't block it.
    """
    file_path = os.path.abspath(file_path)
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        logger.warning("Attachment missing or empty, skipping send: %s", file_path)
        return False

    if caption and not send_imessage(phone_number, caption):
        logger.warning("Caption failed to send before attachment for %s", phone_number)

    try:
        os.makedirs(_IMSG_ATTACH_STAGE, exist_ok=True)
        staged = os.path.join(_IMSG_ATTACH_STAGE, os.path.basename(file_path))
        shutil.copyfile(file_path, staged)
    except OSError as exc:
        logger.warning(
            "Could not stage attachment into ~/Pictures (%s); sending from source.", exc
        )
        staged = file_path

    result = _runner.send_imessage_file_argv(phone_number, staged)
    if result == "SUCCESS":
        return True
    logger.warning("send_imessage_attachment failed for %s: %s", phone_number, result)
    return False
