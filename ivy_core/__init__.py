"""Version-controlled replacement for the untracked ``.ivy/ivy_core.py``.

Every proactive agent should import from here — a fresh clone must contain
everything needed to import and execute the jobs.
"""

from ivy_core.env import MissingEnvironmentVariable, require_env
from ivy_core.llm import query_llm
from ivy_core.messaging import send_imessage, send_imessage_attachment

__all__ = [
    "MissingEnvironmentVariable",
    "require_env",
    "query_llm",
    "send_imessage",
    "send_imessage_attachment",
]
