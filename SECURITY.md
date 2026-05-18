# Security policy

Three Cent Contacts is a small open-source tool. If you find a security issue, this is how to tell me about it and what I commit to in return.

## Reporting a vulnerability

Email **chris@chris-as-is.com** with the subject line `SECURITY: three-cent-contacts`. Please don't open a public GitHub issue for security problems — give me a chance to fix them first.

What to include:
- What the issue is
- How to reproduce it
- Which versions are affected (if you know)
- Your assessment of severity, if you have one

I'll acknowledge your email within 3 business days. If the issue is real, I'll publish a fix and credit you in the release notes (unless you'd rather stay anonymous).

## What this tool does and does not do with your data

**Stays on your machine:**
- Your API keys. We read **only** the five keys this skill recognizes from `~/.env` and project-local `.env` — `OPENROUTER_API_KEY`, `BOUNCER_API_KEY` (or legacy `USEBOUNCER_API_KEY`), `HUNTER_API_KEY`, `BRIGHTDATA_SERP_USERNAME`, `BRIGHTDATA_SERP_PASSWORD`. Anything else in your `.env` (HubSpot, AWS, GitHub tokens, whatever) is **not** read into our process. Keys we do read are never logged or sent anywhere except the third-party APIs you've authorized.
- Your input CSV
- Your output CSV
- The pattern cache at `~/.three-cent-contacts/pattern_cache.json`

**Sent to third parties (only those you've enabled via API keys):**
- OpenRouter — receives company + title strings for Perplexity Sonar searches (stage 1) and scraped website text for Gemini parsing (stages 2, 7). Subject to OpenRouter's privacy policy.
- Bouncer — receives email addresses for SMTP verification (stage 6). Subject to Bouncer's privacy policy.
- Hunter (optional) — receives domain strings for pattern lookups (stage 4). Subject to Hunter's privacy policy.
- Bright Data (optional) — proxies Google search queries (stage 7). Subject to Bright Data's privacy policy.

**Never sent anywhere:**
- Your API keys (each goes only to the service it authenticates)
- The input CSV file itself (we read it locally and send only relevant fields per row)
- The output CSV

## Things we don't do

- No telemetry. No analytics. No "phone home."
- No third-party tracking or fingerprinting in the script.
- No auto-update mechanism that runs new code without you re-installing.
- No background processes or daemons.

## Things to be aware of

- The Bright Data stage uses `verify=False` on the proxy HTTPS connection. This is required by Bright Data's proxy infrastructure and disables TLS verification *for that proxy hop only*. The downstream Google requests still go through TLS via the proxy. If this matters to your threat model, omit `BRIGHTDATA_SERP_USERNAME` / `BRIGHTDATA_SERP_PASSWORD` and stage 7 will silently skip.
- Verified contact emails are written to your output CSV in plaintext. Don't commit the output CSV to a public repo.

## Security scan results

When you install via `npx skills add`, the CLI runs Socket and Snyk scans on the repo. Current findings:

- **Snyk: 1 medium-severity issue** — SNYK-PYTHON-ZIPP-7430899 (infinite loop in `zipp` < 3.19.1). This is a transitive dependency of `click`. Fixed in v0.2.1 by pinning `click>=8.2.0`, which pulls in patched versions of `importlib-metadata` and `zipp`. Not exploitable in this skill's usage (we don't pass adversarial input through zipp paths).
- **Socket: 1 alert** — typical "new repository / no security policy" reputational flag. This file is part of fixing that.
- **Gen: Safe.**

If you see new or different alerts after a future scan, please email me at the address above.
