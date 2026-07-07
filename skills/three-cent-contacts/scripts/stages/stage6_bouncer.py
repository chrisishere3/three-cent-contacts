"""
Stage 6 — Bouncer SMTP verification.

Reads BOUNCER_API_KEY first, falls back to legacy USEBOUNCER_API_KEY.
Returns a plain dict; no models import.

Transient failures (429/5xx/timeouts) retry twice with backoff. A 402
(payment required = credits exhausted) raises BouncerCreditsError so the
orchestrator can stop the whole batch loudly instead of silently burning
Sonar/Hunter/SERP money on rows that can never verify.

Cost: ~$0.01 per verification.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List

import httpx


BOUNCER_URL = "https://api.usebouncer.com/v1.1/email/verify"
MAX_ATTEMPTS = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class BouncerCreditsError(Exception):
    """Raised when Bouncer returns 402 — the account is out of credits."""


def _api_key() -> str | None:
    return os.getenv("BOUNCER_API_KEY") or os.getenv("USEBOUNCER_API_KEY")


async def verify_email(email: str) -> Dict[str, Any]:
    """
    Verify a single email. Status: deliverable, risky, undeliverable, unknown.

    Returns a dict with at minimum: email, verified (bool|None), status.
    Raises BouncerCreditsError on HTTP 402 (out of credits).
    """
    api_key = _api_key()
    if not api_key:
        return {"email": email, "verified": None, "status": "unknown",
                "reason": "no_api_key"}

    last_reason = "unknown"
    for attempt in range(MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    BOUNCER_URL,
                    params={"email": email},
                    headers={"x-api-key": api_key},
                )
        except Exception as e:
            last_reason = f"exception: {e}"
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(2 ** attempt)
            continue

        if response.status_code == 402:
            raise BouncerCreditsError(
                "Bouncer returned 402 Payment Required — account is out of credits."
            )
        if response.status_code == 200:
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
        last_reason = f"http_{response.status_code}"
        if response.status_code not in RETRYABLE_STATUS:
            break
        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(2 ** attempt)

    return {
        "email": email,
        "verified": None,
        "status": "error",
        "reason": last_reason,
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
