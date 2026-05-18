# Email patterns

Reference doc for the `three-cent-contacts` skill. Built by Chris Berkley · [chris-as-is.com](https://chris-as-is.com?ref=three-cent-contacts).

The 11 patterns the skill recognizes and can generate, with examples for `Jane Doe @ acme.com`:

| Code | Pattern | Example |
|------|---------|---------|
| `first.last` | first.last@ | jane.doe@acme.com |
| `firstlast` | firstlast@ | janedoe@acme.com |
| `first_last` | first_last@ | jane_doe@acme.com |
| `flast` | first-initial + last | jdoe@acme.com |
| `first.l` | first + last-initial | jane.d@acme.com |
| `firstl` | first + last-initial (no dot) | janed@acme.com |
| `f.last` | first-initial + dot + last | j.doe@acme.com |
| `first` | first only | jane@acme.com |
| `last` | last only | doe@acme.com |
| `last.first` | last.first@ | doe.jane@acme.com |
| `lastf` | last + first-initial | doej@acme.com |

## Detection (stage 3)

Given a real email like `mfoster@acme.com` and a known name `Maria Foster`, the detector matches against each pattern in order. First match wins. If two patterns match (e.g. `flast` and `last`), the more specific one wins.

## Pattern caching

Once a pattern is detected or fetched for a domain, cache it at `~/.three-cent-contacts/pattern_cache.json` with a 30-day TTL. Subsequent rows for the same domain skip Hunter entirely.

## Edge cases

- **Hyphenated last names** (e.g. `Garcia-Lopez`): both `garcia-lopez` and `garcialopez` are tried; pattern code reports which one is canonical for the domain.
- **Diacritics** (e.g. `Müller`): strip to ASCII (`muller`) before applying pattern.
- **Suffixes** (`Jr`, `III`): drop before pattern application.
- **Single-name people** (rare but real): only `first` and `last` patterns are valid; others are skipped.
