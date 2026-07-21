"""
Pipeline status tracking and error handling for Sharp Picks.

Provides explicit pipeline states and error types so runs are categorized
accurately (success vs degraded vs failure), and authentication/upstream
errors are reported to the administrator rather than silently proceeding.
"""

from enum import Enum
from typing import Optional


class PipelineStatus(str, Enum):
    """Final status of a Sharp Picks run."""
    
    # Successful runs
    SUCCESS = "success"  # All sources healthy, 1+ pick passes minimum threshold
    
    # Degraded runs (partial success)
    DEGRADED = "degraded"  # Non-critical sources unavailable, but minimum picks still delivered
    
    # Failure states
    AUTH_FAILURE = "auth_failure"  # 401/403 on required API (e.g., Odds API)
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"  # All sources down/unavailable
    NO_QUALIFYING_PICKS = "no_qualifying_picks"  # Sources healthy but no picks meet threshold
    INTERNAL_ERROR = "internal_error"  # Code error or unexpected exception
    
    def is_success(self) -> bool:
        """Return True if the run should be considered successful."""
        return self in (PipelineStatus.SUCCESS, PipelineStatus.DEGRADED)
    
    def is_failure(self) -> bool:
        """Return True if the run should be considered a failure."""
        return self in (
            PipelineStatus.AUTH_FAILURE,
            PipelineStatus.UPSTREAM_UNAVAILABLE,
            PipelineStatus.NO_QUALIFYING_PICKS,
            PipelineStatus.INTERNAL_ERROR,
        )


class ProviderAuthenticationError(Exception):
    """Raised when an API returns 401 or 403 (credentials rejected)."""
    
    def __init__(
        self,
        provider: str,
        status_code: int,
        message: str,
        endpoint: Optional[str] = None,
    ):
        self.provider = provider
        self.status_code = status_code
        self.message = message
        self.endpoint = endpoint
        super().__init__(message)
    
    def __str__(self) -> str:
        parts = [f"[{self.provider}] {self.message} (HTTP {self.status_code})"]
        if self.endpoint:
            parts.append(f"Endpoint: {self.endpoint}")
        return " | ".join(parts)


class RetryableProviderError(Exception):
    """Raised when an API returns 429 (rate limited) or 5xx (server error)."""
    
    def __init__(
        self,
        provider: str,
        status_code: int,
        message: str,
        retry_after: Optional[int] = None,
    ):
        self.provider = provider
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after  # Seconds to wait before retry
        super().__init__(message)


class ProviderUnavailableError(Exception):
    """Raised when an API is unavailable (network error, timeout, etc.)."""
    
    def __init__(self, provider: str, message: str, cause: Optional[Exception] = None):
        self.provider = provider
        self.message = message
        self.cause = cause
        super().__init__(message)


class SourceHealth:
    """Track health of a single data source (e.g., "Odds API", "Grok X Search")."""
    
    def __init__(self, name: str, is_required: bool = False):
        """
        Args:
            name: Human-readable source name (e.g., "The Odds API")
            is_required: If True, failure of this source causes pipeline failure
        """
        self.name = name
        self.is_required = is_required
        self.healthy = False
        self.error: Optional[Exception] = None
        self.status_code: Optional[int] = None
        self.pick_count = 0
    
    def mark_success(self, pick_count: int = 0):
        """Mark source as healthy."""
        self.healthy = True
        self.error = None
        self.pick_count = pick_count
    
    def mark_failure(
        self,
        error: Exception,
        status_code: Optional[int] = None,
    ):
        """Mark source as failed."""
        self.healthy = False
        self.error = error
        self.status_code = status_code
    
    def __repr__(self) -> str:
        status = "✅" if self.healthy else "❌"
        req = " [REQUIRED]" if self.is_required else ""
        return f"{status} {self.name}{req}"


class PipelineResult:
    """Comprehensive result of a Sharp Picks run."""
    
    def __init__(self, status: PipelineStatus):
        self.status = status
        self.sources: dict[str, SourceHealth] = {}
        self.picks_count = 0
        self.consensus_count = 0
        self.message: Optional[str] = None
        self.admin_message: Optional[str] = None
        self.error: Optional[Exception] = None
        self.sent = False
        self.report_id: Optional[str] = None
    
    def add_source(self, name: str, is_required: bool = False) -> SourceHealth:
        """Register a data source."""
        source = SourceHealth(name, is_required)
        self.sources[name] = source
        return source
    
    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict for logging/API response."""
        # Map new status values to old result_type values for backward compatibility
        status_to_result_type = {
            PipelineStatus.SUCCESS: "success",
            PipelineStatus.DEGRADED: "degraded",
            PipelineStatus.AUTH_FAILURE: "auth_failure",
            PipelineStatus.UPSTREAM_UNAVAILABLE: "upstream_unavailable",
            PipelineStatus.NO_QUALIFYING_PICKS: "no_picks",
            PipelineStatus.INTERNAL_ERROR: "internal_error",
        }
        
        return {
            "status": self.status.value,
            "result_type": status_to_result_type.get(self.status, self.status.value),
            "picks": self.picks_count,
            "consensus": self.consensus_count,
            "sent": self.sent,
            "attached": False,  # Will be set to True when PDF is attached
            "report_id": self.report_id,
            "message": self.message,
            "admin_alert": self.admin_message,
            "sources": {
                name: {
                    "healthy": source.healthy,
                    "required": source.is_required,
                    "pick_count": source.pick_count,
                    "error": str(source.error) if source.error else None,
                }
                for name, source in self.sources.items()
            },
        }
