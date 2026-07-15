"""Inbound message authorization and trigger detection.

Encapsulates the two gate checks the iMessage worker previously performed
inline: (1) is the sender allowed to command Ivy, and (2) does the message
actually address Ivy. Fails closed — an unreadable/missing allowlist blocks all
external senders (the local user, "me", is always authorized).
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Set

logger = logging.getLogger("ivy.services.message_processor")

TRIGGER_WORD = "ivy"


class MessageProcessor:
    def __init__(self, favorites_path: str = "favorites.json") -> None:
        self.favorites_path = favorites_path
        self._allowed: Set[str] = self._load_favorites()

    def _load_favorites(self) -> Set[str]:
        if not os.path.exists(self.favorites_path):
            logger.warning(
                "favorites.json missing at %s — all external senders will be "
                "blocked.",
                self.favorites_path,
            )
            return set()
        try:
            with open(self.favorites_path, "r") as fh:
                data = json.load(fh)
        except Exception as exc:
            logger.warning("Failed to parse favorites.json: %s", exc)
            return set()

        if isinstance(data, list):
            return {str(item).strip() for item in data if str(item).strip()}
        logger.warning("favorites.json is not a JSON array; ignoring contents.")
        return set()

    def reload(self) -> None:
        """Re-read the allowlist from disk (e.g. after it is edited)."""
        self._allowed = self._load_favorites()

    @property
    def allowed_contacts(self) -> List[str]:
        return sorted(self._allowed)

    def is_authorized(self, sender: str) -> bool:
        """True if ``sender`` is the local user or on the allowlist."""
        if sender is None:
            return False
        if sender.strip().lower() == "me":
            return True
        return sender.strip() in self._allowed

    def should_trigger_ivy(self, text: str) -> bool:
        """True if the message addresses Ivy (case-insensitive substring)."""
        if not text:
            return False
        return TRIGGER_WORD in text.lower()
