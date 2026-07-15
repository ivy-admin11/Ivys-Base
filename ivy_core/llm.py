"""Dual-brain query helper for standalone job agents: DeepSeek primary, Gemini backup.

Gemini is only tried when DeepSeek is unavailable (no API key), returns an
empty response, or raises — never merely because DeepSeek gave an honest
answer the caller didn't expect.
"""

import logging
import os

from google import genai
from openai import OpenAI

logger = logging.getLogger("ivy.llm")

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


def query_llm(prompt_text: str) -> str:
    """Dual-brain failover: deepseek-chat (primary) -> gemini-2.5-flash (backup)."""
    deepseek = _get_deepseek_client()
    if deepseek is not None:
        try:
            logger.info("Querying primary engine (DeepSeek)...")
            response = deepseek.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt_text}],
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
            response = gemini.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt_text,
            )
            return (response.text or "").strip()
        except Exception as exc:
            logger.warning("Gemini backup layer fault: %s", exc)
    else:
        logger.warning("GEMINI_API_KEY missing; no backup available.")

    return "System error: both primary and backup language models are unavailable."
