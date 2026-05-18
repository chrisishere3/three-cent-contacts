"""
Three Cent Contacts — async waterfall entry point.

Session 1 wires Stage 1 (Perplexity Sonar via OpenRouter) and Stage 6 (Bouncer
SMTP verify) only. Stages 2-5 and 7 land in later sessions; rows that don't
resolve at stage 1 are emitted unverified.

Usage:
    python scripts/run_waterfall.py --input prospects.csv --output contacts.csv
"""

from __future__ import annotations

import asyncio
import csv
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click
from dotenv import load_dotenv

from normalizer import extract_domain
from stages.stage1_sonar import research_company_contacts
from stages.stage6_bouncer import verify_email


OPTIONAL_KEYS = (
    "HUNTER_API_KEY",
    "BRIGHTDATA_SERP_USERNAME",
    "BRIGHTDATA_SERP_PASSWORD",
)

# Each entry is a tuple of acceptable env-var names; the first present wins.
# Bouncer key was renamed from USEBOUNCER_API_KEY → BOUNCER_API_KEY; we accept both.
REQUIRED_KEY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("OPENROUTER_API_KEY",),
    ("BOUNCER_API_KEY", "USEBOUNCER_API_KEY"),
)

OUTPUT_COLUMNS = [
    "company",
    "domain",
    "name",
    "title",
    "email",
    "stage_hit",
    "cost",
    "verified",
    "verification_status",
]

# Cost-per-stage in USD. Flat estimates; per-call accounting lands in session 3.
COST_STAGE1_SONAR = 0.005
COST_STAGE6_BOUNCER = 0.010

TYPICAL_COST_PER_ROW = 0.022
CEILING_COST_PER_ROW = 0.040


def check_env() -> List[str]:
    """Return human-friendly names of required key groups where none are set."""
    missing: List[str] = []
    for group in REQUIRED_KEY_GROUPS:
        if not any(os.getenv(name) for name in group):
            missing.append(" or ".join(group))
    return missing


def read_input(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        raise click.ClickException(f"Input CSV {path} is empty.")
    missing_cols = {"domain", "title"} - set(rows[0].keys())
    if missing_cols:
        raise click.ClickException(
            f"Input CSV is missing required column(s): {', '.join(sorted(missing_cols))}"
        )
    return rows


def estimate_cost(n_rows: int) -> Tuple[float, float]:
    return (n_rows * TYPICAL_COST_PER_ROW, n_rows * CEILING_COST_PER_ROW)


def load_env_files() -> None:
    """~/.env first, then project-local .env (project wins)."""
    load_dotenv(Path.home() / ".env")
    load_dotenv(Path.cwd() / ".env", override=True)


_PLACEHOLDER_NAME_TOKENS = {"unknown", "n/a", "na", "none", "null", ""}


def _clean_name_token(value: Optional[str]) -> str:
    if not value:
        return ""
    cleaned = value.strip()
    if cleaned.lower() in _PLACEHOLDER_NAME_TOKENS:
        return ""
    return cleaned


def _full_name(contact: Dict[str, Any]) -> str:
    first = _clean_name_token(contact.get("first_name"))
    last = _clean_name_token(contact.get("last_name"))
    name = f"{first} {last}".strip()
    if name:
        return name
    return _clean_name_token(contact.get("name"))


# Sonar sometimes returns docs-style placeholders like "[email protected]" or
# "name@example.com" when it can't find the real address. Treat those as misses.
_PLACEHOLDER_EMAILS = {
    "[email protected]",
    "example@example.com",
    "name@example.com",
    "john.doe@example.com",
    "email@example.com",
    "user@example.com",
}
_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "domain.com"}


def _is_placeholder_email(email: str) -> bool:
    lowered = email.strip().lower()
    if lowered in _PLACEHOLDER_EMAILS:
        return True
    if "@" in lowered and lowered.split("@", 1)[1] in _PLACEHOLDER_DOMAINS:
        return True
    return False


def _email_at_domain(email: Optional[str], domain: str) -> bool:
    if not email or not domain:
        return False
    cleaned = email.strip().lower()
    if _is_placeholder_email(cleaned):
        return False
    return cleaned.endswith(f"@{domain.lower()}")


def _empty_row(company: str, domain: str, title: str, cost: float) -> Dict[str, Any]:
    return {
        "company": company,
        "domain": domain,
        "name": "",
        "title": title,
        "email": "",
        "stage_hit": 0,
        "cost": round(cost, 4),
        "verified": "false",
        "verification_status": "no_candidate",
    }


