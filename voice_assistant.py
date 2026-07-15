"""
Ivy Voice Assistant with Cache Optimization and Session Management

Features:
- Conversation session tracking with cache-aware context
- Audio input/output support (via speech-to-text/text-to-speech)
- Integration with Gemini + DeepSeek failover
- Prompt caching for repeated voice interactions
- Session persistence and cleanup
"""

import logging
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, List, Any
from enum import Enum

try:
    import google.generativeai as genai
except ImportError:
    genai = None

logger = logging.getLogger("ivy.voice")


class SessionState(str, Enum):
    """Voice session lifecycle states."""
    ACTIVE = "active"
    IDLE = "idle"
    CLOSED = "closed"
    ERROR = "error"


class VoiceSession:
    """Manages a single user's voice conversation session."""

    def __init__(self, user_id: str, session_ttl_seconds: int = 900):
        self.session_id = str(uuid.uuid4())
        self.user_id = user_id
        self.created_at = datetime.now()
        self.last_activity = datetime.now()
        self.ttl_seconds = session_ttl_seconds

        # Conversation context for cache optimization
        self.messages: List[Dict[str, str]] = []
        self.system_context = ""
        self.state = SessionState.ACTIVE

        # Cache statistics
        self.total_queries = 0
        self.cache_hits = 0
        self.start_time = time.time()

        logger.info(f"🎙️ Voice session {self.session_id[:8]}... created for user {user_id}")

    def is_expired(self) -> bool:
        """Check if session has exceeded TTL."""
        return (datetime.now() - self.last_activity).total_seconds() > self.ttl_seconds

    def add_message(self, role: str, content: str) -> None:
        """Add message to conversation history."""
        self.messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        self.last_activity = datetime.now()

    def get_context(self) -> str:
        """Get conversation context for prompt caching."""
        if not self.messages:
            return ""

        formatted = []
        for msg in self.messages[-5:]:  # Last 5 messages for context window
            formatted.append(f"{msg['role'].upper()}: {msg['content']}")
        return "\n".join(formatted)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize session for storage/monitoring."""
        uptime = time.time() - self.start_time
        cache_hit_rate = (self.cache_hits / self.total_queries * 100) if self.total_queries > 0 else 0

        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "uptime_seconds": uptime,
            "total_queries": self.total_queries,
            "cache_hits": self.cache_hits,
            "cache_hit_rate_percent": cache_hit_rate,
            "message_count": len(self.messages),
            "is_expired": self.is_expired()
        }


class VoiceSessionManager:
    """Manages multiple concurrent voice sessions with cleanup."""

    def __init__(self, session_ttl_seconds: int = 900, cleanup_interval_seconds: int = 300):
        self.sessions: Dict[str, VoiceSession] = {}
        self.session_ttl = session_ttl_seconds
        self.cleanup_interval = cleanup_interval_seconds
        self.last_cleanup = time.time()
        logger.info(f"🎙️ VoiceSessionManager initialized (TTL={session_ttl_seconds}s)")

    def create_session(self, user_id: str) -> VoiceSession:
        """Create a new voice session for a user."""
        session = VoiceSession(user_id, self.session_ttl)
        self.sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[VoiceSession]:
        """Retrieve an active session."""
        self._cleanup_expired_sessions()

        session = self.sessions.get(session_id)
        if session and session.state == SessionState.ACTIVE and not session.is_expired():
            return session
        return None

    def get_user_session(self, user_id: str) -> Optional[VoiceSession]:
        """Get active session for a user (creates one if none exists)."""
        self._cleanup_expired_sessions()

        # Find existing active session for user
        for session in self.sessions.values():
            if session.user_id == user_id and session.state == SessionState.ACTIVE and not session.is_expired():
                return session

        # Create new session if none exists
        return self.create_session(user_id)

    def close_session(self, session_id: str) -> bool:
        """Close a session."""
        session = self.sessions.get(session_id)
        if session:
            session.state = SessionState.CLOSED
            logger.info(f"✋ Voice session {session_id[:8]}... closed")
            return True
        return False

    def _cleanup_expired_sessions(self) -> None:
        """Remove expired sessions (background cleanup)."""
        now = time.time()
        if now - self.last_cleanup < self.cleanup_interval:
            return

        expired_ids = [
            sid for sid, sess in self.sessions.items()
            if sess.is_expired() or sess.state == SessionState.CLOSED
        ]

        for sid in expired_ids:
            logger.info(f"🗑️  Removing expired session {sid[:8]}...")
            del self.sessions[sid]

        self.last_cleanup = now
        if expired_ids:
            logger.info(f"🗑️  Cleaned up {len(expired_ids)} expired voice sessions")

    def get_stats(self) -> Dict[str, Any]:
        """Get session manager statistics."""
        self._cleanup_expired_sessions()

        total_sessions = len(self.sessions)
        active_sessions = sum(1 for s in self.sessions.values() if s.state == SessionState.ACTIVE)
        total_queries = sum(s.total_queries for s in self.sessions.values())
        total_cache_hits = sum(s.cache_hits for s in self.sessions.values())
        cache_hit_rate = (total_cache_hits / total_queries * 100) if total_queries > 0 else 0

        return {
            "total_sessions": total_sessions,
            "active_sessions": active_sessions,
            "session_ttl_seconds": self.session_ttl,
            "total_queries_all_sessions": total_queries,
            "total_cache_hits_all_sessions": total_cache_hits,
            "global_cache_hit_rate_percent": cache_hit_rate
        }

    def list_sessions(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all sessions (optionally filtered by user)."""
        self._cleanup_expired_sessions()

        sessions = [
            s.to_dict() for s in self.sessions.values()
            if user_id is None or s.user_id == user_id
        ]
        return sessions


