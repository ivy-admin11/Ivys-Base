"""
Prompt Caching Manager for Gemini API Token Optimization

Implements Google's Prompt Caching to save 80-90% on repeated input tokens.
Tracks cache hits and estimates monthly savings.

CLAUDE COST CRISIS FIX:
- Your Claude usage shows 57M cache writes on 7/12 (catastrophic!)
- You're NOT reading from cache (18K-50M tokens wasted daily)
- This implementation prevents that for Gemini
- For Claude, use prompt_caching=true in batches API
"""

import logging
import hashlib
import json
from datetime import datetime
from typing import Dict, Any, List, Tuple

try:
    import google.generativeai as genai
except ImportError:
    genai = None

logger = logging.getLogger("ivy.cache")

# Gemini Flash input pricing, expressed per TOKEN. The published rates are per
# MILLION tokens ($0.075 fresh input, $0.0075 for cached input), so every rate
# here is divided by 1_000_000 exactly once. Multiplying a token count by the
# per-million figure directly overstates cost by 1000x, which is how a few
# cents of spend gets reported as tens of dollars.
GEMINI_INPUT_COST_PER_MILLION = 0.075
GEMINI_CACHED_INPUT_COST_PER_MILLION = 0.0075
GEMINI_INPUT_COST_PER_TOKEN = GEMINI_INPUT_COST_PER_MILLION / 1_000_000
GEMINI_CACHE_SAVINGS_PER_TOKEN = (
    GEMINI_INPUT_COST_PER_MILLION - GEMINI_CACHED_INPUT_COST_PER_MILLION
) / 1_000_000

# Used only by the very rough "what would this have cost uncached" figure.
AVG_INPUT_TOKENS_PER_REQUEST = 500

_MISSING = object()


def _usage_field(usage: Any, *names: str) -> Any:
    """Return the first attribute of *usage* that actually exists.

    The Gemini SDK spells these prompt_token_count / cached_content_token_count /
    candidates_token_count. Other providers (and hand-rolled stubs) use
    input_tokens / output_tokens. Returning a sentinel rather than 0 for "absent"
    is what lets the caller tell an honest zero apart from a field that was never
    there -- guessing wrong here silently reports every request as a cache miss.
    """
    for name in names:
        value = getattr(usage, name, _MISSING)
        if value is not _MISSING:
            return value
    return _MISSING


def _usage_int(usage: Any, *names: str) -> int:
    value = _usage_field(usage, *names)
    return int(value) if value is not _MISSING else 0


