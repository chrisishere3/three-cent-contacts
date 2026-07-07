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
  - Calls go through _openrouter.chat_completion, which retries transient
    failures (429/5xx/timeouts) with backoff.
  - max_tokens=1200: five contacts with seven fields regularly exceeded the
    old 500-token cap; a truncated array fails every parse fallback and the
    whole stage-1 result was silently discarded.
  - JSON parsing falls back to regex extraction when Sonar prefixes prose.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from _openrouter import chat_completion, extract_json  # type: ignore


MODEL = "perplexity/sonar"
MAX_TOKENS = 1200


ROLE_DESCRIPTIONS = {
    "owner": "owners, founders, principals, or executives",
    "founder": "founders or co-founders",
    "property_manager": "property managers or general managers",
    "ceo": "CEO, president, or executive directors",
    "contact": "key contacts, owners, executives, or decision makers",
}


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
    messages = [
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
    ]

    content = await chat_completion(
        model=MODEL, messages=messages, temperature=0.1, max_tokens=MAX_TOKENS
    )
    if not content:
        return None

    parsed = extract_json(content)
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
