"""
Three Cent Contacts — async waterfall entry point.

Cheapest-first cascade across 7 stages. Each row stops at the first stage that
produces a verified email; stages 4 (Hunter) and 7 (Bright Data SERP) silently
skip if their credentials are absent.

  Stage 1  Perplexity Sonar       (OpenRouter)   ~$0.005
  Stage 2  Site scrape + Gemini   (httpx + OR)   ~$0.000 (+~$0.001 AI)
  Stage 3  Detect pattern         (local)        $0
  Stage 4  Hunter domain-search   (Hunter)       ~$0.010 (optional)
  Stage 5  Apply pattern          (local)        $0
  Stage 6  Bouncer SMTP verify    (Bouncer)      ~$0.010
  Stage 7  Bright Data SERP       (BD + OR)      ~$0.015-0.025 (optional)

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

from email_patterns import (
    detect_pattern,
    generate_email,
    get_cached_pattern,
    get_pattern_from_hunter,
    save_pattern,
    update_pattern_stats,
)
from normalizer import extract_domain
from stages.stage1_sonar import research_company_contacts
from stages.stage2_site_scrape import extract_contacts_from_website
from stages.stage6_bouncer import verify_email
from stages.stage7_serp import serp_lookup


OPTIONAL_KEYS = (
    "HUNTER_API_KEY",
    "BRIGHTDATA_SERP_USERNAME",
    "BRIGHTDATA_SERP_PASSWORD",
)

# Each entry is a tuple of acceptable env-var names; the first present wins.
# Bouncer key was renamed from USEBOUNCER_API_KEY → BOUNCER_API_KEY; accept both.
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

# Flat per-call cost estimates in USD. Per-token accounting can come later.
COST_STAGE1_SONAR = 0.005
COST_STAGE2_SCRAPE_AI = 0.001  # Gemini parse; the scrape itself is free.
COST_STAGE4_HUNTER = 0.010
COST_STAGE6_BOUNCER = 0.010
COST_STAGE7_SERP = 0.020  # BD proxy charge + Gemini parse.

TYPICAL_COST_PER_ROW = 0.022
CEILING_COST_PER_ROW = 0.060  # All stages fire, plus a couple of Bouncer checks.


# --------------------------- helpers ---------------------------


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


def _candidate_first_last(
    contact: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    first = _clean_name_token(contact.get("first_name"))
    last = _clean_name_token(contact.get("last_name"))
    if first and last:
        return first, last
    full = _clean_name_token(contact.get("name"))
    if full and " " in full:
        parts = full.split()
        return parts[0], parts[-1]
    return None, None


def _dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for c in candidates:
        key = (
            _clean_name_token(c.get("first_name")).lower(),
            _clean_name_token(c.get("last_name")).lower(),
            (c.get("email") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# --------------------------- waterfall ---------------------------


async def _verify_first_domain_hit(
    candidates: List[Dict[str, Any]],
    domain: str,
    *,
    budget_remaining: Optional[float],
    on_bouncer_call: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Walk candidates and Bouncer-verify the first one whose email is at domain.
    Returns (verified_candidate, verification_result, last_unverified_seen).
    Calls on_bouncer_call() each time we spend a Bouncer credit.
    """
    last_unverified: Optional[Dict[str, Any]] = None
    last_check: Optional[Dict[str, Any]] = None
    for candidate in candidates:
        if not _email_at_domain(candidate.get("email"), domain):
            continue
        if budget_remaining is not None and budget_remaining < COST_STAGE6_BOUNCER:
            break
        check = await verify_email(candidate["email"])
        on_bouncer_call()
        if budget_remaining is not None:
            budget_remaining -= COST_STAGE6_BOUNCER
        if check.get("verified"):
            return candidate, check, None
        last_unverified = candidate
        last_check = check
    return None, None, last_unverified if last_check else None


def _emit_verified(
    *,
    candidate: Dict[str, Any],
    check: Dict[str, Any],
    company: str,
    domain: str,
    title: str,
    stage_hit: int,
    row_cost: float,
) -> Dict[str, Any]:
    return {
        "company": company,
        "domain": domain,
        "name": _full_name(candidate),
        "title": candidate.get("title") or title,
        "email": candidate.get("email", ""),
        "stage_hit": stage_hit,
        "cost": round(row_cost, 4),
        "verified": "true" if check.get("verified") else "false",
        "verification_status": check.get("status", "unknown"),
    }


