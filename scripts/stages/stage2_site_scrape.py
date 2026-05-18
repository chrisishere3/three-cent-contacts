"""
Stage 2 — free website scrape.

Pulls a handful of likely-team-info pages (homepage, /about, /team, /leadership,
/contact, etc.), strips HTML, and asks Gemini Flash via OpenRouter to extract
named leadership.

Returns a list of dicts shaped like stage 1's output so the orchestrator can
treat both stages uniformly.

Source: contact-finder-tool/backend/services/website_scraper.py.
Key change vs source: extract_contacts_from_website returns a *list* (source
returned a single dict) and accepts a free-text `title` instead of a fixed
role enum. Gemini model centralized to MODEL constant.

Cost: stage 2 itself is free (direct httpx); the AI extraction call is a few
tenths of a cent.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import httpx

from _openrouter import chat_completion, extract_json  # type: ignore


MODEL = "google/gemini-2.0-flash-001"
SCRAPE_TIMEOUT_S = 15.0
MAX_PAGES = 4
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PAGES_TO_TRY = [
    ("homepage", ""),
    ("about", "/about"),
    ("about-us", "/about-us"),
    ("team", "/team"),
    ("our-team", "/our-team"),
    ("leadership", "/leadership"),
    ("contact", "/contact"),
    ("contact-us", "/contact-us"),
]
PRIORITY_ORDER = [
    "about", "about-us", "team", "our-team", "leadership",
    "contact", "contact-us", "homepage",
]


async def scrape_website_page(url: str) -> Optional[str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT_S, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
        if response.status_code == 200 and response.text:
            return response.text
    except Exception:
        return None
    return None


async def scrape_company_pages(domain: str) -> Dict[str, str]:
    """Return {page_name: html} for up to MAX_PAGES non-empty pages."""
    if not domain.startswith("http"):
        domain = f"https://{domain}"
    domain = domain.rstrip("/")
    results: Dict[str, str] = {}
    for page_name, path in PAGES_TO_TRY:
        if len(results) >= MAX_PAGES:
            break
        html = await scrape_website_page(f"{domain}{path}")
        if html and len(html) > 500:
            results[page_name] = html
    return results


def extract_text_from_html(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<head[^>]*>.*?</head>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:15000]


async def _parse_with_ai(text: str, target_role: str, domain: str) -> List[Dict[str, Any]]:
    prompt = f"""Analyze this company website content and find people who hold the role "{target_role}" or closely related leadership roles.

Company domain: {domain}

Website content:
{text[:12000]}

Extract every named person you find with a leadership/relevant role. Prioritize the person whose title best matches "{target_role}", but include other named leaders too.

Respond ONLY with a JSON array. Each item has:
- first_name (string or null)
- last_name (string or null)
- title (exact title as shown, string or null)
- email (string or null)
- phone (string or null)
- linkedin_url (string or null)

Return [] if no named people appear on the pages."""

    content = await chat_completion(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=800,
    )
    parsed = extract_json(content) if content else None
    if isinstance(parsed, list):
        return [c for c in parsed if isinstance(c, dict) and (c.get("first_name") or c.get("email"))]
    if isinstance(parsed, dict) and (parsed.get("first_name") or parsed.get("email")):
        return [parsed]
    return []


async def extract_contacts_from_website(
    domain: str,
    target_role: str = "owner",
) -> List[Dict[str, Any]]:
    """
    Scrape company pages and AI-extract leadership contacts.

    Returns a list of candidate dicts. Empty list when nothing usable
    was found (or scraping was fully blocked).
    """
    pages = await scrape_company_pages(domain)
    if not pages:
        return []
    prioritized = ""
    for page_name in PRIORITY_ORDER:
        if page_name in pages:
            text = extract_text_from_html(pages[page_name])
            prioritized += f"\n\n=== {page_name.upper()} PAGE ===\n{text}"
    if len(prioritized) < 100:
        return []
    return await _parse_with_ai(prioritized, target_role, domain)
