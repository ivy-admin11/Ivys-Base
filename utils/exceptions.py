"""Custom exception hierarchy for the Ivy Local Admin API Gateway.

All application-level errors derive from :class:`IvyError`. Each exception
carries a machine-readable ``error_code``, an HTTP ``status_code`` and a
*user-safe* message. Internal / sensitive details should be logged separately
and never placed in the message that reaches the client.
"""

from __future__ import annotations


class IvyError(Exception):
    """Base class for all Ivy application errors.

    Attributes:
        message: User-safe message (no stack traces / secrets / internals).
        error_code: Stable machine-readable identifier for the error class.
        status_code: HTTP status to return when surfaced through the API.
    """

    error_code: str = "ivy_error"
    status_code: int = 500

    def __init__(self, message: str = "An internal error occurred.") -> None:
        self.message = message
        super().__init__(message)


class AuthenticationError(IvyError):
    """Raised when a request fails authentication/authorization."""

    error_code = "authentication_error"
    status_code = 401


class GroceryServiceError(IvyError):
    """Raised when the grocery staging pipeline fails."""

    error_code = "grocery_service_error"
    status_code = 502


class ExternalServiceError(IvyError):
    """Raised when an upstream dependency (LLM, Readwise, etc.) fails."""

    error_code = "external_service_error"
    status_code = 502