def _emit_unverified_best(
    *,
    candidates: List[Dict[str, Any]],
    company: str,
    domain: str,
    title: str,
    stage_hit: int,
    row_cost: float,
) -> Dict[str, Any]:
    """Prefer an on-domain candidate over an off-domain one."""
    on_domain = [c for c in candidates if _email_at_domain(c.get("email"), domain)]
    pool = on_domain or candidates
    best = pool[0]
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
        "stage_hit": stage_hit,
        "cost": round(row_cost, 4),
        "verified": "false",
        "verification_status": status,
    }


def _emit_empty(company: str, domain: str, title: str, cost: float) -> Dict[str, Any]:
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
    *,
    include_unverified: bool,
    max_cost_per_row: Optional[float],
    skip_serp: bool,
) -> Dict[str, Any]:
    domain_raw = (row.get("domain") or "").strip()
    domain = extract_domain(website=domain_raw) or domain_raw.lower()
    company = (row.get("company_name") or domain).strip()
    title = (row.get("title") or "").strip()

    row_cost = 0.0

    def budget_left() -> Optional[float]:
        if max_cost_per_row is None:
            return None
        return max(0.0, max_cost_per_row - row_cost)

    def on_bouncer():
        nonlocal row_cost
        row_cost += COST_STAGE6_BOUNCER

    candidates: List[Dict[str, Any]] = []

    # ---- Stage 1: Sonar ----
    sonar = await research_company_contacts(
        company=company, domain=domain, target_role=title or "contact"
    )
    row_cost += COST_STAGE1_SONAR
    if sonar:
        candidates.extend(sonar)

    chosen, check, _ = await _verify_first_domain_hit(
        candidates, domain, budget_remaining=budget_left(), on_bouncer_call=on_bouncer
    )
    if chosen and check:
        return _emit_verified(
            candidate=chosen, check=check, company=company, domain=domain,
            title=title, stage_hit=1, row_cost=row_cost,
        )

    # ---- Stage 2: site scrape ----
    if max_cost_per_row is None or row_cost + COST_STAGE2_SCRAPE_AI < max_cost_per_row:
        scraped = await extract_contacts_from_website(domain=domain, target_role=title or "contact")
        row_cost += COST_STAGE2_SCRAPE_AI
        if scraped:
            candidates.extend(scraped)
            candidates = _dedupe_candidates(candidates)
            chosen, check, _ = await _verify_first_domain_hit(
                candidates, domain, budget_remaining=budget_left(), on_bouncer_call=on_bouncer
            )
            if chosen and check:
                return _emit_verified(
                    candidate=chosen, check=check, company=company, domain=domain,
                    title=title, stage_hit=2, row_cost=row_cost,
                )

    # ---- Stage 3-5: pattern detect / fetch / apply ----
    pattern = await get_cached_pattern(domain)
    pattern_source = "cache" if pattern else None

    # Stage 3 — detect from any on-domain emails we already have.
    if not pattern:
        for c in candidates:
            email = (c.get("email") or "").strip().lower()
            first, last = _candidate_first_last(c)
            if email and first and last and _email_at_domain(email, domain):
                detected = detect_pattern(email, first, last)
                if detected:
                    pattern = {"pattern": detected, "confidence": 0.80, "source": "stage3_detect"}
                    await save_pattern(domain, pattern)
                    pattern_source = "stage3_detect"
                    break

    # Stage 4 — Hunter fallback (only if we have names but still no pattern).
    has_named_candidates = any(_candidate_first_last(c) != (None, None) for c in candidates)
    if (
        not pattern
        and has_named_candidates
        and os.getenv("HUNTER_API_KEY")
        and (max_cost_per_row is None or row_cost + COST_STAGE4_HUNTER < max_cost_per_row)
    ):
        hunter = await get_pattern_from_hunter(domain)
        row_cost += COST_STAGE4_HUNTER
        if hunter:
            pattern = hunter
            await save_pattern(domain, pattern)
            pattern_source = "stage4_hunter"

    # Stage 5 — generate emails from pattern, verify each until one passes.
    if pattern and pattern.get("pattern"):
        generated: List[Dict[str, Any]] = []
        seen_emails: set[str] = {
            (c.get("email") or "").strip().lower() for c in candidates if c.get("email")
        }
        for c in candidates:
            if c.get("email"):
                continue  # Already had an email; no point regenerating it.
            first, last = _candidate_first_last(c)
            if not (first and last):
                continue
            new_email = generate_email(first, last, domain, pattern["pattern"])
            if not new_email or new_email.lower() in seen_emails:
                continue
            seen_emails.add(new_email.lower())
            generated.append({**c, "email": new_email, "source": f"pattern:{pattern['pattern']}"})

        for candidate in generated:
            if max_cost_per_row is not None and row_cost + COST_STAGE6_BOUNCER > max_cost_per_row:
                break
            check = await verify_email(candidate["email"])
            on_bouncer()
            await update_pattern_stats(domain, bool(check.get("verified")))
            if check.get("verified"):
                stage_hit = 5
                return _emit_verified(
                    candidate=candidate, check=check, company=company, domain=domain,
                    title=title, stage_hit=stage_hit, row_cost=row_cost,
                )
        # Even if no generated email verified, keep them as unverified candidates.
        candidates.extend(generated)
        candidates = _dedupe_candidates(candidates)

    # ---- Stage 7: SERP fallback ----
    if (
        not skip_serp
        and os.getenv("BRIGHTDATA_SERP_USERNAME")
        and os.getenv("BRIGHTDATA_SERP_PASSWORD")
        and (max_cost_per_row is None or row_cost + COST_STAGE7_SERP < max_cost_per_row)
    ):
        serp_candidates = await serp_lookup(company=company, domain=domain, title=title or "contact")
        row_cost += COST_STAGE7_SERP
        if serp_candidates:
            candidates.extend(serp_candidates)
            candidates = _dedupe_candidates(candidates)
            chosen, check, _ = await _verify_first_domain_hit(
                serp_candidates, domain, budget_remaining=budget_left(), on_bouncer_call=on_bouncer
            )
            if chosen and check:
                return _emit_verified(
                    candidate=chosen, check=check, company=company, domain=domain,
                    title=title, stage_hit=7, row_cost=row_cost,
                )

    # ---- Nothing verified. Optionally emit best unverified candidate. ----
    if include_unverified and candidates:
        return _emit_unverified_best(
            candidates=candidates, company=company, domain=domain,
            title=title, stage_hit=1, row_cost=row_cost,
        )
    return _emit_empty(company, domain, title, row_cost)


