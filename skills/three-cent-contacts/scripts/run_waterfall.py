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

Rows are processed concurrently (default 5 workers; --concurrency). Results
append to the output CSV as they finish, so a crashed or aborted run can pick
up where it left off with --resume.

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
from stages.stage6_bouncer import BouncerCreditsError, verify_email
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

DEFAULT_CONCURRENCY = 5


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


# Only these keys are ever read from the user's .env files. We do not load the
# rest of ~/.env into the process, so unrelated secrets the user keeps there
# (HubSpot, AWS, whatever) never end up in our environment.
SKILL_ENV_KEYS = (
    "OPENROUTER_API_KEY",
    "BOUNCER_API_KEY",
    "USEBOUNCER_API_KEY",  # legacy alias for BOUNCER_API_KEY
    "HUNTER_API_KEY",
    "BRIGHTDATA_SERP_USERNAME",
    "BRIGHTDATA_SERP_PASSWORD",
)


def _scoped_env_read(path: Path) -> Dict[str, str]:
    """Parse a .env file and return only the keys this skill recognizes."""
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if key not in SKILL_ENV_KEYS:
                continue
            value = value.strip().strip('"').strip("'")
            out[key] = value
    except OSError:
        return out
    return out


def load_env_files() -> None:
    """
    Load only the keys this skill needs from ~/.env then project-local .env
    (project wins). We do NOT use dotenv's load_dotenv() because it pulls the
    entire file into os.environ — that's a footgun for users who keep
    unrelated secrets in ~/.env.
    """
    for source in (Path.home() / ".env", Path.cwd() / ".env"):
        scoped = _scoped_env_read(source)
        for key, value in scoped.items():
            # Project-local .env (loaded second) overrides ~/.env.
            os.environ[key] = value


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
    """True for exact-domain and subdomain matches (john@mail.acme.com)."""
    if not email or not domain:
        return False
    cleaned = email.strip().lower()
    if _is_placeholder_email(cleaned):
        return False
    email_domain = cleaned.rpartition("@")[2]
    target = domain.lower()
    return email_domain == target or email_domain.endswith(f".{target}")


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


def _row_domain(row: Dict[str, str]) -> str:
    domain_raw = (row.get("domain") or "").strip()
    return extract_domain(website=domain_raw) or domain_raw.lower()


# Per-domain locks so concurrent rows for the same domain don't both pay
# Hunter for a pattern the other is already fetching.
_domain_locks: Dict[str, asyncio.Lock] = {}


def _domain_lock(domain: str) -> asyncio.Lock:
    return _domain_locks.setdefault(domain, asyncio.Lock())


# --------------------------- waterfall ---------------------------


async def _verify_first_domain_hit(
    candidates: List[Dict[str, Any]],
    domain: str,
    *,
    budget_remaining: Optional[float],
    on_bouncer_call: Any,
) -> Tuple[
    Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Tuple[Dict[str, Any], Dict[str, Any]]]
]:
    """
    Walk candidates and Bouncer-verify the first one whose email is at domain.
    Returns (verified_candidate, verification_result, first_risky) where
    first_risky is a (candidate, check) pair for the first catch-all/'risky'
    result seen, so the caller can surface it if nothing fully verifies.
    Calls on_bouncer_call() each time we spend a Bouncer credit.
    """
    first_risky: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None
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
            return candidate, check, first_risky
        if check.get("risky") and first_risky is None:
            first_risky = (candidate, check)
    return None, None, first_risky


