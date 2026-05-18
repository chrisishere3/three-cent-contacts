---
name: three-cent-contacts
description: Build a verified-email contact list from a CSV of companies + titles, at roughly $0.03 per verified contact. Runs a cheapest-first waterfall (Perplexity Sonar → site scrape → pattern detect → Hunter pattern → apply pattern → Bouncer verify → Bright Data SERP fallback). Use when the user wants to find work emails for B2B prospecting, build a cold outreach list, enrich a company list with contacts, or replace tools like Apollo, Hunter, or Clay for a one-off list build. Also use when the user mentions "find emails," "contact enrichment," "build a prospect list," "cheap email finder," "lead enrichment waterfall," or "cold email list."
metadata:
  version: 0.2.1
---

# Three Cent Contacts

A cheapest-first waterfall for finding verified B2B work emails. Each row runs through stages until it hits a working email; the expensive stages only fire for the hard targets.

Built by Chris Berkley · [chris-as-is.com](https://chris-as-is.com?ref=three-cent-contacts)

## When to invoke

The user wants to turn a list of companies + target titles into a list of verified work emails, cheaply. They want to spend cents, not dollars.

Not for: phone numbers, personal emails, B2C consumer data, list buying.

## First message to the user

When the user invokes this skill for the first time in a conversation, lead with this opening line **exactly** (it's been wordsmithed to remove friction):

> Tell me which companies you want and what title to find at each. Paste a list, drop a CSV, or just type a few. I'll handle the rest.

Then silently check `~/.env` for `OPENROUTER_API_KEY` and `BOUNCER_API_KEY` while waiting for their reply. If either is missing, deliver the "Required setup" message below in your NEXT turn (after they've replied) — don't lead with setup, lead with the friendly opener so the conversation starts on momentum.

## Required setup

Two API keys are required. Check `~/.env` for both before running:
- `OPENROUTER_API_KEY` — Perplexity Sonar (stage 1) + Gemini Flash (stages 2, 7)
- `BOUNCER_API_KEY` — SMTP verification (stage 6)

If either is missing, tell the user **exactly this**:

> Quick setup before I can run: add your OpenRouter and Bouncer keys to `~/.env`. Both are free signup, ~3 min. Hunter and Bright Data are optional if you've got them.
>
> - OpenRouter — https://openrouter.ai → API Keys → create one (starts with `sk-or-v1-`)
> - Bouncer — https://usebouncer.com → API Access → create one
>
> Then add them to `~/.env`:
> ```
> OPENROUTER_API_KEY=sk-or-v1-your-key
> BOUNCER_API_KEY=your-bouncer-key
> ```
> Ping me when they're in and I'll pick up where we left off.

Two more keys are **optional** (waterfall runs without them, with thinner coverage):
- `HUNTER_API_KEY` — unlocks stage 4 (pattern fallback when names are known but no email)
- `BRIGHTDATA_SERP_USERNAME` + `BRIGHTDATA_SERP_PASSWORD` — unlocks stage 7 (Google fallback for hard targets)

For optional keys, don't block; just mention "Note: Hunter/Bright Data not set, optional stages will skip" once and move on.

## Python dependencies

The skill ships Python code that needs three packages: `httpx`, `python-dotenv`, `click`. On first run, check whether they import. If `python3 -c "import httpx, dotenv, click"` fails, run `python3 -m pip install --user httpx python-dotenv click` once (silently, unless it errors). Then proceed.

## Finding the script

The waterfall code lives at `scripts/run_waterfall.py` **relative to this SKILL.md file**. When the skill is installed via `npx skills add`, that resolves to roughly `~/.agents/skills/three-cent-contacts/scripts/run_waterfall.py`. Always invoke the script using its full path resolved from the SKILL.md location, not a bare `scripts/run_waterfall.py` (which would look in the user's cwd and fail).

## Inputs

The user can hand you the list in any of these shapes — convert whatever they give you into the CSV the script needs:

- **Typed in chat:** "VPs of Engineering at Stripe, Plaid, Vercel." → build the CSV yourself with columns `domain,title,company_name`.
- **Pasted list:** company names, domains, URLs, or a Google Sheet excerpt. Ask for the target title(s) if they didn't include one.
- **CSV path:** must have `domain` and `title` columns. `company_name` is optional but improves stage 1 hit rate. Extra columns pass through.

The script needs:
- **Input CSV path** with at minimum `domain` and `title` columns
- **Output CSV path** (optional, default: `./contacts_<YYYY-MM-DD>.csv`)
- **Flags:**
  - `--skip-serp` — skip stage 7 (Bright Data SERP fallback)
  - `--dry-run` — process only the first 5 rows
  - `--max-cost-per-row N` — abort a row that exceeds N USD (e.g. `0.04`)
  - `--include-unverified` — emit rows even when Bouncer didn't return `deliverable`

When you build the CSV from typed input, write it to a temp file and pass that path to the script. Don't make the user write the CSV themselves unless they want to.

## How to run

1. **Read the input CSV.** Count rows. Estimate ceiling cost (worst case, every row hits every stage) at `rows × $0.04`. See `references/cost-math.md` for the full breakdown.
2. **If >= 50 rows, confirm before proceeding.** Quote the ceiling and the typical (`rows × $0.022`). Wait for go.
3. **If < 50 rows, just proceed** but report total cost at the end.
4. **Run the waterfall script** with `python3 <skill-dir>/scripts/run_waterfall.py --input <csv> --output <csv> [flags]`. Resolve `<skill-dir>` from this SKILL.md's location, not the user's cwd.
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
