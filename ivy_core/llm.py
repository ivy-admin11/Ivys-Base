"""Dual-brain query helper for standalone job agents: DeepSeek primary, Gemini backup.

Gemini is only tried when DeepSeek is unavailable (no API key), returns an
empty response, or raises — never merely because DeepSeek gave an honest
answer the caller didn't expect.
"""

import logging
import os
import re
from typing import Optional

from google import genai
from openai import OpenAI

logger = logging.getLogger("ivy.llm")

_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def strip_json_fence(text: str) -> str:
    """Strip a markdown code fence (```json ... ``` or ``` ... ```) that both
    DeepSeek and Gemini routinely wrap JSON responses in, even when asked for
    "ONLY valid JSON, no markdown formatting" — json.loads() chokes on the
    fence otherwise. Returns the input unchanged if there's no fence."""
    match = _JSON_FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text.strip()

_deepseek_client = None
_gemini_client = None


def _get_deepseek_client():
    global _deepseek_client
    if _deepseek_client is None:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            return None
        _deepseek_client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
    return _deepseek_client


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return None
        # Scoped unset: keep ADC out of the Generative Language API client
        # without disturbing Docs/Slides service-account auth elsewhere.
        saved = {
            k: os.environ.pop(k)
            for k in ("GOOGLE_APPLICATION_CREDENTIALS", "GCLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT")
            if k in os.environ
        }
        try:
            _gemini_client = genai.Client(api_key=api_key)
        finally:
            os.environ.update(saved)
    return _gemini_client


def query_llm(prompt_text: str, temperature: Optional[float] = None) -> str:
    """Dual-brain failover: deepseek-chat (primary) -> gemini-2.5-flash (backup).

    temperature is optional and forwarded to whichever provider answers —
    both SDKs accept it as a real generation parameter, not a made-up one.
    """
    deepseek = _get_deepseek_client()
    if deepseek is not None:
        try:
            logger.info("Querying primary engine (DeepSeek)...")
            kwargs = {} if temperature is None else {"temperature": temperature}
            response = deepseek.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt_text}],
                **kwargs,
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                return text
            logger.warning("DeepSeek returned an empty response; falling back to Gemini.")
        except Exception as exc:
            logger.warning("DeepSeek primary layer fault: %s. Falling back to Gemini.", exc)
    else:
        logger.warning("DEEPSEEK_API_KEY missing; skipping primary layer.")

    gemini = _get_gemini_client()
    if gemini is not None:
        try:
            logger.info("Engaging backup engine (Gemini)...")
            config = None if temperature is None else genai.types.GenerateContentConfig(temperature=temperature)
            response = gemini.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt_text,
                config=config,
            )
            return (response.text or "").strip()
        except Exception as exc:
            logger.warning("Gemini backup layer fault: %s", exc)
    else:
        logger.warning("GEMINI_API_KEY missing; no backup available.")

    return "System error: both primary and backup language models are unavailable."
