---
name: three-cent-contacts
description: Build a verified-email contact list from a CSV of companies + titles, at roughly $0.03 per verified contact. Runs a cheapest-first waterfall (Perplexity Sonar → site scrape → pattern detect → Hunter pattern → apply pattern → Bouncer verify → Bright Data SERP fallback). Use when the user wants to find work emails for B2B prospecting, build a cold outreach list, enrich a company list with contacts, or replace tools like Apollo, Hunter, or Clay for a one-off list build. Also use when the user mentions "find emails," "contact enrichment," "build a prospect list," "cheap email finder," "lead enrichment waterfall," or "cold email list."
metadata:
  version: 0.1.0
---

# Three Cent Contacts

A cheapest-first waterfall for finding verified B2B work emails. Each row runs through stages until it hits a working email; the expensive stages only fire for the hard targets.

Built by Chris Berkley · [chris-as-is.com](https://chris-as-is.com?ref=three-cent-contacts)

## When to invoke

The user wants to turn a list of companies + target titles into a list of verified work emails, cheaply. They have (or can produce) a CSV. They want to spend cents, not dollars.

Not for: phone numbers, personal emails, B2C consumer data, list buying.

## Required setup

Before running, check that these environment variables exist in `~/.env`:

**Required (skill will not run without these):**
- `OPENROUTER_API_KEY` — for Perplexity Sonar (stage 1) and Gemini Flash (stage 7 parsing)
- `BOUNCER_API_KEY` — for SMTP verification (stage 6)

**Optional (skill runs with degraded coverage if missing):**
- `HUNTER_API_KEY` — for domain pattern lookups (stage 4). Without it, stage 4 silently skips and any row that needed pattern fallback returns unresolved.
- `BRIGHTDATA_SERP_USERNAME` and `BRIGHTDATA_SERP_PASSWORD` — proxy credentials (not a single API key) for stage 7 SERP fallback. Both must be set together. Without them, stage 7 silently skips and hard targets stay unresolved.

If a required key is missing, abort with a clear error and tell the user where to get it (point them at `.env.example`). For optional keys, warn but continue.

## Inputs

The user provides:

- **Input CSV path** (required). Must contain at minimum two columns:
  - `domain` — company website domain (e.g. `acme-pm.com`)
  - `title` — target job title or seniority (e.g. `VP of Operations`, `CEO`)
  - Optional: `company_name`, `linkedin_url`, anything else (passed through)
- **Output CSV path** (optional, default: `./contacts_<YYYY-MM-DD>.csv`)
- **Flags:**
  - `--skip-serp` — skip the Bright Data fallback (stage 7)
  - `--dry-run` — run the first 5 rows only, report what would happen
  - `--max-cost-per-row N` — abort row if cumulative cost on that row exceeds N USD (e.g. `0.04` = four cents). See `references/cost-math.md`.

## How to run

1. **Read the input CSV.** Count rows. Estimate ceiling cost (worst case, every row hits every stage) at `rows × $0.04`. See `references/cost-math.md` for the full breakdown.
2. **If >= 50 rows, confirm before proceeding.** Quote the ceiling and the typical (`rows × $0.022`). Wait for go.
3. **If < 50 rows, just proceed** but report total cost at the end.
4. **Run `scripts/run_waterfall.py`** with the input/output paths and any flags.
5. **Stream stage hits to the user** as they happen ("row 47/412: stage 1 hit, found Sarah Kenner skenner@acme-pm.com, $0.005").
6. **At completion, report:**
   - Total contacts found
   - Verification rate (% deliverable)
   - Total cost
   - Average cost per verified contact
   - Stage distribution (how many hit each stage)

## How the waterfall works

| # | Stage | Provider | Cost | What it does |
|---|-------|----------|------|--------------|
| 1 | AI search | Perplexity Sonar (via OpenRouter) | $0.005 | Asks "who at <domain> has the title <title>?" Often returns name + a real email. |
| 2 | Site scrape | httpx direct | $0 | If stage 1 missed, scrape /about, /team, /leadership, /contact for names/emails. |
| 3 | Pattern detect | Read off an existing email | $0 | If we have any real email at the domain, infer pattern (`first.last`, `flast`, `first`, etc.) |
| 4 | Pattern fetch | Hunter pattern API | $0.01 | If we have names but no real email, ask Hunter for the domain's pattern. Skipped if `HUNTER_API_KEY` not set. |
| 5 | Apply pattern | Local | $0 | Apply the pattern to each name we have. |
| 6 | Verify | Bouncer SMTP | $0.01 | Per generated email, SMTP-check. Drop anything not `deliverable`. |
| 7 | Fallback | Bright Data SERP + Gemini Flash | ~$0.02 | If we still have no names, scrape Google for "{title} at {company}" and parse with Gemini. Skipped if Bright Data proxy creds not set. |

Most rows clear at stage 1 or 2. Stage 7 is for the long tail.

## Output

CSV with these columns:

- `company` / `domain` (preserved from input)
- `name` (full name)
- `title`
- `email`
- `stage_hit` (1-7)
- `cost` (USD)
- `verified` (true if Bouncer returned `deliverable`)
- `verification_status` (Bouncer raw status)

Only `verified=true` rows are emitted by default. To include unverified, pass `--include-unverified`.

## Things to watch for

**Domain drift.** Stage 7 fallback can return emails on unrelated domains ("john@gmail.com" instead of "john@acme-pm.com"). Always post-filter by domain match before writing the output row. The code handles this — but if you ever extend the waterfall, preserve that filter.

**Pattern cache.** Hunter charges per lookup. Cache patterns per domain on disk (`~/.three-cent-contacts/pattern_cache.json`) with a 30-day TTL so we never pay twice for the same domain within a month.

**Sonar request fees.** Sonar's token cost is tiny; the per-request **search fee** is what adds up. Stage 1 is $0.005 because of the search, not the tokens. Don't accidentally retry stage 1 in a loop.

**Bouncer credits.** Verify the user has credits before a large batch. Mid-batch credit exhaustion silently fails.

## References

- `references/waterfall-design.md` — why each stage is ordered the way it is
- `references/email-patterns.md` — supported pattern strings and detection logic
- `references/cost-math.md` — how the per-contact estimate is computed

## Related

- Project page + sample output: [chris-as-is.com/projects/three-cent-contacts](https://chris-as-is.com/projects/three-cent-contacts?ref=three-cent-contacts)
- Want this run for you instead of running it yourself? DM [@chris_as_is on X](https://x.com/chris_as_is).
