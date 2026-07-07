# Email patterns

Reference doc for the `three-cent-contacts` skill. Built by Chris Berkley · [chris-as-is.com](https://chris-as-is.com?ref=three-cent-contacts).

The 11 patterns the skill recognizes and can generate, with examples for `Jane Doe @ acme.com`. Codes match the `PATTERNS` dict in `scripts/email_patterns.py` exactly.

| Code | Pattern | Example |
|------|---------|---------|
| `first.last` | first.last@ | jane.doe@acme.com |
| `firstlast` | firstlast@ | janedoe@acme.com |
| `first_last` | first_last@ | jane_doe@acme.com |
| `flast` | first-initial + last | jdoe@acme.com |
| `firstl` | first + last-initial | janed@acme.com |
| `first` | first only | jane@acme.com |
| `last.first` | last.first@ | doe.jane@acme.com |
| `f.last` | first-initial + dot + last | j.doe@acme.com |
| `last` | last only | doe@acme.com |
| `lfirst` | last-initial + first | djane@acme.com |
| `last_first` | last_first@ | doe_jane@acme.com |

## Detection (stage 3)

Given a real email like `mfoster@acme.com` and a known name `Maria Foster`, the detector matches against each pattern in order. First match wins.

## Pattern caching

Once a pattern is detected or fetched for a domain, cache it at `~/.three-cent-contacts/pattern_cache.json` with a 30-day TTL. Subsequent rows for the same domain skip Hunter entirely. Concurrent rows for the same domain share one lookup (per-domain lock in the orchestrator).

## Name normalization

Before detection or generation, names are normalized the same way:

- **Lowercase**, then **diacritics folded to ASCII** (`Müller` → `muller`, `José` → `jose`).
- **Everything that isn't a-z is stripped**: hyphens (`Garcia-Lopez` → `garcialopez`), apostrophes (`O'Brien` → `obrien`), spaces, digits.
- **Single-name people:** without both a first and last token, no pattern is generated; the row falls through to the next stage.

Suffixes (`Jr`, `III`) are not stripped — a name like "Doe Jr" would normalize to `doejr`. If a source returns suffixed names, clean them before passing to the generator.
