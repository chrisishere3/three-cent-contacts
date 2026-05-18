"""
Small async helper for OpenRouter chat completions.

We use plain httpx instead of the `openai` SDK to keep dependencies thin.
Same JSON-extraction tolerance as stage 1 (fenced blocks, prose-prefix).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import httpx


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TIMEOUT_S = 60.0


def extract_json(content: str) -> Any:
    """Strict parse, then fenced-block, then first {...}/[...] regex grab."""
    if not content:
        return None
    raw = content.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    if "```json" in raw:
        try:
            inner = raw.split("```json", 1)[1].split("```", 1)[0].strip()
            return json.loads(inner)
        except (IndexError, json.JSONDecodeError):
            pass
    elif "```" in raw:
        try:
            inner = raw.split("```", 1)[1].split("```", 1)[0].strip()
            return json.loads(inner)
        except (IndexError, json.JSONDecodeError):
            pass
    for pattern in (r"\[.*\]", r"\{.*\}"):
        match = re.search(pattern, raw, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
    return None


async def chat_completion(
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 800,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Optional[str]:
    """
    Call OpenRouter chat completions and return raw message content,
    or None on missing key / non-200 / exception.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(OPENROUTER_URL, headers=headers, json=payload)
        if response.status_code != 200:
            return None
        data = response.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "") or None
    except Exception:
        return None
