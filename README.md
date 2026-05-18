# Three Cent Contacts

A Claude Code skill that finds verified B2B work emails at roughly $0.03 per contact. It runs a cheapest-first waterfall through Perplexity Sonar, free site scrapes, Hunter pattern detection, and Bouncer SMTP verification. Most contacts hit the cheap stages and never reach the expensive ones.

Built by [Chris Berkley](https://chris-as-is.com?ref=three-cent-contacts). Need it run for you on your own list? DM [@chris_as_is on X](https://x.com/chris_as_is).

## What it does

Input: a CSV of companies and target job titles.
Output: a CSV of verified work emails, with the cost and stage each one hit.

Most rows clear at stage 1 (Perplexity Sonar, half a cent) or stage 2 (free site scrape). A few fall to Hunter for the pattern. The hard targets drop into a SERP fallback.

## Install

The recommended path is the [`npx skills`](https://github.com/vercel-labs/skills) CLI:

```bash
npx skills add chrisishere3/three-cent-contacts
```

That installs the skill into your local Claude Code (and any other supported agent) by symlinking it into the right directory.

Or clone the repo and read the code:

```bash
git clone https://github.com/chrisishere3/three-cent-contacts.git
```

The repo also ships a Claude Code plugin manifest (`.claude-plugin/`), so `/plugin marketplace add chrisishere3/three-cent-contacts` followed by `/plugin install three-cent-contacts` should work — that path is unverified end-to-end at v0.1. If you hit a snag with the plugin install, fall back to `npx skills add` or a manual symlink and open an issue.

## Setup

You need API keys for two services to run anything. Two more keys unlock optional stages.

| Service | Required? | What for | Free tier? |
|---------|-----------|----------|------------|
| [OpenRouter](https://openrouter.ai) | Yes | Perplexity Sonar (stage 1) + Gemini Flash for SERP parsing (stage 7) | Pay-as-you-go, ~$0.005/call |
| [Bouncer](https://usebouncer.com) | Yes | SMTP verification (stage 6) | Trial credits on signup |
| [Hunter](https://hunter.io) | Optional | Domain pattern fallback (stage 4). Without it, stage 4 skips. | 25 lookups/mo free |
| [Bright Data SERP](https://brightdata.com) | Optional | Google scrape for hard targets (stage 7). Without it, stage 7 skips. | Pay-as-you-go |

Copy `.env.example` to `~/.env` (or the project root) and fill in:

```env
OPENROUTER_API_KEY=sk-or-v1-...
BOUNCER_API_KEY=...
HUNTER_API_KEY=...                  # optional but recommended
BRIGHTDATA_SERP_USERNAME=...        # optional, both needed if you want stage 7
BRIGHTDATA_SERP_PASSWORD=...
```

**Note on Bright Data:** the SERP API uses proxy credentials (a username/password pair), not a single API key. Set up a "SERP API" zone in your Bright Data dashboard to get them. The username looks like `brd-customer-hl_XXXXX-zone-serp_api1`.

## Usage

Once installed, just ask Claude Code:

```
Find contacts for the companies in ~/Downloads/prospects.csv,
target titles VP Operations and Director of Leasing.
```

The skill handles cost estimation, runs the waterfall, and writes a verified CSV.

Or run the script directly:

```bash
python scripts/run_waterfall.py \
  --input ~/Downloads/prospects.csv \
  --output ~/Downloads/contacts.csv
```

## Input format

CSV with these columns:

| Column | Required | Notes |
|--------|----------|-------|
| `domain` | Yes | `acme-pm.com` (no `https://`) |
| `title` | Yes | `VP of Operations` |
| `company_name` | No | Passed through to output |
| anything else | No | Passed through |

See `examples/input_sample.csv`.

## Output format

CSV with these columns:

| Column | Notes |
|--------|-------|
| `company` / `domain` | Preserved from input |
| `name` | Full name |
| `title` | |
| `email` | The verified work email |
| `stage_hit` | 1 through 7 (see waterfall below) |
| `cost` | USD spent finding this one |
| `verified` | `true` if Bouncer returned `deliverable` |
| `verification_status` | Raw Bouncer status |

See `examples/output_sample.csv`.

## The waterfall

| # | Stage | Provider | Cost |
|---|-------|----------|------|
| 1 | AI search | Perplexity Sonar (OpenRouter) | $0.005 |
| 2 | Site scrape | httpx direct request | $0 |
| 3 | Pattern detect | Read off existing email | $0 |
| 4 | Pattern fetch | Hunter Pattern API | $0.01 |
| 5 | Apply pattern | Local | $0 |
| 6 | Verify | Bouncer SMTP | $0.01 |
| 7 | Hard-target fallback | Bright Data SERP + Gemini Flash | ~$0.02 |

Most rows total $0.015 ($0.005 sonar + $0.01 verify). Hard rows can climb to $0.04+. Average across realistic batches: ~$0.03 per verified contact.

## What it's not

- Not a phone number finder. Phones are expensive.
- Not a Clay replacement. Clay does a lot more.
- Not a consumer data source. B2B work emails only.
- Not gated. No email signup, no freemium tier, no "request access."

It's a lean, public tool for bootstrappers and lean teams who want a contact list without paying Apollo or Clay rates.

## Contributing

PRs and issues welcome. Especially appreciated:

- Additional pattern detection rules
- Alternative verification providers (MillionVerifier is cheaper than Bouncer)
- Cost optimizations

## License

[MIT](LICENSE). Use it however you want.

## Related

- Project page with run log + sample output: [chris-as-is.com/projects/cheap-email-finder.html](https://chris-as-is.com/projects/cheap-email-finder.html?ref=three-cent-contacts)
- More about the build: [chris-as-is.com](https://chris-as-is.com?ref=three-cent-contacts)
