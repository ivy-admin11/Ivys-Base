"""Fail-closed environment variable access for standalone job scripts."""

import os


class MissingEnvironmentVariable(RuntimeError):
    """Raised by :func:`require_env` when a required variable is unset/empty."""


def require_env(var_name: str) -> str:
    """Return the value of ``var_name``, raising if it's unset or empty.

    Raises rather than calling ``sys.exit`` so this is safe to use from
    reusable business logic, not just top-level scripts.
    """
    value = os.environ.get(var_name)
    if not value:
        raise MissingEnvironmentVariable(
            f"Missing required environment variable: '{var_name}'"
        )
    return value
