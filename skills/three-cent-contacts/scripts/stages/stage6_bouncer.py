"""
Stage 6 — Bouncer SMTP verification.

Reads BOUNCER_API_KEY first, falls back to legacy USEBOUNCER_API_KEY.
Returns a plain dict; no models import.

Cost: ~$0.01 per verification.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import httpx


BOUNCER_URL = "https://api.usebouncer.com/v1.1/email/verify"


def _api_key() -> str | None:
    return os.getenv("BOUNCER_API_KEY") or os.getenv("USEBOUNCER_API_KEY")


async def verify_email(email: str) -> Dict[str, Any]:
    """
    Verify a single email. Status: deliverable, risky, undeliverable, unknown.

    Returns a dict with at minimum: email, verified (bool|None), status.
    """
    api_key = _api_key()
    if not api_key:
        return {"email": email, "verified": None, "status": "unknown",
                "reason": "no_api_key"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                BOUNCER_URL,
                params={"email": email},
                headers={"x-api-key": api_key},
            )
        if response.status_code != 200:
            return {
                "email": email,
                "verified": None,
                "status": "error",
                "reason": f"http_{response.status_code}",
            }
        data = response.json()
        status = data.get("status", "unknown")
        return {
            "email": email,
            "verified": status == "deliverable",
            "risky": status == "risky",
            "status": status,
            "reason": data.get("reason"),
            "score": data.get("score"),
            "toxic": data.get("toxic"),
            "did_you_mean": data.get("didYouMean"),
        }
    except Exception as e:
        return {
            "email": email,
            "verified": None,
            "status": "error",
            "reason": f"exception: {e}",
        }


async def verify_emails_batch(emails: List[str]) -> List[Dict[str, Any]]:
    """Verify multiple unique emails sequentially."""
    seen = set()
    results: List[Dict[str, Any]] = []
    for email in emails:
        if not email or email in seen:
            continue
        seen.add(email)
        results.append(await verify_email(email))
    return results
