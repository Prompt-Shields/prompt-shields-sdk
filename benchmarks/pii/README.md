# Nordic/EU PII benchmark (#35)

A labelled benchmark and recall gate for the pattern PII detector in
`packages/sdk/prompt_shields/pii.py` (+ `national_ids.py`) and its gateway port,
`gateway/src/middlewares/router/pii.ts`. The gateway uses the port to escalate sensitivity.

| file | what |
|---|---|
| `generate.py` | Deterministic generator (seed 35). `--check` fails if either dataset is stale. |
| `dataset.v1.jsonl` | 716 synthetic examples: 11 locales (se, no, dk, fi, de, fr, nl, es, it, pl, gb), 503 positive, 33 neutral, 209 hard negatives. |
| `dataset.optin.v1.jsonl` | 141 hand-written held-out examples for the opt-in categories (#351): 102 positive, 39 hard negatives, the same 11 locales. |
| `score.py` | Scorer for the SDK detector: per-category precision/recall/F1 plus recall by locale. `--gate` applies the thresholds, `--misses CAT` lists the misses and false positives. `--extended` scores the opt-in detector, `--holdout` scores it on the held-out set. |
| `thresholds.json` | The recall gate: per category, or `category@locale`. The `opt_in` section gates the opt-in detector. |

The gate runs in CI through the existing test jobs:
`packages/sdk/tests/test_pii_benchmark.py` covers the SDK,
`packages/sdk/tests/test_pii_extended.py` covers the opt-in detector, and
`gateway/src/middlewares/router/piiBenchmark.test.ts` covers the gateway (set
`PS_PII_BENCH_REPORT=1` to print its report).

## Synthetic data only

No example contains real personal data:

- Names are common first and last names, combined at random.
- Emails use RFC 2606 reserved domains only.
- Phones use regulator fiction ranges where one exists (SE 070-17406xx, UK 07700 900xxx, FR 06 39 98) and random digits elsewhere.
- National IDs (SE personnummer and samordningsnummer, NO fødselsnummer, D-numbers and Tenor numbers, DK CPR, FI HETU, NL BSN, PL PESEL, ES DNI, FR NIR), IBANs (11 countries, compact and in groups of four) and card numbers are random but pass their checksums.
- IPs come from the documentation ranges (RFC 5737 and RFC 3849).

A test checks the email domains and the IP ranges.

Hard negatives are values that look like PII: dates, order, invoice and customer numbers (`YYYYMMDD-NNNN`, `YYMMDD-NNNN`, 11-digit numbers), tracking numbers, EAN-13 codes, org and VAT numbers, firmware versions, build timestamps, UUIDs, ISBNs and `@handles`.

## Scoring

The detectors report which categories are present, never spans, so scoring
works the same way. For each example and each category:

- TP: the category is labelled and detected.
- FP: the category is detected but not labelled.
- FN: the category is labelled but missed.

`financial_data` is labelled on examples that contain IBANs and on bank-account
or tax-ID topics. `health_data` is labelled on an individual's medical
information. `person_name` and `postal_address` are labelled, but the default
detectors never report them: only the opt-in SDK detector does (see below).

## Results (SDK and gateway, identical on every example)

| category | support | TP | FP | FN | precision | recall | F1 | gate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| credit_card | 44 | 44 | 3 | 0 | 0.936 | 1.000 | 0.967 | 1.00 |
| email | 99 | 99 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.00 |
| financial_data | 104 | 27 | 0 | 77 | 1.000 | 0.260 | 0.412 | 0.25 |
| health_data | 22 | 2 | 0 | 20 | 1.000 | 0.091 | 0.167 | 0.09 |
| iban | 82 | 82 | 0 | 0 | 1.000 | 1.000 | 1.000 | 1.00 |
| ip_address | 44 | 38 | 14 | 6 | 0.731 | 0.864 | 0.792 | 0.86 |
| national_id | 68 | 60 | 13 | 8 | 0.822 | 0.882 | 0.851 | 0.88 (1.00 for each of se/no/dk/fi) |
| phone | 132 | 109 | 155 | 23 | 0.413 | 0.826 | 0.551 | 0.82 |
| person_name | 104 | 0 | 0 | 104 | - | 0.000 | - | not detected (opt-in only) |
| postal_address | 22 | 0 | 0 | 22 | - | 0.000 | - | not detected (opt-in only) |

Each gate value is the measured recall, rounded down to 2 decimals. The values
are a proposal and still need sign-off from security.

### Before the fixes in this change (same dataset)

| category | precision | recall |
|---|---:|---:|
| credit_card | 0.376 | 1.000 |
| iban | 0.780 | 0.476 |
| phone | 0.190 | 0.462 |

The other categories did not change. The fixes, made in both the SDK and the gateway:

- **IBAN:** grouped (`SE45 5000 0000 …`) as well as compact form, with a mod-97 check. An IBAN that passes the checksum is masked before the other patterns run, so its digits never also count as a card or phone number. Only the checksum-valid part is masked, so a card number written right after an IBAN is still found. A spaced IBAN used to be reported as `credit_card` only.
- **Credit card:** Luhn check. When a 13-19 digit match fails it, the search retries from the match's next digit group, so a card number after an unrelated number (`Ref 12 4111 1111 1111 1111`) is still found.
- **Phone:** grouped international (`+46 70 174 06 12`) and trunk-0 national (`070-174 06 12`, `06 39 98 22 48`, `(07700) 900 290`) formats. The US-shaped pattern no longer matches inside longer digit runs (letters next to it are still allowed, as in `555-123-4567x89`).
- **Email (SDK):** the regex is anchored like the gateway's (#245). The unanchored one was quadratic: about 0.5 s on 20 KB, and seconds on 100 KB.

## Opt-in detector: names, addresses, multilingual vocabulary (#351)

`prompt_shields.pii_extended.detect_pii_categories_extended` (SDK only) returns
what the default detector returns, plus `person_name` and `postal_address`, and
matches `health_data` and `financial_data` with multilingual keyword lists
(and counts a checksum-valid IBAN as `financial_data`). It is opt-in: nothing
calls it, and the default detector, `find_pii_spans`, telemetry, the router,
the pseudonymiser and the gateway port are unchanged. **These categories are
for detection and telemetry only. They must not be used for blocking until
security signs off their thresholds (#35).**

The default backend is rule-based (pure Python, no dependencies, linear time):
a common given name followed by a capitalised surname, a title ("Frau Weber",
"M. Dubois") or a cue ("my name is", "jag heter", "Namn:") for names; a street
with a house number in each locale's forms, or a UK postcode, for addresses.
`ner="spacy"` adds a spaCy model's `PER` entities (`prompt-shields[ner]` extra
plus a model, `xx_ent_wiki_sm` by default).

`dataset.v1.jsonl` was used to build the rules, so its numbers are optimistic:
its names are drawn from short lists of common names, and the rules' given-name
list includes them. `dataset.optin.v1.jsonl` is a separate hand-written set
with other names (some outside the given-name list, some inflected), streets
and sentences, plus hard negatives (capitalised non-names, streets without a
number, "diagnostics", "Rezept", "prescrizione"). The rules were not changed
after it was first scored. Its numbers are the honest estimate. Only the four
opt-in categories are scored on it.

### Rule-based (`ner=None`), gated in CI

| category | v1 recall (support) | v1 precision | held-out recall (support) | held-out precision | gate v1 / held-out |
|---|---:|---:|---:|---:|---:|
| person_name | 1.000 (104) | 1.000 | 0.688 (48) | 0.971 | 1.00 / 0.68 |
| postal_address | 1.000 (22) | 1.000 | 0.818 (22) | 1.000 | 1.00 / 0.81 |
| health_data | 0.955 (22) | 1.000 | 0.864 (22) | 0.950 | 0.95 / 0.86 |
| financial_data | 1.000 (104) | 1.000 | 0.955 (22) | 1.000 | 1.00 / 0.95 |

Held-out recall by locale (support in brackets):

| category | de | dk | es | fi | fr | gb | it | nl | no | pl | se |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| financial_data | 1.000 (2) | 1.000 (2) | 1.000 (2) | 0.500 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) |
| health_data | 0.500 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 0.500 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 0.500 (2) | 1.000 (2) |
| person_name | 0.600 (5) | 0.750 (4) | 0.750 (4) | 0.750 (4) | 0.750 (4) | 0.400 (5) | 1.000 (4) | 0.750 (4) | 0.750 (4) | 0.500 (4) | 0.667 (6) |
| postal_address | 0.500 (2) | 0.500 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 1.000 (2) | 0.500 (2) | 1.000 (2) | 0.500 (2) |

