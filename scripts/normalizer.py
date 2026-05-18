"""
Domain normalization helper.

Input CSV is required to have a `domain` column, so we don't port any of the
AI/SERP domain-guessing helpers from the source. extract_domain just cleans up
whatever the user gave us (full URL, bare host, or email).
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse


def extract_domain(
    website: Optional[str] = None, email: Optional[str] = None
) -> Optional[str]:
    """Extract a bare lowercase domain from a website URL or an email address."""
    if website:
        website = website.strip()
        if not website.startswith(("http://", "https://")):
            website = "https://" + website
        parsed = urlparse(website)
        domain = parsed.netloc or parsed.path.split("/")[0]
        domain = domain.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain or None

    if email:
        match = re.search(r"@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", email)
        if match:
            return match.group(1).lower()

    return None