async def process_rows(
    rows: List[Dict[str, str]],
    output_path: Path,
    include_unverified: bool,
    max_cost_per_row: Optional[float],
    skip_serp: bool,
) -> Dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    totals = {"rows": 0, "verified": 0, "cost": 0.0, "stages": {}}
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for i, row in enumerate(rows, start=1):
            result = await process_row(
                row,
                include_unverified=include_unverified,
                max_cost_per_row=max_cost_per_row,
                skip_serp=skip_serp,
            )
            writer.writerow(result)
            f.flush()
            totals["rows"] += 1
            totals["cost"] += float(result.get("cost") or 0)
            if result.get("verified") == "true":
                totals["verified"] += 1
            stage = result.get("stage_hit", 0)
            totals["stages"][stage] = totals["stages"].get(stage, 0) + 1
            status = result["verification_status"]
            email = result["email"] or "(none)"
            click.echo(
                f"[{i}/{len(rows)}] {result['company']} | stage={result['stage_hit']} "
                f"| {email} | {status} | ${result['cost']:.4f}"
            )
    return totals


# --------------------------- CLI ---------------------------


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
    click.echo(f"  Skip SERP: {skip_serp}")
    click.echo(f"  Dry run:   {dry_run}")
    if max_cost_per_row is not None:
        click.echo(f"  Max/row:   ${max_cost_per_row:.4f}")
    click.echo("")

    totals = asyncio.run(
        process_rows(
            rows=rows,
            output_path=resolved_output,
            include_unverified=include_unverified,
            max_cost_per_row=max_cost_per_row,
            skip_serp=skip_serp,
        )
    )

    click.echo("")
    click.echo("=" * 60)
    click.echo(f"Done. {totals['verified']}/{totals['rows']} verified · "
               f"total ${totals['cost']:.4f} · "
               f"avg ${totals['cost'] / max(totals['rows'], 1):.4f}/row")
    if totals["stages"]:
        dist = ", ".join(f"stage {s}: {n}" for s, n in sorted(totals["stages"].items()))
        click.echo(f"Stage distribution: {dist}")
    click.echo(f"Wrote {resolved_output}")


if __name__ == "__main__":
    main()
