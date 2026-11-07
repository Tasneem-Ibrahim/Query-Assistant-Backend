"""Thin wrapper around the DeepSeek chat API (OpenAI-compatible)."""

from __future__ import annotations

import json
import re
import time

from . import config

_client = None


class LLMNotConfigured(RuntimeError):
    pass


def get_client():
    global _client
    if _client is None:
        if not config.DEEPSEEK_API_KEY:
            raise LLMNotConfigured(
                "DEEPSEEK_API_KEY is missing. Copy .env.example to .env and add your key."
            )
        from openai import OpenAI

        _client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL, timeout=90)
    return _client


def parse_json(raw: str) -> dict:
    """Parse JSON from a model reply, tolerating ``` fences and stray text around the object."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model did not return a JSON object")
    return data


def chat_json(system: str, user: str, max_tokens: int = 3000, retries: int = 3) -> dict:
    """Ask the model for a JSON object. Retries on API errors and invalid JSON."""
    client = get_client()
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = client.chat.completions.create(
                model=config.DEEPSEEK_MODEL,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=max_tokens,
            )
            return parse_json(response.choices[0].message.content or "")
        except LLMNotConfigured:
            raise
        except Exception as exc:  # network error, rate limit, invalid JSON ...
            last_error = exc
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"LLM call failed after {retries} attempts: {last_error}")
