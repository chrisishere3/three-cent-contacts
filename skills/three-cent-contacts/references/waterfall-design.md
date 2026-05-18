# Waterfall design

Reference doc for the `three-cent-contacts` skill. Built by Chris Berkley · [chris-as-is.com](https://chris-as-is.com?ref=three-cent-contacts).

## Why this order

The waterfall is ordered by **cost-to-resolve** (cheapest first) and **probability-of-hit** (highest at the cheapest stages). The expensive stages exist only because *some* targets can't be resolved any other way.

In practice, on a typical B2B property-management or SaaS prospect list, the distribution looks like this:

| Stage | % of contacts that resolve here |
|-------|----|
| 1 — Sonar | ~60-70% |
| 2 — Free site scrape | ~10-15% |
| 4 — Hunter pattern | ~10-15% |
| 7 — SERP fallback | ~3-8% |
| Unresolved | ~3-5% |

That distribution is what produces a $0.03 blended average. If the SERP fallback fires for everyone, you're paying $0.04+ per contact — which is still cheap, but it means stages 1 and 2 are failing and something is wrong upstream (bad input, weird industry, dark domains).

## Why stage 1 is Sonar and not a static scrape

Stage 1 used to be a site scrape. We moved Sonar to stage 1 because Sonar:

1. **Already does the scrape.** Sonar's web-search includes the company site as a top source. Doing both is duplicative.
2. **Returns names AND emails together.** A static scrape gives you a page; you still have to parse names out. Sonar returns structured "person + email" pairs.
3. **Has better coverage for thin-content sites.** Many B2B companies have minimal "team" content. Sonar pulls from LinkedIn, news, press releases, and the long tail.
4. **Costs $0.005.** Roughly the same as a Bright Data scrape call. Worth it for the better answer.

The site scrape stayed because it's free and it catches the cases Sonar misses on (especially small local businesses with high-content "About" pages but no LinkedIn presence).

## Why stage 3 (read pattern off existing email) comes before stage 4 (Hunter)

Hunter charges per lookup. If Sonar or the scrape already returned `someone@company.com`, we can infer the domain's pattern (e.g. `first.last`, `flast`, `first`) for free. Only fall back to Hunter when we have names but zero example emails.

## Why verify even when the email comes from Sonar

Two reasons:

1. **Sonar hallucinates.** It rarely returns a totally fake email, but it sometimes returns a real-looking email that bounces.
2. **Domain drift.** Sonar occasionally returns an email on the wrong domain ("John Doe at Acme → john.doe@gmail.com" instead of `@acme.com`). The post-filter catches most of this; Bouncer catches what slips through.

Verification is non-negotiable. Skipping it = burning your sender reputation.

## Why stage 7 (SERP fallback) is last

It's the slowest (SERP scrape + LLM parse takes 5-10 sec per row), the most expensive (~$0.02), and the most likely to introduce wrong-domain emails. Only fires when stages 1-6 produced nothing usable.

## Future stages we considered and rejected

- **LinkedIn scrape (stage 0).** Tempting but rate-limited, captcha-prone, ToS-violating. The risk-reward is bad. Sonar pulls from LinkedIn for us at stage 1 anyway.
- **Clearbit / Apollo as backup.** Defeats the purpose. We built this to *avoid* paying their per-contact rates.
- **GPT-4 for parsing.** Gemini Flash is 10× cheaper and good enough for SERP/site HTML parsing.