class PromptCacheManager:
    """Manages cached prompt content for Gemini API calls with cost tracking."""

    def __init__(self, enable_caching: bool = True, ttl_seconds: int = 3600):
        self.enable_caching = enable_caching
        self.ttl_seconds = ttl_seconds
        
        # Cache statistics for cost analysis
        self.cache_stats = {
            "total_requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "tokens_cached": 0,
            "tokens_saved": 0,
            "requests_since_start": datetime.now(),
        }
        
        # In-memory cache of system prompt + tools (minimal memory footprint)
        self._cached_system_block = None
        self._cached_system_hash = None
        logger.info(f"💾 PromptCacheManager initialized (caching={'ON' if enable_caching else 'OFF'}, TTL={ttl_seconds}s)")

    def build_cached_system_prompt(
        self,
        system_instruction: str,
        tool_declarations: List[Dict[str, Any]]
    ) -> str:
        """Build the cached system prompt block (reusable across requests)."""
        if not self.enable_caching:
            return system_instruction

        # Create deterministic hash of system config (detects changes)
        config_str = json.dumps({
            "system": system_instruction,
            "tools": tool_declarations
        }, sort_keys=True)
        config_hash = hashlib.md5(config_str.encode(), usedforsecurity=False).hexdigest()

        # Return cached block if config hasn't changed
        if self._cached_system_hash == config_hash and self._cached_system_block:
            logger.debug(f"💾 System prompt already cached (hash: {config_hash[:8]}...)")
            return self._cached_system_block

        # Build new cached block
        tool_docs = self._format_tools_for_caching(tool_declarations)
        cached_block = f"""# SYSTEM INSTRUCTIONS (Cached for Token Optimization)
{system_instruction}

# AVAILABLE TOOLS (Cached for Token Optimization)
{tool_docs}

---
Cache enabled: This content is cached across requests to save ~80% input tokens.
"""
        
        self._cached_system_block = cached_block
        self._cached_system_hash = config_hash
        logger.info(f"🆕 New system prompt cached (hash: {config_hash[:8]}...)")
        
        return cached_block

    def _format_tools_for_caching(self, tools: List[Dict[str, Any]]) -> str:
        """Format tools in a compact way for caching."""
        formatted = []
        for tool in tools:
            name = tool.get("name", "unknown")
            desc = tool.get("description", "No description")
            params = tool.get("parameters", {})
            
            tool_str = f"## {name}\n{desc}\n"
            if params and "properties" in params:
                required = params.get("required", [])
                props = params["properties"]
                tool_str += "Parameters: "
                param_list = []
                for prop_name, prop_info in props.items():
                    req_marker = "(required)" if prop_name in required else "(optional)"
                    param_list.append(f"{prop_name} {req_marker}")
                tool_str += ", ".join(param_list) + "\n"
            
            formatted.append(tool_str)
        
        return "\n".join(formatted)

    def create_cached_gemini_request(
        self,
        user_message: str,
        system_instruction: str,
        tool_declarations: List[Dict[str, Any]]
    ) -> List:
        """
        Create Gemini content list optimized for caching.
        
        Returns: [cached_system_block, user_message]

        The system+tools block is kept byte-identical across requests and sent
        as the FIRST part, because a stable prefix is the only thing Gemini's
        caching keys off. There is deliberately no "cache_control" marker: that
        is Anthropic's API, and the Gemini SDK rejects any Part dict carrying
        keys it does not know.
        """
        if genai is None:
            logger.warning("google.generativeai not installed, caching disabled")
            return None
            
        if not self.enable_caching:
            # Fallback: send everything in a single message (no caching)
            return [genai.types.ContentDict(
                role="user",
                parts=[genai.types.PartDict(text=f"{system_instruction}\n\nUser: {user_message}")]
            )]

        messages = []

        # PART 1: Cached system + tools (byte-identical across requests)
        cached_system = self.build_cached_system_prompt(system_instruction, tool_declarations)
        messages.append(
            genai.types.ContentDict(
                role="user",
                parts=[genai.types.PartDict(text=cached_system)]
            )
        )

        # ✅ PART 2: Current user message (unique, not cached)
        messages.append(
            genai.types.ContentDict(
                role="user",
                parts=[genai.types.PartDict(text=user_message)]
            )
        )

        return messages

    def log_cache_efficiency(
        self,
        response: Any,
        endpoint: str = "background_worker",
        model: str = "gemini"
    ) -> Tuple[int, int]:
        """
        Log cache hit information and track statistics.
        
        Returns: (cached_tokens, fresh_tokens)
        """
        self.cache_stats["total_requests"] += 1
        
        if not hasattr(response, 'usage_metadata'):
            logger.warning(f"⚠️ No usage_metadata in response from {endpoint}")
            return 0, 0

        usage = response.usage_metadata
        cached_tokens = _usage_int(usage, "cached_content_token_count", "cached_content_input_tokens")

        prompt_tokens = _usage_field(usage, "prompt_token_count")
        if prompt_tokens is not _MISSING:
            # Gemini documents prompt_token_count as the TOTAL effective prompt --
            # cached content included. Adding cached_tokens on top would double
            # count it and report efficiencies above 100%.
            total_input = int(prompt_tokens)
            input_tokens = max(total_input - cached_tokens, 0)
        else:
            input_tokens = _usage_int(usage, "input_tokens")
            total_input = cached_tokens + input_tokens

        output_tokens = _usage_int(usage, "candidates_token_count", "output_tokens")

        if cached_tokens > 0:
            self.cache_stats["cache_hits"] += 1
            # Cached input is 90% cheaper; savings are per token, see constants above.
            cache_savings = cached_tokens * GEMINI_CACHE_SAVINGS_PER_TOKEN
            self.cache_stats["tokens_cached"] += cached_tokens
            self.cache_stats["tokens_saved"] += int(cached_tokens * 0.9)
            
            efficiency = (cached_tokens / total_input) * 100 if total_input > 0 else 0
            
            logger.info(
                f"💾 CACHE HIT [{endpoint}] | "
                f"Model: {model} | "
                f"Cached: {cached_tokens:,} | "
                f"Fresh: {input_tokens:,} | "
                f"Output: {output_tokens:,} | "
                f"Efficiency: {efficiency:.1f}% | "
                f"Est. Saved: ${cache_savings:.4f}"
            )
        else:
            self.cache_stats["cache_misses"] += 1
            logger.info(
                f"⚠️  CACHE MISS [{endpoint}] | "
                f"Model: {model} | "
                f"Fresh Input: {input_tokens:,} | "
                f"Output: {output_tokens:,}"
            )

        return cached_tokens, input_tokens

    def get_cache_statistics(self) -> Dict[str, Any]:
        """Return cache performance statistics for monitoring."""
        uptime = datetime.now() - self.cache_stats["requests_since_start"]
        total_req = self.cache_stats["total_requests"]
        
        hit_rate = (
            (self.cache_stats["cache_hits"] / total_req * 100)
            if total_req > 0
            else 0
        )
        
        # Rough cost estimation at the published per-million input rate.
        estimated_cost_without_cache = (
            total_req * AVG_INPUT_TOKENS_PER_REQUEST * GEMINI_INPUT_COST_PER_TOKEN
        )
        estimated_savings = self.cache_stats["tokens_saved"] * GEMINI_INPUT_COST_PER_TOKEN
        
        return {
            "uptime_seconds": uptime.total_seconds(),
            "total_requests": total_req,
            "cache_hits": self.cache_stats["cache_hits"],
            "cache_misses": self.cache_stats["cache_misses"],
            "hit_rate_percent": hit_rate,
            "total_cached_tokens": self.cache_stats["tokens_cached"],
            "estimated_tokens_saved": self.cache_stats["tokens_saved"],
            "estimated_cost_without_cache": f"${estimated_cost_without_cache:.4f}",
            "estimated_savings": f"${estimated_savings:.4f}",
            "recommendation": (
                "✅ Caching working well!" if hit_rate > 70
                else "⚠️  Low cache hit rate - check system prompt consistency"
            )
        }


# Global cache manager instance
cache_manager = PromptCacheManager(enable_caching=True, ttl_seconds=3600)
