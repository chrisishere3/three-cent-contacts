"""
Stage 7 — Bright Data SERP fallback.

Used for the hard targets where stages 1-6 produced nothing usable. We ask
Google "{title} at {company} email" via Bright Data's SERP proxy, parse the
returned HTML for organic results, and ask Gemini Flash to extract any named
contacts. **Critical:** we then apply a strict @domain post-filter before
returning anything, because Google snippets routinely surface personal
addresses (gmail/yahoo) and unrelated-company addresses that would otherwise
get sent to Bouncer.

Source: contact-finder-tool/backend/services/serp_scraper.py.
Key change vs source: source had NO domain filter and used SDK calls; this
version uses httpx + applies @domain filter unconditionally. Source's per-
person query shape (`first_name last_name`) is replaced with a company-search
query rebuilt from title + company/domain.

Cost: ~$0.015-0.025 per row that reaches stage 7 (proxy charge per request +
one Gemini parse).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import httpx

from _openrouter import GEMINI_MODEL as MODEL, chat_completion, extract_json  # type: ignore


PROXY_HOST = "brd.superproxy.io:33335"
QUERY_TIMEOUT_S = 60.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _proxy_url() -> Optional[str]:
    user = os.getenv("BRIGHTDATA_SERP_USERNAME")
    pwd = os.getenv("BRIGHTDATA_SERP_PASSWORD")
    if not user or not pwd:
        return None
    return f"http://{user}:{pwd}@{PROXY_HOST}"


async def search_serp_query(query: str) -> List[Dict[str, Any]]:
    """Query Google via Bright Data proxy; return parsed organic results."""
    proxy = _proxy_url()
    if not proxy:
        return []
    search_url = f"https://www.google.com/search?q={quote_plus(query)}&num=15"
    try:
        async with httpx.AsyncClient(proxy=proxy, verify=False, timeout=QUERY_TIMEOUT_S) as client:
            response = await client.get(search_url, headers={"User-Agent": USER_AGENT})
        if response.status_code != 200:
            return []
        return parse_google_serp_html(response.text, query)
    except Exception:
        return []


def parse_google_serp_html(html: str, query: str) -> List[Dict[str, Any]]:
    """Extract organic title + link + snippet triples from Google SERP HTML."""
    link_pattern = re.compile(r'<a[^>]+href="(/url\?q=|)(https?://[^"&]+)[^"]*"[^>]*>')
    title_pattern = re.compile(r"<h3[^>]*>([^<]+)</h3>")
    seen: List[str] = []
    for match in link_pattern.finditer(html):
        url = match.group(2)
        if not url:
            continue
        if any(skip in url for skip in ("google.com", "googleapis.com", "gstatic.com", "youtube.com/results")):
            continue
        if url not in seen:
            seen.append(url)
    titles = [re.sub(r"<[^>]+>", "", t) for t in title_pattern.findall(html)]
    results: List[Dict[str, Any]] = []
    for i, link in enumerate(seen[:10]):
        title = titles[i] if i < len(titles) else ""
        snippet = ""
        link_pos = html.find(link)
        if link_pos > 0:
            window = html[link_pos : link_pos + 1000]
            span_match = re.search(r"<span[^>]*>([^<]{50,300})</span>", window)
            if span_match:
                snippet = span_match.group(1)
        results.append({"title": title, "link": link, "snippet": snippet, "query": query})
    return results


async def extract_contacts_from_serp(
    results: List[Dict[str, Any]], company_context: str
) -> List[Dict[str, Any]]:
    """AI-extract candidate contacts from SERP snippets. Returns list of dicts."""
    if not results:
        return []
    snippets_text = "\n\n".join(
        f"Title: {r.get('title','')}\nURL: {r.get('link','')}\nSnippet: {r.get('snippet','')}"
        for r in results[:15]
    )
    if not snippets_text.strip():
        return []
    messages = [
        {
            "role": "system",
            "content": (
                "You are a data extraction assistant. Extract named people from "
                "Google search results. Return ONLY a JSON array of objects with "
                "fields: first_name, last_name, title, email, phone, linkedin_url. "
                "Use null for missing fields. Return [] if no named people appear."
            ),
        },
        {
            "role": "user",
            "content": f"Find key personnel for: {company_context}\n\nSearch results:\n{snippets_text}",
        },
    ]
    content = await chat_completion(model=MODEL, messages=messages, temperature=0.0, max_tokens=1000)
    parsed = extract_json(content) if content else None
    if isinstance(parsed, list):
        return [c for c in parsed if isinstance(c, dict) and (c.get("first_name") or c.get("email"))]
    return []


def filter_to_domain(candidates: List[Dict[str, Any]], domain: str) -> List[Dict[str, Any]]:
    """
    Mandatory post-filter: only keep candidates whose email is at the target
    domain. Source code lacked this; without it, Bouncer would burn credits
    verifying gmail/yahoo addresses that came back from the search.
    """
    if not domain:
        return []
    target = domain.lower()

    def _on_domain(email: str) -> bool:
        email_domain = email.strip().lower().rpartition("@")[2]
        return email_domain == target or email_domain.endswith(f".{target}")

    return [c for c in candidates if _on_domain(c.get("email") or "")]


async def serp_lookup(company: str, domain: str, title: str) -> List[Dict[str, Any]]:
    """
    Full stage 7: build query, hit SERP, AI-extract, apply @domain filter.
    Convenience function called by the orchestrator.
    """
    company_label = company or domain
    query = f'"{title}" "{company_label}" email'
    serp = await search_serp_query(query)
    candidates = await extract_contacts_from_serp(serp, f"{title} at {company_label}")
    return filter_to_domain(candidates, domain)