On v1 every locale is at 1.000 for the four categories, except `health_data@it` (0.500, 2 examples:
"La pressione era alta" has no keyword, because "pressione" alone is too
ambiguous). On v1 the default categories are unchanged, so the opt-in detector
also passes the default gate. Per-locale support is small (2 to 6 examples),
so locale numbers are indicative only and are not gated.

What the held-out set misses:

- **Names:** given names outside the list (Gunnel, Torbjörn, Bjørg, Dorthe,
  Pirkko, Katrin, Julien, Alberto, Harriet, Priya, Tom); inflected titles
  and names ("Herrn Schulz", "Panią Kowalczyk", "Piotra Lewandowskiego");
  lowercase titles ("mevrouw Visser"). One false positive: "Karl Johans gate"
  (a street named after a king).
- **Addresses:** streets without a street word ("Strøget 2", "Bryggen 3",
  "Am Markt 5") and the Swedish separate-word form "Södra vägen 7".
- **Health:** "Allergie ... festgestellt", "zwolnieniu lekarskim" (inflected),
  "off sick"; one false positive ("Kør en diagnose på printeren").
- **Financial:** inflected Finnish "tilinumerosi".

Fixing these needs a new held-out set to measure the fix honestly.

### spaCy NER (`ner="spacy"`, `xx_ent_wiki_sm` 3.8.0), measured once, not gated