def _emit_contact(
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
        "stage_hit": best.get("_stage", 0),
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
    include_risky: bool,
    max_cost_per_row: Optional[float],
    skip_serp: bool,
) -> Dict[str, Any]:
    domain = _row_domain(row)
    company = (row.get("company_name") or domain).strip()
    title = (row.get("title") or "").strip()

    row_cost = 0.0
    # First 'risky' (catch-all domain) result seen anywhere in the row, as
    # (candidate, check, stage). Real addresses on catch-all domains come back
    # 'risky', so this is worth surfacing when nothing fully verifies.
    risky_hit: Optional[Tuple[Dict[str, Any], Dict[str, Any], int]] = None

    def budget_left() -> Optional[float]:
        if max_cost_per_row is None:
            return None
        return max(0.0, max_cost_per_row - row_cost)

    def on_bouncer():
        nonlocal row_cost
        row_cost += COST_STAGE6_BOUNCER

    def note_risky(risky: Optional[Tuple[Dict[str, Any], Dict[str, Any]]], stage: int):
        nonlocal risky_hit
        if risky and risky_hit is None:
            risky_hit = (risky[0], risky[1], stage)

    def tag(contacts: List[Dict[str, Any]], stage: int) -> List[Dict[str, Any]]:
        for c in contacts:
            c.setdefault("_stage", stage)
        return contacts

    candidates: List[Dict[str, Any]] = []

    # ---- Stage 1: Sonar ----
    sonar = await research_company_contacts(
        company=company, domain=domain, target_role=title or "contact"
    )
    row_cost += COST_STAGE1_SONAR
    if sonar:
        candidates.extend(tag(sonar, 1))

    chosen, check, risky = await _verify_first_domain_hit(
        candidates, domain, budget_remaining=budget_left(), on_bouncer_call=on_bouncer
    )
    note_risky(risky, 1)
    if chosen and check:
        return _emit_contact(
            candidate=chosen, check=check, company=company, domain=domain,
            title=title, stage_hit=1, row_cost=row_cost,
        )

    # ---- Stage 2: site scrape ----
    if max_cost_per_row is None or row_cost + COST_STAGE2_SCRAPE_AI < max_cost_per_row:
        scraped = await extract_contacts_from_website(domain=domain, target_role=title or "contact")
        row_cost += COST_STAGE2_SCRAPE_AI
        if scraped:
            candidates.extend(tag(scraped, 2))
            candidates = _dedupe_candidates(candidates)
            chosen, check, risky = await _verify_first_domain_hit(
                candidates, domain, budget_remaining=budget_left(), on_bouncer_call=on_bouncer
            )
            note_risky(risky, 2)
            if chosen and check:
                return _emit_contact(
                    candidate=chosen, check=check, company=company, domain=domain,
                    title=title, stage_hit=2, row_cost=row_cost,
                )

    # ---- Stage 3-5: pattern detect / fetch / apply ----
    # Per-domain lock so concurrent rows for the same domain share one pattern
    # lookup instead of both paying Hunter.
    async with _domain_lock(domain):
        pattern = await get_cached_pattern(domain)

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

    # Stage 5 — generate emails from pattern, verify each until one passes.
    if pattern and pattern.get("pattern"):
        generated: List[Dict[str, Any]] = []
        seen_emails: set[str] = {
            (c.get("email") or "").strip().lower() for c in candidates if c.get("email")
        }
        for c in candidates:
            if _email_at_domain(c.get("email"), domain):
                continue  # Already has a corporate email; it just failed verify.
            first, last = _candidate_first_last(c)
            if not (first and last):
                continue
            new_email = generate_email(first, last, domain, pattern["pattern"])
            if not new_email or new_email.lower() in seen_emails:
                continue
            seen_emails.add(new_email.lower())
            generated.append(
                {**c, "email": new_email, "_stage": 5, "source": f"pattern:{pattern['pattern']}"}
            )

        for candidate in generated:
            if max_cost_per_row is not None and row_cost + COST_STAGE6_BOUNCER > max_cost_per_row:
                break
            check = await verify_email(candidate["email"])
            on_bouncer()
            await update_pattern_stats(domain, bool(check.get("verified")))
            if check.get("verified"):
                return _emit_contact(
                    candidate=candidate, check=check, company=company, domain=domain,
                    title=title, stage_hit=5, row_cost=row_cost,
                )
            if check.get("risky"):
                note_risky((candidate, check), 5)
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
            candidates.extend(tag(serp_candidates, 7))
            candidates = _dedupe_candidates(candidates)
            chosen, check, risky = await _verify_first_domain_hit(
                serp_candidates, domain, budget_remaining=budget_left(), on_bouncer_call=on_bouncer
            )
            note_risky(risky, 7)
            if chosen and check:
                return _emit_contact(
                    candidate=chosen, check=check, company=company, domain=domain,
                    title=title, stage_hit=7, row_cost=row_cost,
                )

    # ---- Nothing fully verified. ----
    # A 'risky' hit is a real address on a catch-all domain — with
    # --include-risky, emit it as the row's result (verified stays false).
    if include_risky and risky_hit:
        candidate, check, stage = risky_hit
        return _emit_contact(
            candidate=candidate, check=check, company=company, domain=domain,
            title=title, stage_hit=stage, row_cost=row_cost,
        )
    if include_unverified and candidates:
        return _emit_unverified_best(
            candidates=candidates, company=company, domain=domain,
            title=title, row_cost=row_cost,
        )
    return _emit_empty(company, domain, title, row_cost)


