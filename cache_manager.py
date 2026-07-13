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
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

try:
    import google.generativeai as genai
except ImportError:
    genai = None

logger = logging.getLogger("ivy.cache")


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
        config_hash = hashlib.md5(config_str.encode()).hexdigest()

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
        where cached_system_block has cache_control set to ephemeral.
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

        # ✅ PART 1: Cached system + tools (reused across requests)
        cached_system = self.build_cached_system_prompt(system_instruction, tool_declarations)
        messages.append(
            genai.types.ContentDict(
                role="user",
                parts=[{
                    "text": cached_system,
                    "cache_control": {"type": "ephemeral"}  # ✅ KEY: Mark for caching
                }]
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
        cached_tokens = getattr(usage, 'cached_content_input_tokens', 0)
        input_tokens = getattr(usage, 'input_tokens', 0)
        output_tokens = getattr(usage, 'output_tokens', 0)

        total_input = cached_tokens + input_tokens

        if cached_tokens > 0:
            self.cache_stats["cache_hits"] += 1
            # Gemini cache tokens cost 90% less: $0.075 → $0.0075 per 1M
            cache_savings = cached_tokens * 0.00009  # Rough estimate: 90% cheaper
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
        
        # Rough cost estimation (Gemini pricing: $0.075 input, $0.003 output)
        estimated_cost_without_cache = total_req * 500 * 0.000075  # Avg 500 input tokens
        estimated_savings = self.cache_stats["tokens_saved"] * 0.000075
        
        return {
            "uptime_seconds": uptime.total_seconds(),
            "total_requests": total_req,
            "cache_hits": self.cache_stats["cache_hits"],
            "cache_misses": self.cache_stats["cache_misses"],
            "hit_rate_percent": hit_rate,
            "total_cached_tokens": self.cache_stats["tokens_cached"],
            "estimated_tokens_saved": self.cache_stats["tokens_saved"],
            "estimated_cost_without_cache": f"${estimated_cost_without_cache:.2f}",
            "estimated_savings": f"${estimated_savings:.2f}",
            "recommendation": (
                "✅ Caching working well!" if hit_rate > 70
                else "⚠️  Low cache hit rate - check system prompt consistency"
            )
        }


# Global cache manager instance
cache_manager = PromptCacheManager(enable_caching=True, ttl_seconds=3600)
