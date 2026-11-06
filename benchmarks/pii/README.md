# Nordic/EU PII benchmark (#35)

A labelled benchmark and recall gate for the pattern PII detector in
`packages/sdk/prompt_shields/pii.py` (+ `national_ids.py`) and its gateway port,
`gateway/src/middlewares/router/pii.ts`. The gateway uses the port to escalate sensitivity.

| file | what |
|---|---|
| `generate.py` | Deterministic generator (seed 35). `--check` fails if `dataset.v1.jsonl` is stale. |
| `dataset.v1.jsonl` | 716 synthetic examples: 11 locales (se, no, dk, fi, de, fr, nl, es, it, pl, gb), 503 positive, 33 neutral, 209 hard negatives. |
| `score.py` | Scorer for the SDK detector: per-category precision/recall/F1 plus recall by locale. `--gate` applies the thresholds, `--misses CAT` lists the misses and false positives. |
| `thresholds.json` | The recall gate: per category, or `category@locale`. |

The gate runs in CI through the existing test jobs:
`packages/sdk/tests/test_pii_benchmark.py` covers the SDK, and
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
information. `person_name` and `postal_address` are labelled, but no detector
reports them, so they are listed and not gated.

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
| phone | 132 | 109 | 144 | 23 | 0.431 | 0.826 | 0.566 | 0.82 |
| person_name | 104 | 0 | 0 | 104 | - | 0.000 | - | not detected |
| postal_address | 22 | 0 | 0 | 22 | - | 0.000 | - | not detected |

Each gate value is the measured recall, rounded down to 2 decimals. The values
are a proposal and still need sign-off from security.

### Before the fixes in this change (same dataset)

| category | precision | recall |
|---|---:|---:|
| credit_card | 0.376 | 1.000 |
| iban | 0.780 | 0.476 |
| phone | 0.190 | 0.462 |

The other categories did not change. The fixes, made in both the SDK and the gateway:

- **IBAN:** grouped (`SE45 5000 0000 …`) as well as compact form, with a mod-97 check. An IBAN that passes the checksum is masked before the other patterns run, so its digits never also count as a card or phone number. A spaced IBAN used to be reported as `credit_card` only.
- **Credit card:** Luhn check.
- **Phone:** grouped international (`+46 70 174 06 12`) and trunk-0 national (`070-174 06 12`, `06 39 98 22 48`, `(07700) 900 290`) formats. The US-shaped pattern no longer matches inside longer digit runs.
- **Email (SDK):** the regex is anchored like the gateway's (#245). The unanchored one was quadratic: about 0.5 s on 20 KB, and seconds on 100 KB.

## Known gaps (not fixed here)

- **Health and financial data outside English:** detection is keyword-based and English-only, so recall is 0.09 for health and 0.26 for financial. Multilingual keywords need care: `diagnos` would also hit "diagnostics", for example.
- **Phones without a `+` or trunk 0:** NO, DK, PL and ES numbers such as `41 15 51 25` or `569 605 158` are missed. They look the same as other grouped numbers, so catching them needs context words.
- **Phone precision:** the US-shaped pattern matches most 10- to 14-digit runs (national IDs, order numbers, timestamps).
- **EU national IDs:** NL BSN, PL PESEL, ES DNI and FR NIR are not detected (`national_id` recall for those locales is 0).
- **DK CPR false positives:** a hyphenated CPR only needs a valid date, so invoice numbers like `YYMMDD-NNNN` are flagged. SE, NO and FI IDs pass their checksum about 1 time in 10 by chance.
- **IP addresses:** IPv6 is not detected, and dotted firmware versions are flagged as IPv4.
- **No detector for names or postal addresses.**
