"""
Email pattern detection, generation, and disk-backed cache.

Detects an email pattern from a known (name, email, domain) triple, generates
candidate emails for new contacts at the same domain, and persists patterns to
~/.three-cent-contacts/pattern_cache.json with a 30-day TTL.

Source: ported from contact-finder-tool/backend/services/email_patterns.py.
Changes vs source:
  - In-memory _pattern_cache replaced with disk-backed JSON.
  - Real TTL eviction (30 days) added. Source had decay-only.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import httpx


PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "first_last": lambda f, l: f"{f}_{l}",
    "flast": lambda f, l: f"{f[0]}{l}" if f else None,
    "firstl": lambda f, l: f"{f}{l[0]}" if l else None,
    "first": lambda f, l: f,
    "last.first": lambda f, l: f"{l}.{f}",
    "f.last": lambda f, l: f"{f[0]}.{l}" if f else None,
    "last": lambda f, l: l,
    "lfirst": lambda f, l: f"{l[0]}{f}" if l else None,
    "last_first": lambda f, l: f"{l}_{f}",
}

HUNTER_PATTERN_MAP = {
    "{first}.{last}": "first.last",
    "{first}{last}": "firstlast",
    "{first}_{last}": "first_last",
    "{f}{last}": "flast",
    "{first}{l}": "firstl",
    "{first}": "first",
    "{last}.{first}": "last.first",
    "{f}.{last}": "f.last",
    "{last}": "last",
    "{l}{first}": "lfirst",
    "{last}_{first}": "last_first",
}

CACHE_DIR = Path.home() / ".three-cent-contacts"
CACHE_FILE = CACHE_DIR / "pattern_cache.json"
TTL_DAYS = 30


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_cache() -> Dict[str, Dict[str, Any]]:
    if not CACHE_FILE.exists():
        return {}
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    os.replace(tmp, CACHE_FILE)


def _is_expired(entry: Dict[str, Any]) -> bool:
    updated = _parse_iso(entry.get("updated_at")) or _parse_iso(entry.get("created_at"))
    if not updated:
        return True
    if updated.tzinfo:
        updated = updated.replace(tzinfo=None)
    return (datetime.utcnow() - updated) > timedelta(days=TTL_DAYS)


def _normalize_name(name: str) -> str:
    """
    Lowercase, fold diacritics to ASCII (Müller → muller), then strip anything
    that isn't a-z. Without the fold, non-ASCII letters were dropped entirely
    (Müller → mller), generating emails that can never verify.
    """
    folded = (
        unicodedata.normalize("NFKD", name.lower().strip())
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"[^a-z]", "", folded)


def detect_pattern(email: str, first_name: str, last_name: str) -> Optional[str]:
    """Given a verified email and name, return the pattern key or None."""
    if not email or not first_name or not last_name:
        return None
    try:
        local = email.split("@")[0].lower()
        first = _normalize_name(first_name)
        last = _normalize_name(last_name)
        if not first or not last:
            return None
        for pattern_name, generator in PATTERNS.items():
            try:
                expected = generator(first, last)
                if expected and local == expected:
                    return pattern_name
            except (IndexError, TypeError):
                continue
    except Exception:
        return None
    return None


def generate_email(
    first_name: str, last_name: str, domain: str, pattern: str
) -> Optional[str]:
    """Generate an email address using a known pattern."""
    if not first_name or not last_name or not domain or not pattern:
        return None
    first = _normalize_name(first_name)
    last = _normalize_name(last_name)
    if not first or not last:
        return None
    generator = PATTERNS.get(pattern)
    if not generator:
        return None
    try:
        local = generator(first, last)
        if local:
            return f"{local}@{domain}"
    except (IndexError, TypeError):
        return None
    return None


async def get_pattern_from_hunter(domain: str) -> Optional[Dict[str, Any]]:
    """
    Look up a domain's email pattern via Hunter.io domain-search.

    Cost: ~$0.01 per call (1 Hunter request). Returns None if HUNTER_API_KEY
    is unset, the request fails, or Hunter returns a pattern we don't map.
    """
    api_key = os.getenv("HUNTER_API_KEY")
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": api_key, "limit": 1},
            )
        if response.status_code != 200:
            return None
        hunter_pattern = response.json().get("data", {}).get("pattern")
        if not hunter_pattern:
            return None
        our_pattern = HUNTER_PATTERN_MAP.get(hunter_pattern)
        if not our_pattern:
            return None
        return {
            "pattern": our_pattern,
            "confidence": 0.85,
            "source": "hunter_api",
            "sample_count": 1,
        }
    except Exception:
        return None


def apply_confidence_decay(pattern: Dict[str, Any]) -> Dict[str, Any]:
    """Decay confidence 5% per 30 days without verification, floor 0.30."""
    if not pattern:
        return pattern
    last_update = _parse_iso(pattern.get("updated_at"))
    if not last_update:
        return pattern
    now = datetime.utcnow()
    if last_update.tzinfo:
        now = now.replace(tzinfo=last_update.tzinfo)
    days_since_update = (now - last_update).days
    decay_periods = days_since_update // 30
    if decay_periods <= 0:
        return pattern
    original = pattern.get("confidence", 0.5)
    decayed = max(0.30, original * (0.95 ** decay_periods))
    if decayed < original:
        pattern["confidence"] = round(decayed, 3)
        pattern["confidence_decayed"] = True
        pattern["days_since_update"] = days_since_update
    return pattern


async def get_cached_pattern(domain: str) -> Optional[Dict[str, Any]]:
    """Return cached pattern for a domain or None if missing/expired."""
    cache = _load_cache()
    entry = cache.get(domain)
    if not entry:
        return None
    if _is_expired(entry):
        cache.pop(domain, None)
        _write_cache(cache)
        return None
    return apply_confidence_decay(entry)


async def save_pattern(domain: str, pattern_data: Dict[str, Any]) -> None:
    """Persist a pattern to the disk cache."""
    cache = _load_cache()
    pattern_data = dict(pattern_data)
    pattern_data["domain"] = domain
    pattern_data["updated_at"] = _utcnow_iso()
    if domain in cache:
        existing = cache[domain]
        existing.update(pattern_data)
        existing["sample_count"] = existing.get("sample_count", 0) + 1
    else:
        pattern_data.setdefault("created_at", _utcnow_iso())
        pattern_data.setdefault("sample_count", 1)
        pattern_data.setdefault("verified_count", 0)
        pattern_data.setdefault("failed_count", 0)
        cache[domain] = pattern_data
    _write_cache(cache)


async def update_pattern_stats(domain: str, success: bool) -> None:
    """Update verified/failed counts and recompute blended confidence."""
    cache = _load_cache()
    pattern = cache.get(domain)
    if not pattern:
        return
    if success:
        pattern["verified_count"] = pattern.get("verified_count", 0) + 1
    else:
        pattern["failed_count"] = pattern.get("failed_count", 0) + 1
    total = pattern.get("verified_count", 0) + pattern.get("failed_count", 0)
    if total > 0:
        success_rate = pattern.get("verified_count", 0) / total
        original = pattern.get("confidence", 0.5)
        pattern["confidence"] = (original + success_rate) / 2
    pattern["updated_at"] = _utcnow_iso()
    cache[domain] = pattern
    _write_cache(cache)
