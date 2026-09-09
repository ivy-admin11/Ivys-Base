"""Tests for the prompt-cache accounting that feeds the /cache-stats endpoint.

cache_manager had no tests. Nothing in it can crash a request loudly enough to
notice -- it builds a prompt prefix and then reports numbers about it -- so the
failure mode is a dashboard that confidently states the wrong thing. Three live
bugs were found while writing these and are pinned below:

  1. log_cache_efficiency read usage_metadata fields that the Gemini SDK does
     not emit, so every real response was counted as a cache MISS with zero
     tokens -- the exact symptom this module exists to detect.
  2. create_cached_gemini_request stamped an Anthropic-style "cache_control"
     key into a Gemini Part dict, which makes the SDK raise KeyError. The
     caching path could never have completed a request.
  3. The cost estimates multiplied token counts by a PER-MILLION price,
     overstating spend and savings by 1000x.

Everything here runs against fresh PromptCacheManager instances; the module's
global `cache_manager` singleton is never mutated, and nothing touches disk,
network, or a real API key.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import cache_manager as cache_manager_module
from cache_manager import (
    GEMINI_INPUT_COST_PER_TOKEN,
    PromptCacheManager,
)

TOOLS = [
    {
        "name": "check_calendar",
        "description": "Scan the calendar.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "timeframe": {"type": "STRING", "description": "today or tomorrow"},
                "limit": {"type": "INTEGER"},
            },
            "required": ["timeframe"],
        },
    }
]


def gemini_usage(*, prompt: int, cached: int = 0, candidates: int = 0):
    """A response shaped the way the Gemini SDK actually shapes one.

    Note `prompt` is the TOTAL effective prompt size: Google documents
    prompt_token_count as including the cached content, not as the fresh
    remainder.
    """
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            cached_content_token_count=cached,
            candidates_token_count=candidates,
            total_token_count=prompt + candidates,
        )
    )


def legacy_usage(*, input_tokens: int, cached: int = 0, output_tokens: int = 0):
    """A response using the generic input/output naming the module was written against."""
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            cached_content_input_tokens=cached,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )


@pytest.fixture
def manager():
    return PromptCacheManager(enable_caching=True, ttl_seconds=60)


class TestUsageMetadataFieldNames:
    def test_a_gemini_cache_hit_is_recorded_as_a_hit(self, manager):
        """Regression: the module read field names Gemini does not emit.

        A response with 9k of its 10k prompt served from cache was reported as a
        MISS with 0 tokens, so /cache-stats showed a permanent 0% hit rate --
        indistinguishable from caching being genuinely broken.
        """
        cached, fresh = manager.log_cache_efficiency(gemini_usage(prompt=10_000, cached=9_000))

        assert (cached, fresh) == (9_000, 1_000)
        assert manager.cache_stats["cache_hits"] == 1
        assert manager.cache_stats["cache_misses"] == 0
        assert manager.cache_stats["tokens_cached"] == 9_000

    def test_field_names_match_the_installed_sdk(self, manager):
        """Pins the names against the real SDK object so they cannot drift again.

        The stub above is only as good as its resemblance to the genuine
        protobuf; this builds the genuine one.
        """
        from google.genai import types as genai_types

        # Built from the SDK that now produces these responses. google-genai
        # happens to spell the fields identically to the retired protobuf
        # layer, which is why the migration did not silently report every
        # request as a cache miss — but "happens to" is exactly why this is
        # pinned against the real object rather than a stub.
        usage = genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10_000,
            cached_content_token_count=9_000,
            candidates_token_count=250,
            total_token_count=10_250,
        )

        assert manager.log_cache_efficiency(SimpleNamespace(usage_metadata=usage)) == (9_000, 1_000)

    def test_cached_tokens_are_not_double_counted_in_efficiency(self, manager, caplog):
        """prompt_token_count already contains the cached tokens.

        Adding them on top would report 9000/19000 = 47% for a request that was
        actually 90% cached -- or, with other numbers, an impossible >100%.
        """
        caplog.set_level(logging.INFO, logger="ivy.cache")
        manager.log_cache_efficiency(gemini_usage(prompt=10_000, cached=9_000))

        assert "Efficiency: 90.0%" in caplog.text

    def test_generic_input_output_names_still_understood(self, manager):
        """Non-Gemini responses name the fields input_tokens/output_tokens.

        There, input_tokens is the fresh remainder rather than the total, so it
        must not have the cached count subtracted from it.
        """
        assert manager.log_cache_efficiency(legacy_usage(input_tokens=1_000, cached=9_000)) == (9_000, 1_000)

    def test_a_zero_cached_count_is_a_miss_not_a_missing_field(self, manager):
        cached, fresh = manager.log_cache_efficiency(gemini_usage(prompt=500, cached=0))

        assert (cached, fresh) == (0, 500)
        assert manager.cache_stats["cache_misses"] == 1
        assert manager.cache_stats["cache_hits"] == 0

    def test_response_without_usage_metadata_invents_nothing(self, manager):
        """Some error/blocked responses carry no usage at all.

        It must not be scored either way -- counting it as a hit would inflate
        the rate, counting the tokens as real would inflate the savings.
        """
        assert manager.log_cache_efficiency(SimpleNamespace()) == (0, 0)
        assert manager.cache_stats["cache_hits"] == 0
        assert manager.cache_stats["tokens_cached"] == 0

    def test_tokens_saved_is_the_ninety_percent_discount_on_cached_tokens(self, manager):
        manager.log_cache_efficiency(gemini_usage(prompt=11_000, cached=10_000))

        assert manager.cache_stats["tokens_saved"] == 9_000


class TestCostEstimates:
    def test_savings_use_the_per_million_rate(self, manager):
        """Regression: the code multiplied tokens by the per-MILLION price.

        One million cached tokens is 0.9M token-equivalents saved at $0.075/M,
        i.e. about seven cents. The unfixed code reported $67.50 -- a 1000x
        overstatement on the endpoint the owner uses to judge spend.
        """
        manager.log_cache_efficiency(gemini_usage(prompt=1_000_000, cached=1_000_000))
        stats = manager.get_cache_statistics()

        assert stats["estimated_tokens_saved"] == 900_000
        assert float(stats["estimated_savings"].lstrip("$")) == pytest.approx(0.0675, abs=1e-4)

    def test_uncached_cost_estimate_uses_the_per_million_rate(self, manager):
        """1000 requests x 500 assumed tokens = 500k tokens = $0.0375, not $37.50."""
        for _ in range(1_000):
            manager.log_cache_efficiency(gemini_usage(prompt=500))

        cost = float(manager.get_cache_statistics()["estimated_cost_without_cache"].lstrip("$"))
        assert cost == pytest.approx(500_000 * GEMINI_INPUT_COST_PER_TOKEN, abs=1e-6)
        assert cost == pytest.approx(0.0375, abs=1e-4)

    def test_per_token_rate_is_the_published_rate_divided_by_a_million(self):
        # The constant itself is the thing that was wrong; pin it directly.
        assert GEMINI_INPUT_COST_PER_TOKEN == pytest.approx(0.075 / 1_000_000)

    def test_no_requests_reports_zero_rather_than_dividing_by_zero(self, manager):
        stats = manager.get_cache_statistics()

        assert stats["total_requests"] == 0
        assert stats["hit_rate_percent"] == 0
        assert stats["uptime_seconds"] >= 0


class TestBuildCachedSystemPrompt:
    def test_unchanged_config_returns_a_byte_identical_block(self, manager):
        """A stable prefix is the entire mechanism -- one changed byte and the
        cache misses, which is the failure this module was built to prevent."""
        first = manager.build_cached_system_prompt("You are Ivy.", TOOLS)
        second = manager.build_cached_system_prompt("You are Ivy.", TOOLS)

        assert first == second

    def test_changed_system_instruction_rebuilds_the_block(self, manager):
        first = manager.build_cached_system_prompt("You are Ivy.", TOOLS)
        second = manager.build_cached_system_prompt("You are Ivy, a sports agent.", TOOLS)

        assert first != second
        assert "a sports agent" in second

    def test_changed_tool_list_rebuilds_the_block(self, manager):
        """Serving a stale prefix after a tool is added would advertise a tool
        list that no longer matches what is passed to generate_content."""
        manager.build_cached_system_prompt("You are Ivy.", TOOLS)
        extra = TOOLS + [{"name": "send_text", "description": "Send an iMessage."}]
        rebuilt = manager.build_cached_system_prompt("You are Ivy.", extra)

        assert "send_text" in rebuilt

    def test_key_order_in_a_tool_declaration_does_not_bust_the_cache(self, manager):
        """The config hash is taken over sorted JSON, so a dict literal written
        in a different order must still be recognised as the same config."""
        first = manager.build_cached_system_prompt("You are Ivy.", [{"name": "a", "description": "d"}])
        hash_after_first = manager._cached_system_hash
        manager.build_cached_system_prompt("You are Ivy.", [{"description": "d", "name": "a"}])

        assert manager._cached_system_hash == hash_after_first
        assert manager._cached_system_block == first

    def test_caching_disabled_returns_the_bare_instruction(self):
        off = PromptCacheManager(enable_caching=False)

        assert off.build_cached_system_prompt("You are Ivy.", TOOLS) == "You are Ivy."


class TestToolFormatting:
    def test_required_and_optional_parameters_are_distinguished(self, manager):
        block = manager.build_cached_system_prompt("sys", TOOLS)

        assert "timeframe (required)" in block
        assert "limit (optional)" in block

    def test_a_tool_without_parameters_is_still_listed(self, manager):
        block = manager.build_cached_system_prompt("sys", [{"name": "ping", "description": "Ping."}])

        assert "## ping" in block
        assert "Ping." in block

    def test_missing_name_or_description_does_not_raise(self, manager):
        block = manager.build_cached_system_prompt("sys", [{}])

        assert "unknown" in block
        assert "No description" in block


class TestCreateCachedGeminiRequest:
    def test_the_message_list_is_accepted_by_the_gemini_sdk(self, manager):
        """Regression: the cached part carried a "cache_control" key.

        Gemini has no such field -- it is Anthropic's -- and the SDK's Part
        conversion raises KeyError on any unrecognised key, so every
        caching-enabled request blew up inside generate_content. main.py's only
        guard is `if messages is None`, which this sails straight past.
        """
        from google.genai import types as genai_types

        messages = manager.create_cached_gemini_request("Who plays tonight?", "You are Ivy.", TOOLS)

        # Validated against the SDK that actually ships now. Confirmed to still
        # bite: Content.model_validate rejects a part carrying cache_control,
        # so this is a real check and not a no-op that happens to pass.
        for message in messages:
            genai_types.Content.model_validate(message)

    def test_no_anthropic_cache_control_key_leaks_into_a_part(self, manager):
        messages = manager.create_cached_gemini_request("Who plays tonight?", "You are Ivy.", TOOLS)

        for message in messages:
            for part in message["parts"]:
                assert "cache_control" not in part

    def test_user_text_is_kept_out_of_the_cached_prefix(self, manager):
        """If the per-request question were folded into the prefix, the prefix
        would differ every time and nothing would ever be cached."""
        messages = manager.create_cached_gemini_request("Who plays tonight?", "You are Ivy.", TOOLS)

        assert len(messages) == 2
        prefix = messages[0]["parts"][0]["text"]
        assert "You are Ivy." in prefix
        assert "Who plays tonight?" not in prefix
        assert messages[1]["parts"][0]["text"] == "Who plays tonight?"

    def test_the_prefix_is_identical_across_two_requests(self, manager):
        first = manager.create_cached_gemini_request("q1", "You are Ivy.", TOOLS)
        second = manager.create_cached_gemini_request("q2", "You are Ivy.", TOOLS)

        assert first[0]["parts"][0]["text"] == second[0]["parts"][0]["text"]

    def test_caching_disabled_collapses_to_one_combined_message(self):
        off = PromptCacheManager(enable_caching=False)
        messages = off.create_cached_gemini_request("Who plays tonight?", "You are Ivy.", TOOLS)

        assert len(messages) == 1
        assert "You are Ivy." in messages[0]["parts"][0]["text"]
        assert "Who plays tonight?" in messages[0]["parts"][0]["text"]

    def test_it_no_longer_needs_an_sdk_to_build_a_dict(self, manager):
        """This returned None whenever `import google.generativeai` failed, and
        main.py read that as "caching unavailable" and silently dropped to an
        uncached request. The import had nothing to do with caching: the module
        only ever assembled {"role", "parts"} dicts, which both the retired SDK
        and google-genai accept. So the dependency is gone, and with it the
        failure mode where an unrelated import error quietly cost every request
        its cache.

        main.py still branches on `messages is None` and that branch is
        harmless, but nothing here reaches it any more.
        """
        assert not hasattr(cache_manager_module, "genai"), (
            "cache_manager must not import an SDK to construct a dict"
        )
        out = manager.create_cached_gemini_request("q", "sys", TOOLS)
        assert out is not None
        assert out[0]["role"] == "user"
        assert isinstance(out[0]["parts"][0]["text"], str)


class TestStatisticsReporting:
    def test_hit_rate_is_a_percentage_of_all_requests(self, manager):
        manager.log_cache_efficiency(gemini_usage(prompt=1_000, cached=900))
        manager.log_cache_efficiency(gemini_usage(prompt=1_000, cached=0))
        manager.log_cache_efficiency(gemini_usage(prompt=1_000, cached=0))
        manager.log_cache_efficiency(gemini_usage(prompt=1_000, cached=900))

        stats = manager.get_cache_statistics()
        assert stats["total_requests"] == 4
        assert stats["hit_rate_percent"] == pytest.approx(50.0)

    def test_a_healthy_hit_rate_is_not_reported_as_a_problem(self, manager):
        """The recommendation string is what the owner actually reads; with the
        field-name bug it always said "low hit rate" no matter how well caching
        was working."""
        for _ in range(10):
            manager.log_cache_efficiency(gemini_usage(prompt=1_000, cached=900))

        assert "working well" in manager.get_cache_statistics()["recommendation"]

    def test_a_poor_hit_rate_is_flagged(self, manager):
        for _ in range(10):
            manager.log_cache_efficiency(gemini_usage(prompt=1_000, cached=0))

        assert "Low cache hit rate" in manager.get_cache_statistics()["recommendation"]
