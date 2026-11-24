# Stream usage (#389)

Every streamed call reports token usage: measured when the provider sends it,
estimated otherwise. Events say which with `usage_source` (`provider` or
`estimated`); OpenTelemetry spans with `ps.usage_source`.

- `include-usage.v1.json` — where the gateway (`gateway/src/middlewares/psStreamUsage.ts`)
  and the SDK (`prompt_shields/streaming.py`) ask for usage with
  `stream_options.include_usage`. Tests on both sides check their lists
  against it.
- `vectors/estimates.json` — shared vectors for the estimate below. Both
  `prompt_shields/usage_estimate.py` and `gateway/src/middlewares/psUsageEstimate.ts`
  must return `expected` exactly; `reference` is the count of OpenAI's
  tokenizer for the accuracy check.

## The estimate

It approximates a byte-pair tokenizer without shipping a vocabulary. All
arithmetic is in integer hundredths of a token; the result is
`floor((total + 50) / 100)`.

1. Classify each code point: whitespace (the fixed set in the code), ASCII
   digit, ASCII letter, other ASCII (punctuation), accented Latin
   (U+00C0–U+024F except × ÷), other alphabetic (U+0370–U+1FFF and anything
   not listed), symbol (U+2000–U+2BFF, U+FE00–U+FE0F, ≥ U+1F000), Hangul,
   CJK punctuation (U+3000–U+303F, U+FF01–U+FF0F, U+FF1A–U+FF20), CJK.
2. Group runs: consecutive letters of any of the three letter classes form one
   word; otherwise consecutive code points of the same class.
3. Cost each run with the family's parameters (`FAMILY_PARAMS`):
   - ASCII-only word: split at lower→upper case changes; each part of length
     k costs 100 if k ≤ `word_free`, else 100 + 100·⌈(k − word_free)/word_step⌉.
   - Word with other alphabetic letters: max(100, length·`alpha`); with
     accented Latin letters: max(100, length·`latin`).
   - Digits: 100·⌈n/`digit_run`⌉. Punctuation: a single mark directly before a
     word costs `glued` (100 if a whitespace run precedes it); otherwise
     100·⌈n/`punct_run`⌉.
   - CJK, Hangul, CJK punctuation, symbols: n times their per-character cost.
   - Whitespace: a final space (or tab before a word) joins the next run
     (not before digits); after punctuation, newlines join that punctuation;
     anything left costs 100.
4. Requests add 3 tokens per message and 3 for the reply; images and other
   media count 1,600 tokens.

Families: `o200k` (OpenAI's current models, and Gemini) and `cl100k` (all
others, Anthropic included). The parameters were fitted to OpenAI's
`o200k_base` and `cl100k_base` encodings on the fixture texts. Over the 26
fixtures the total is within 3% of the tokenizer for both, the median text
within 5%; the worst texts are 14% (`o200k`) and 27% (`cl100k`, Italian
prose) off. It is an estimate: languages and formats outside the fixtures can
be further off, and the Anthropic and Gemini figures are not checked against
their own tokenizers, which are not public.

Changing the algorithm or parameters means regenerating `expected` in the
vectors and keeping both implementations equal.