async def process_row(
    row: Dict[str, str],
    include_unverified: bool,
    max_cost_per_row: Optional[float],
) -> Dict[str, Any]:
    domain = extract_domain(website=row.get("domain")) or (row.get("domain") or "").strip().lower()
    company = (row.get("company_name") or domain).strip()
    title = (row.get("title") or "").strip()
    row_cost = 0.0

    # Stage 1 — Sonar.
    candidates = await research_company_contacts(
        company=company, domain=domain, target_role=title or "contact"
    ) or []
    row_cost += COST_STAGE1_SONAR

    domain_matches = [
        c for c in candidates if _email_at_domain(c.get("email"), domain)
    ]

    chosen: Optional[Dict[str, Any]] = None
    verification: Optional[Dict[str, Any]] = None
    for candidate in domain_matches:
        if max_cost_per_row is not None and row_cost + COST_STAGE6_BOUNCER > max_cost_per_row:
            break
        check = await verify_email(candidate["email"])
        row_cost += COST_STAGE6_BOUNCER
        if check.get("verified"):
            chosen = candidate
            verification = check
            break
        # If not deliverable but caller wants risky/unknown, hold onto the first one.
        if include_unverified and chosen is None:
            chosen = candidate
            verification = check

    if chosen and verification:
        return {
            "company": company,
            "domain": domain,
            "name": _full_name(chosen),
            "title": chosen.get("title") or title,
            "email": chosen.get("email", ""),
            "stage_hit": 1,
            "cost": round(row_cost, 4),
            "verified": "true" if verification.get("verified") else "false",
            "verification_status": verification.get("status", "unknown"),
        }

    # Nothing at domain or nothing deliverable.
    if include_unverified and candidates:
        best = candidates[0]
        raw_email = (best.get("email") or "").strip()
        if raw_email and _is_placeholder_email(raw_email):
            raw_email = ""
        if raw_email and not _email_at_domain(raw_email, domain):
            status = "off_domain"
        elif raw_email:
            status = "on_domain_unverified"
        else:
            status = "no_email"
        return {
            "company": company,
            "domain": domain,
            "name": _full_name(best),
            "title": best.get("title") or title,
            "email": raw_email,
            "stage_hit": 1,
            "cost": round(row_cost, 4),
            "verified": "false",
            "verification_status": status,
        }
    return _empty_row(company, domain, title, row_cost)


async def process_rows(
    rows: List[Dict[str, str]],
    output_path: Path,
    include_unverified: bool,
    max_cost_per_row: Optional[float],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for i, row in enumerate(rows, start=1):
            result = await process_row(row, include_unverified, max_cost_per_row)
            writer.writerow(result)
            f.flush()
            status = result["verification_status"]
            email = result["email"] or "(none)"
            click.echo(
                f"[{i}/{len(rows)}] {result['company']} | stage={result['stage_hit']} "
                f"| {email} | {status} | ${result['cost']:.4f}"
            )


@click.command()
@click.option(
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to input CSV. Must contain at minimum 'domain' and 'title' columns.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to write the verified-contacts CSV. Defaults to ./contacts_<today>.csv.",
)
@click.option("--skip-serp", is_flag=True, help="Skip stage 7 (Bright Data SERP fallback).")
@click.option("--dry-run", is_flag=True, help="Process only the first 5 rows.")
@click.option(
    "--max-cost-per-row",
    type=float,
    default=None,
    help=(
        "Abort a row if its cumulative cost would exceed this USD value "
        "(e.g. 0.04 = four cents). Other rows continue."
    ),
)
@click.option(
    "--include-unverified",
    is_flag=True,
    help="Emit rows even when Bouncer didn't return 'deliverable'.",
)
def main(
    input_path: Path,
    output_path: Optional[Path],
    skip_serp: bool,
    dry_run: bool,
    max_cost_per_row: Optional[float],
    include_unverified: bool,
) -> None:
    load_env_files()

    missing = check_env()
    if missing:
        click.echo(f"Missing required env vars: {', '.join(missing)}", err=True)
        click.echo("Copy .env.example to ~/.env and fill in your API keys.", err=True)
        sys.exit(1)

    if not os.getenv("HUNTER_API_KEY"):
        click.echo(
            "Note: HUNTER_API_KEY not set — stage 4 (Hunter pattern fallback) will skip.",
            err=True,
        )
    if not (os.getenv("BRIGHTDATA_SERP_USERNAME") and os.getenv("BRIGHTDATA_SERP_PASSWORD")):
        click.echo(
            "Note: Bright Data SERP proxy creds not set — stage 7 (SERP fallback) will skip.",
            err=True,
        )

    rows = read_input(input_path)
    if dry_run:
        rows = rows[:5]

    typical, ceiling = estimate_cost(len(rows))
    resolved_output = output_path or Path.cwd() / f"contacts_{date.today().isoformat()}.csv"

    click.echo("=" * 60)
    click.echo(f"Three Cent Contacts — {len(rows)} rows")
    click.echo("=" * 60)
    click.echo(f"  Input:     {input_path}")
    click.echo(f"  Output:    {resolved_output}")
    click.echo(f"  Est cost:  ${typical:.2f} typical / ${ceiling:.2f} ceiling")
    click.echo(f"  Skip SERP: {skip_serp} (session 1: stages 2-5 + 7 not yet wired)")
    click.echo(f"  Dry run:   {dry_run}")
    if max_cost_per_row is not None:
        click.echo(f"  Max/row:   ${max_cost_per_row:.4f}")
    click.echo("")

    asyncio.run(
        process_rows(
            rows=rows,
            output_path=resolved_output,
            include_unverified=include_unverified,
            max_cost_per_row=max_cost_per_row,
        )
    )

    click.echo("")
    click.echo(f"Wrote {resolved_output}")


if __name__ == "__main__":
    main()