async def process_rows(
    rows: List[Dict[str, str]],
    output_path: Path,
    include_unverified: bool,
    include_risky: bool,
    max_cost_per_row: Optional[float],
    skip_serp: bool,
    concurrency: int,
    append: bool,
) -> Dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    totals: Dict[str, Any] = {
        "rows": 0, "verified": 0, "risky": 0, "cost": 0.0, "stages": {}, "aborted": False,
    }
    sem = asyncio.Semaphore(max(1, concurrency))
    write_lock = asyncio.Lock()
    abort = asyncio.Event()
    done_count = 0

    mode = "a" if append and output_path.exists() and output_path.stat().st_size > 0 else "w"
    with output_path.open(mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        if mode == "w":
            writer.writeheader()

        async def worker(row: Dict[str, str]) -> None:
            nonlocal done_count
            async with sem:
                if abort.is_set():
                    return
                try:
                    result = await process_row(
                        row,
                        include_unverified=include_unverified,
                        include_risky=include_risky,
                        max_cost_per_row=max_cost_per_row,
                        skip_serp=skip_serp,
                    )
                except BouncerCreditsError:
                    abort.set()
                    return
            async with write_lock:
                writer.writerow(result)
                f.flush()
                done_count += 1
                totals["rows"] += 1
                totals["cost"] += float(result.get("cost") or 0)
                if result.get("verified") == "true":
                    totals["verified"] += 1
                if result.get("verification_status") == "risky":
                    totals["risky"] += 1
                stage = result.get("stage_hit", 0)
                totals["stages"][stage] = totals["stages"].get(stage, 0) + 1
                email = result["email"] or "(none)"
                click.echo(
                    f"[{done_count}/{len(rows)}] {result['company']} | stage={result['stage_hit']} "
                    f"| {email} | {result['verification_status']} | ${result['cost']:.4f}"
                )

        await asyncio.gather(*(worker(row) for row in rows))

    totals["aborted"] = abort.is_set()
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
@click.option(
    "--include-risky",
    is_flag=True,
    help=(
        "Emit 'risky' results (catch-all domains — usually real addresses "
        "Bouncer can't SMTP-confirm) as row results, marked verified=false."
    ),
)
@click.option(
    "--concurrency",
    type=int,
    default=DEFAULT_CONCURRENCY,
    show_default=True,
    help="How many rows to process in parallel.",
)
@click.option(
    "--resume",
    is_flag=True,
    help=(
        "Skip input rows whose domain already appears in the output CSV and "
        "append new results to it. Use after a crash or credit-exhaustion abort."
    ),
)
def main(
    input_path: Path,
    output_path: Optional[Path],
    skip_serp: bool,
    dry_run: bool,
    max_cost_per_row: Optional[float],
    include_unverified: bool,
    include_risky: bool,
    concurrency: int,
    resume: bool,
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
    resolved_output = output_path or Path.cwd() / f"contacts_{date.today().isoformat()}.csv"

    if resume and resolved_output.exists():
        with resolved_output.open(newline="", encoding="utf-8") as f:
            done_domains = {
                (r.get("domain") or "").strip().lower() for r in csv.DictReader(f)
            }
        before = len(rows)
        rows = [r for r in rows if _row_domain(r) not in done_domains]
        click.echo(f"Resume: {before - len(rows)} rows already in output, {len(rows)} to go.")

    if dry_run:
        rows = rows[:5]

    if not rows:
        click.echo("Nothing to do.")
        return

    typical, ceiling = estimate_cost(len(rows))

    click.echo("=" * 60)
    click.echo(f"Three Cent Contacts — {len(rows)} rows")
    click.echo("=" * 60)
    click.echo(f"  Input:       {input_path}")
    click.echo(f"  Output:      {resolved_output}")
    click.echo(f"  Est cost:    ${typical:.2f} typical / ${ceiling:.2f} ceiling")
    click.echo(f"  Concurrency: {concurrency}")
    click.echo(f"  Skip SERP:   {skip_serp}")
    click.echo(f"  Dry run:     {dry_run}")
    if max_cost_per_row is not None:
        click.echo(f"  Max/row:     ${max_cost_per_row:.4f}")
    click.echo("")

    totals = asyncio.run(
        process_rows(
            rows=rows,
            output_path=resolved_output,
            include_unverified=include_unverified,
            include_risky=include_risky,
            max_cost_per_row=max_cost_per_row,
            skip_serp=skip_serp,
            concurrency=concurrency,
            append=resume,
        )
    )

    click.echo("")
    click.echo("=" * 60)
    click.echo(f"Done. {totals['verified']}/{totals['rows']} verified · "
               f"{totals['risky']} risky (catch-all) · "
               f"total ${totals['cost']:.4f} · "
               f"avg ${totals['cost'] / max(totals['rows'], 1):.4f}/row")
    if totals["stages"]:
        dist = ", ".join(f"stage {s}: {n}" for s, n in sorted(totals["stages"].items()))
        click.echo(f"Stage distribution: {dist}")
    click.echo(f"Wrote {resolved_output}")

    if totals["aborted"]:
        click.echo("", err=True)
        click.echo(
            "ABORTED: Bouncer is out of credits. "
            f"{totals['rows']} rows finished and are saved. "
            "Top up at usebouncer.com, then re-run the same command with --resume.",
            err=True,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