The extra is not installed in CI, so this backend is measured here and not gated.

| person_name | recall | precision |
|---|---:|---:|
| v1, rules + spaCy | 1.000 | 0.515 (98 FP) |
| v1, spaCy alone | 0.962 | 0.505 |
| held-out, rules + spaCy | 0.958 | 0.807 (11 FP) |
| held-out, spaCy alone | 0.917 | 0.800 |

The multilingual model finds names the rules miss, but it tags many
capitalised words in short prompts as people. That is too noisy for blocking,
and noisy for telemetry.

### Latency (SDK, per prompt, one CPU, benchmark prompts)

| detector | p50 | p95 |
|---|---:|---:|
| default `detect_pii_categories` | 0.02 ms | 0.05 ms |
| opt-in, rules | 0.05 ms | 0.10 ms |
| opt-in, rules + spaCy | 1.5 ms | 2.4 ms |

The issue allows a gateway port only if the p95 overhead is under 10 ms. Both
backends are under that in the SDK, but there is no gateway port yet (the
rules would need porting to TypeScript, and spaCy has no Node runtime).

## Known gaps (not fixed here)

- **Health and financial data outside English (default detector):** the default detector is keyword-based and English-only, so recall is 0.09 for health and 0.26 for financial. The opt-in detector covers 11 languages (see above).
- **Phones without a `+` or trunk 0:** NO, DK, PL and ES numbers such as `41 15 51 25` or `569 605 158` are missed. They look the same as other grouped numbers, so catching them needs context words.
- **Phone precision:** the US-shaped pattern matches most 10- to 14-digit runs (national IDs, order numbers, timestamps).
- **EU national IDs:** NL BSN, PL PESEL, ES DNI and FR NIR are not detected (`national_id` recall for those locales is 0).
- **DK CPR false positives:** a hyphenated CPR only needs a valid date, so invoice numbers like `YYMMDD-NNNN` are flagged. SE, NO and FI IDs pass their checksum about 1 time in 10 by chance.
- **IP addresses:** IPv6 is not detected, and dotted firmware versions are flagged as IPv4.
- **Names and postal addresses:** only the opt-in SDK detector reports them, with the gaps listed above. The gateway has no detector for them.
