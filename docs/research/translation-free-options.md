# Free and cheap translation options (follow-up to translation.md)

Checked: 2026-10-30. Researcher findings, later restructured by the architect around the chosen stack. Decision: `docs/adr/ADR-0001-stack.md` section 5. No key or `.env` was requested or handled.

## 1. Question and decision it informs

What are the free and low-cost alternatives to a paid translation API, and how do they compare on quality, size and privacy? The user prefers free or cheap.

**Outcome:** Claude Haiku 5.5 is primary (`translation.md`). A local `quickmt` pack is the optional fallback, and `null` is the last resort.

## 2. Paid models, published prices

| Model | Verified (source, date) | Inferred / unknown |
|---|---|---|
| Haiku 5.5 | $0.10 in / $0.50 out per MTok, prompt caching (`translation.md`) | Slang quality unmeasured |
| Sonnet 5.5 | $2 / $10 per MTok (`translation.md`) | Likely better quality, but heavy for live chat |
| deepseek-flash | DeepSeek docs: 1M context, JSON output and tool calls, thinking on by default, $0.30 in (cache miss) / $1.20 out per MTok peak, half off-peak (https://api-docs.deepseek.com/quick_start/pricing) | Strong Chinese is an expectation, not tested. Servers are DeepSeek's |
| Gemini 3.8 Flash | Google: `gemini-3.8-flash` stable; $0.75 in / $3.75 out through 2026-12-31, then $1.50 / $7.50 (https://ai.google.dev/gemini-api/docs/pricing) | Price doubles on 2027-01-01 |
| Gemini 3 Flash | Google lists `gemini-3-flash-preview` as Preview at $0.50 / $3.00 | Preview models can change or vanish |
| Gemini 3.1 Flash-Lite | $0.25 / $1.50 (same pricing source) | Chinese slang quality unknown |
| Gemini 3.5 Flash-Lite | $0.30 / $2.50 (same pricing source) | Chinese slang quality unknown |

## 3. LibreTranslate and Argos

### 3.1 Verified facts
- Licence AGPL-3.0, engine is Argos Translate (https://github.com/LibreTranslate/LibreTranslate, 2026-10-30). Install: `pip install libretranslate` (Python >= 3.8, https://docs.libretranslate.com/guides/installation/), or Docker (`run.bat` on Windows). `LT_LOAD_ONLY` (= `--load-only`) restricts loaded languages; by default all load.
- **No PyTorch needed with LibreTranslate itself.** PyPI `libretranslate` 1.9.6 requires `argos-translate-lt==1.12.1`, whose requirements are ctranslate2, sentencepiece, sacremoses, minisbd, packaging (PyPI JSON, 2026-10-30). By contrast stock `argostranslate` 1.11.0 requires `stanza`, which has a hard `torch>=1.13.0` requirement. The rule "no PyTorch" therefore holds only with `argos-translate-lt` or `libretranslate`, not stock `argostranslate`. Confirmed by installing into a throwaway venv: no torch, stanza or spacy present.
- **Local measurement:** `pip install libretranslate` into a clean Python 3.12 venv = **429 MB** (pymupdf 105, ctranslate2 62, onnxruntime 48, numpy 69, babel 32). Argos zh->en model: **74.5 MB** download. Translation about **23 ms per line** on this 32-thread machine (16 lines, one at a time). RAM not measured. A hosting vendor says "8 GB+ RAM" for all languages, which applies to loading 30+ languages, not zh-en alone (weak evidence).
- Hosted `libretranslate.com` is not free: the portal shows paid keys (the plan prices found conflict: $9, $19, $58; portal shows 80 or 200 calls/min and a 2,000-character limit per call; weak, ambiguous). Its privacy terms were not retrievable. Treat public instances as: text goes to a stranger, no SLA. Not acceptable by default. Self-hosting is free.
- Glossary or context: **no documented glossary or context parameter** in the pages that could be read. Treat as unsupported (inference; check the API docs). Classic NMT cannot follow style instructions either.

### 3.2 Quality (16 hand-written casual lines, informal judgement, no references)
Full table: `docs/research/data/translation_free_offline_samples.md`.
- Argos zh-en: fast, but drops clauses, mistranslates slang, and gets some order wording wrong. About 5 of 16 lines wrong or incomplete.
- quickmt zh-en (CT2 int8): better at UI and commerce wording, still wrong on gaming slang, about 5 of 16 wrong. 18 ms/line int8, 35 ms/line default, CPU, 4 threads, beam 2. `model.bin` is 407 MB (float16) with 2 small SentencePiece files; memory not measured; int8 was a load-time setting, and an int8 file conversion was not tried.
- Conclusion: both are usable for UI labels and plain sentences, **not for slang**. Neither takes a glossary. quickmt looks slightly better; both beat nothing when the network is down. 16 lines is a smoke test, not a benchmark.

## 4. Other free options (verified)

- **Gemini API free tier.** Flash and Flash-Lite 3.x models available free, rate limits per project and "not guaranteed", numbers only visible in AI Studio (https://ai.google.dev/gemini-api/docs/pricing, /rate-limits, /billing, 2026-10-30). Model ids: `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` stable. **Data terms:** on the free tier "content may be used to improve Google's products", and the terms say human reviewers "may read, annotate, and process" input and output (https://ai.google.dev/gemini-api/terms). Paid tier: not used for product improvement. EEA/UK/Swiss users must use paid. Linking billing and prepaying $5 moves a project to the paid tier. Billing is not required for free. This conflicts with the privacy rule for other people's chats, so it would need an explicit opt-in with a warning.
- **DeepL API Free:** 500,000 chars/month (`translation.md`). Retention: official wording not retrieved (the docs page 404'd). Third-party sources say free-tier text may be stored and used to improve models, API Pro is not stored (weak, unverified). Treat as the same privacy class as Gemini free until confirmed.
- **Local quickmt:** CC-BY-4.0, free, offline, see above. Does not fit the 400 MB RAM target alongside OCR, so it is an optional pack (ADR-0001).
- Not researched in depth, listed for completeness: Google Cloud NMT 500k chars/month free (needs GCP billing), Bergamot/translateLocally (not tested), local small LLMs (violate CPU-only/lightweight).

## 5. Cost model

Uses `translation.md` assumptions: light room 180 lines/h, busy 1,200 lines/h, 4 h/day = 120 h/month = **1,440 h/year**, per-request tokens as there (light 1.5 lines/request, busy 4). No caching and no thinking tokens assumed for non-Anthropic models (their prompt caching was not verified), so these are floors for thinking models.

| Engine | Light /h | Busy /h | Per year light / busy |
|---|---|---|---|
| quickmt / Argos local | $0 | $0 | $0 / $0 |
| Gemini free tier | $0 | $0 (if inside unpublished limits) | $0 / $0 |
| DeepL API Free | $0 up to 500k chars/month | about 28 busy hours then blocked | $0 within quota |
| Haiku 5.5 (`translation.md`) | $0.011 | $0.039 | **$15 / $56** |
| Gemini 3.1 Flash-Lite paid ($0.25 / $1.50) | $0.066 | $0.20 | $95 / $289 |
| Gemini 3.5 Flash-Lite paid ($0.30 / $2.50) | $0.083 | $0.26 | $120 / $381 |
| deepseek-flash direct, peak ($0.30 / $1.20) | $0.076 | $0.22 | $109 / $317 |

(Derivation per request, micro-dollars, no cache: light input 1,908 tokens, output 49; busy 1,988 and 114. Example Gemini 3.1 Flash-Lite light: 1,908 x 0.25 + 49 x 1.5 = 550.5, x 120 requests/h = $0.066/h. Haiku's win comes from its $0.10 input price plus caching. This is arithmetic on stated assumptions, not measurement.) Thinking tokens on Gemini or DeepSeek would add to the output cost.

## 6. Recommendation

1. **Primary: Anthropic Haiku 5.5**, the cheapest paid route compared here, and it follows glossary and style instructions.
2. **Optional fallback: local `quickmt-zh-en` pack (CT2, int8)**, free, offline, about 20 ms/line, handles UI labels and plain sentences. Output is marked as rough in the UI because slang is unreliable. LibreTranslate is not needed: it is a server wrapper around the weaker Argos model, with a 429 MB install and no documented glossary.
3. **Last resort: `null`** (show original, amber status).

LLM quality is much better than NMT here (section 3.2), so glossary and context features work only with the LLM.

## 7. Rejected

- **LibreTranslate public instance:** paid keys, unclear privacy, stranger-operated.
- **LibreTranslate as the local engine:** 429 MB, server process, Argos zh-en weaker in the smoke test, no documented glossary.
- **Stock `argostranslate` package:** pulls stanza and therefore PyTorch.
- **Gemini free tier as default:** content used for improvement and human review; fine only as an explicit opt-in.
- **DeepL Free in the MVP:** privacy terms unverified, 500k char cap, needs its own adapter.
- **Sonnet 5.5, Gemini Pro models for live chat:** heavier and slower than needed.
- **NLLB, opus-mt, local LLMs:** see `translation.md`.

## 8. Risks and unknowns

1. Haiku 5.5 latency, slang quality and refusal rate are unmeasured. Anthropic data-retention terms are unresearched (`translation.md` risk 8).
2. quickmt: 407 MB, RAM and cold start unmeasured, self-reported quality, slang weak; the 16-line comparison is informal.
3. Gemini and DeepSeek prices change (Gemini 3.8 Flash doubles on 2027-01-01).
4. LibreTranslate public pricing evidence conflicts ($9/$19/$58).
5. DeepL Free retention terms are unverified.

## 9. Sources (checked 2026-10-30)

- Gemini models: https://ai.google.dev/gemini-api/docs/models
- Gemini pricing: https://ai.google.dev/gemini-api/docs/pricing
- Gemini rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini billing: https://ai.google.dev/gemini-api/docs/billing
- Gemini API terms: https://ai.google.dev/gemini-api/terms
- DeepSeek pricing: https://api-docs.deepseek.com/quick_start/pricing
- LibreTranslate repo: https://github.com/LibreTranslate/LibreTranslate
- LibreTranslate docs: https://docs.libretranslate.com/ and https://docs.libretranslate.com/guides/installation/
- LibreTranslate portal (price, ambiguous): https://portal.libretranslate.com
- PyPI JSON: https://pypi.org/pypi/libretranslate/json, https://pypi.org/pypi/argostranslate/json, https://pypi.org/pypi/argos-translate-lt/json, https://pypi.org/pypi/stanza/json
- Argos model index: https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json
- quickmt-zh-en: https://huggingface.co/quickmt/quickmt-zh-en
- DeepL retention (third party, weak): https://conductatlas.com/platform/deepl/deepl-privacy-policy/
- Local data: `docs/research/data/translation_free_offline_samples.md`; scripts `docs/research/scratch/bench_argos_zh_en.py`, `docs/research/scratch/bench_quickmt_cpu.py`