class VoiceProcessor:
    """Processes voice queries with cache optimization."""

    def __init__(self, cache_manager=None):
        self.cache_manager = cache_manager
        logger.info("🎙️ VoiceProcessor initialized")

    def create_voice_prompt(
        self,
        user_query: str,
        session: VoiceSession,
        system_instruction: str,
        tool_declarations: List[Dict[str, Any]]
    ) -> List:
        """
        Create optimized prompt for voice query using cached system context.
        Returns messages list ready for Gemini with cache control.

        Does NOT record the user's turn into session history — the caller
        does that once, regardless of which provider ends up answering.
        """
        conversation_context = session.get_context()

        # Build system prompt with conversation history
        enriched_system = f"""{system_instruction}

## Conversation History (last 5 exchanges):
{conversation_context}

## Voice Assistant Directives:
- Keep responses concise and conversational (under 50 words)
- Optimize for speech synthesis clarity
- Confirm actions with brief summaries
- Use natural language, no markdown or special formatting
"""

        if self.cache_manager and hasattr(self.cache_manager, 'create_cached_gemini_request'):
            return self.cache_manager.create_cached_gemini_request(
                user_message=user_query,
                system_instruction=enriched_system,
                tool_declarations=tool_declarations
            )

        # Fallback: return simple message list
        return [genai.types.ContentDict(
            role="user",
            parts=[genai.types.PartDict(text=f"{enriched_system}\n\nUser: {user_query}")]
        )] if genai else None

    def log_voice_query(self, session: VoiceSession, response_text: str, cached_tokens: int = 0) -> None:
        """Log voice query statistics for monitoring."""
        session.total_queries += 1
        if cached_tokens > 0:
            session.cache_hits += 1

        cache_rate = (session.cache_hits / session.total_queries * 100) if session.total_queries > 0 else 0
        logger.info(
            f"🎤 Voice Query | Session: {session.session_id[:8]}... | "
            f"Queries: {session.total_queries} | "
            f"Cache Hits: {session.cache_hits} ({cache_rate:.1f}%) | "
            f"Response: {response_text[:50]}..."
        )


# Global voice session manager instance
voice_session_manager = VoiceSessionManager(session_ttl_seconds=900)
