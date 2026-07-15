"""Abstract base class for all Ivy LangChain tools."""

from __future__ import annotations

from langchain_core.tools import BaseTool
from pydantic import ConfigDict


class BaseIvyTool(BaseTool):
    """Base class for Ivy tools.

    Extends LangChain's :class:`BaseTool` with an Ivy-specific
    ``requires_confirmation`` flag. Tools that mutate the outside world in a way
    the user may want to approve first (e.g. staging a grocery cart) set this to
    ``True`` so the agent layer can gate them behind a human confirmation.

    Concrete tools must define ``name``, ``description``, ``args_schema`` and
    implement ``_run``. Async is optional; all current tools are synchronous and
    inherit ``BaseTool``'s default ``_arun`` (which runs ``_run`` in a thread).
    """

    # arbitrary_types_allowed lets subclasses hold non-pydantic collaborators
    # such as the AppleScriptRunner.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    requires_confirmation: bool = False
