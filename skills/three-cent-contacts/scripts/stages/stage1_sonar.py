"""
Stage 1 — Perplexity Sonar (via OpenRouter) deep-research.

Given a company + domain + target role, ask Sonar to return up to N likely
decision-makers with whatever contact info it can verify. Returns a list of
plain dicts. Cost: ~$0.005 per call.

Source: research_company_contacts from contact-finder-tool/backend/services/
ai_researcher.py. Single-person research path is intentionally dropped — the
waterfall iterates rows, not people, so we always ask for "up to N contacts at
this company in this role".

Hardening vs source:
  - Model centralized to MODEL constant (was hardcoded inline twice).
  - JSON parsing falls back to regex extraction when Sonar prefixes prose.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx


MODEL = "perplexity/sonar"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REQUEST_TIMEOUT_S = 60.0


ROLE_DESCRIPTIONS = {
    "owner": "owners, founders, principals, or executives",
    "founder": "founders or co-founders",
    "property_manager": "property managers or general managers",
    "ceo": "CEO, president, or executive directors",
    "contact": "key contacts, owners, executives, or decision makers",
}


def _api_key() -> Optional[str]:
    return os.getenv("OPENROUTER_API_KEY")


def _extract_json(content: str) -> Any:
    """
    Sonar usually returns clean JSON but sometimes prefixes prose, wraps it in
    ```json fences, or appends a citation block. Try strict parse first, then
    fenced-block extraction, then a regex grab of the first {...} or [...].
    """
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


def _build_query(company: str, domain: Optional[str], target_role: str, max_contacts: int) -> str:
    role_desc = ROLE_DESCRIPTIONS.get(target_role, target_role or "owners or key contacts")
    company_info = f"{company} ({domain})" if domain else company
    return f"""Find up to {max_contacts} {role_desc} at {company_info} and their contact information.

Search for key personnel including:
- Owners, founders, principals
- Executives (CEO, President, VP)
- Property managers or general managers
- Any key decision makers

For each person, find:
- Their full name
- Their job title
- Their email address
- Their phone number
- Their LinkedIn profile

Search across company websites, press releases, news articles, podcasts, conferences, LinkedIn, and any public sources.

Return ONLY a JSON array of contacts (use null for missing fields):
[
    {{
        "first_name": "John",
        "last_name": "Doe",
        "title": "CEO",
        "email": "john@company.com",
        "phone": "+1-555-123-4567",
        "linkedin_url": "https://linkedin.com/in/johndoe",
        "source": "company website"
    }}
]

Only return verified information you actually found. Do not guess or make up contact info. Return an empty array [] if no contacts found."""


async def research_company_contacts(
    company: str,
    domain: Optional[str] = None,
    target_role: str = "owner",
    max_contacts: int = 5,
) -> Optional[List[Dict[str, Any]]]:
    """Return a list of contact dicts, or None when the call/parse fails."""
    api_key = _api_key()
    if not api_key:
        return None

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a research assistant that finds business contacts. "
                    "Only return factual information you find in your search. "
                    "Never make up or guess contact details. Return results as JSON."
                ),
            },
            {
                "role": "user",
                "content": _build_query(company, domain, target_role, max_contacts),
            },
        ],
        "temperature": 0.1,
        "max_tokens": 500,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            response = await client.post(OPENROUTER_URL, headers=headers, json=payload)
        if response.status_code != 200:
            return None
        data = response.json()
        content = (
            data.get("choices", [{}])[0].get("message", {}).get("content", "")
        )
    except Exception:
        return None

    parsed = _extract_json(content)
    if parsed is None:
        return None

    if isinstance(parsed, list):
        valid = [
            c for c in parsed
            if isinstance(c, dict) and (c.get("first_name") or c.get("email"))
        ]
        return valid or None

    if isinstance(parsed, dict):
        if parsed.get("first_name") or parsed.get("email"):
            return [parsed]
        return None

    return None
