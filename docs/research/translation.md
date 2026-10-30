# Translation engine research (zh -> en chat lines and UI labels)

Checked: 2026-10-30. Author: researcher. Status: Phase 1 input for the architect. No code outside `docs/research/` touched.

## 1. Question and decision it informs

Which translator should be the primary, fallback and offline engine behind the `Translator` port, and how should Gloss batch, add context, use a glossary and cache so a live Chinese chat costs pennies per hour?
Decisions fed: `[translate]` defaults in `config/default.toml` (provider, model, batch size, wait, context lines, request cap), prompt/cache layout, fallback chain (SPEC section 4).

## 2. Options compared

| Option | Maturity / status (checked 2026-10-30) | Licence / cost model | Size | Speed | Quality for zh chat + slang | Windows / Python notes |
|---|---|---|---|---|---|---|
| Anthropic `claude-haiku-5-5` | Released 2026-10-07 (3 weeks old). Retirement "not sooner than 2027-10-07" | Pay-per-token: $0.10 in / $0.50 out per MTok (prompts <=100K) | 0 (cloud) | Doc says "Fastest" of the lineup. No published ms figure. Not measured by me | Best available for slang/tone/glossary instructions (LLM). No Gloss-specific eval yet | Official `anthropic` SDK, structured outputs GA, caching |
| Anthropic `claude-sonnet-5-5` | Current, "Fast" | $2 / $10 per MTok | 0 | Slower than Haiku | Likely better on puns/slang; unmeasured | Same SDK. Models API says thinking cannot be set to `disabled` on it |
| Anthropic `claude-haiku-4-5` | Legacy, still available | $1 / $5 per MTok | 0 | Fast | Known quantity | Needs 4,096-token minimum for caching (vs 512) |
| DeepL API | Mature NMT | Free: 500,000 chars/month. Pro: base fee + per-million chars (see risks: exact rate unconfirmed) | 0 | Fast (latency_optimized model_type exists) | Good literary quality; weak on slang/gaming jargon unless glossary; no style prompt | REST, `deepl` pkg. Has `context` param (not billed) and glossaries |
| Google Cloud Translation (NMT) | Mature | $20 / 1M chars after 500,000 free ($10 credit) per month | 0 | Fast | Fine for plain text, weakest on slang | Needs GCP project + service-account auth: heavier setup than an API key |
| Google "Translation LLM" | GA on pricing page | $10 / 1M chars in + $10 / 1M out | 0 | Unknown | Unknown | Same GCP setup burden |
| quickmt-zh-en (CTranslate2) | HF model updated 2026-04-13, ~115 downloads | CC-BY-4.0, free | ~200M params; `model.bin` is 407 MB in repo (int8 conversion would be smaller, untested) | GPU-only timing published (1.1 s / 1012 sentences on RTX 4070S). **CPU not measured** | FLORES zh-en BLEU 30.0 (news-like text, not chat) | Needs `ctranslate2`, `sentencepiece`; quickmt lib installs from GitHub |
| opus-mt-zh-en | Old, very common | Open | ~300 MB class | 3.7 s GPU benchmark above | BLEU 22.99 | Same stack |
| NLLB-200 distilled 600M / 1.3B | Common | CC-BY-NC (non-commercial; check before bundling) | 600M-1.3B params | 21.7 s / 37.6 s GPU benchmark above (slow) | BLEU 26.02 / 28.61 | Heavy for "lightweight" goal |
| Local LLM (e.g. Qwen) | Not researched in depth | Open weights | GBs | Needs GPU or slow CPU | Claimed good for zh, no benchmark found | Violates "lightweight, CPU-only" |

## 3. Evidence

### 3.1 Anthropic (all from platform.claude.com, which `docs.claude.com` redirects to; checked 2026-10-30)

- Current lineup and IDs: `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`; each ID is a pinned snapshot, no date suffix. The SPEC/config default `claude-haiku-5-5` is **correct as of today**. Source: [Models overview](https://platform.claude.com/docs/en/about-claude/models/overview).
- Haiku 5.5: released 2026-10-07, 1M context, 128K max output, latency "Fastest", adaptive thinking, default effort `medium`, retirement not sooner than 2027-10-07. Source: [Haiku 5.5 page](https://platform.claude.com/docs/en/models/haiku-5-5/overview).
- Pricing per MTok (prompts up to 100K): Haiku 5.5 base in $0.10, 5m cache write $0.125, 1h write $0.20, cache read $0.01, out $0.50. Sonnet 5.5: $2 / $2.50 / $4 / $0.10 / $10. Haiku 4.5: $1 / $1.25 / $2 / $0.10 / $5. Batch API is 50% off (not useful for live chat). Source: [Pricing](https://platform.claude.com/docs/en/about-claude/pricing).
- Tokenizer: Haiku 5.5 uses the newer tokenizer that yields about 30% more tokens for the same text than Haiku 4.5. Sources: Haiku 5.5 page, Pricing. I could not call the token-counting endpoint (no key, and I must not touch keys), so Chinese token counts below are assumptions.
- Prompt caching: minimum cacheable prompt is **512 tokens** for Haiku 5.5 and Sonnet 5.5 (4,096 for Haiku 4.5). Default TTL 5 minutes, refreshed free on each hit; 1-hour TTL optional. Prefix order tools -> system -> messages; up to 4 explicit breakpoints; top-level automatic `cache_control` available. Changing `output_config.format`, tool definitions, or (for messages) the effort setting invalidates cache. Source: [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
- Structured output is GA via `output_config.format = {type: "json_schema", schema}`; `claude-haiku-5-5` is supported. Limits: no `minLength/maxLength`, no `minItems` above 1, `additionalProperties` must be `false`. First request with a new schema pays grammar-compile latency; compiled grammar is cached 24 h. Claude gets an extra system prompt describing the format (more input tokens). Refusal (`stop_reason: "refusal"`) and `max_tokens` truncation can break schema conformance. Source: [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
- Thinking on Haiku 5.5: **on by default, counts toward `max_tokens`, and is billed as output**. `effort: "low"` is cheapest/fastest ("chat, short tool tasks, simple high-volume requests"). `thinking: {"type":"disabled"}` works at low/medium/high. Telling the model in the prompt to "answer directly" did not stop thinking. With thinking off, the model "sometimes writes reasoning-like text in the reply"; Anthropic suggests adaptive thinking + medium if seen. Changing effort between requests invalidates the message cache. Source: [Prompting Haiku 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-haiku-5-5), [Effort](https://platform.claude.com/docs/en/build-with-claude/effort).
- Sonnet 5.5: the Models overview states the Models API reports `thinking.types.disabled.supported = false` for it (so only effort control). Source: Models overview, "Using the Models API".
- New in Haiku 5.5: safeguard classifiers can return `stop_reason: "refusal"`, no server-side fallback; "sending the same request again usually returns another refusal". Gloss must treat a refusal as a per-batch failure and fall back (chat text could be flagged). Source: Prompting Haiku 5.5.
- Rate limits (org-level, Messages API), Start tier for Haiku 5.5: 1,000 RPM, 2,000,000 ITPM, 400,000 OTPM; Build 5,000 / 5M / 1M. Cached reads do not count toward ITPM. New orgs may start in an "Evaluation" tier with lower limits (numbers not published on that page). Monthly spend caps: Start $500, Build $1,000. Hitting the cap returns HTTP 429 with `error.details.error_code = enforced_spend_limit_reached` and **no `retry-after`**, so SDK retries just fail. A user-set spend limit instead returns HTTP 400. Source: [Rate limits](https://platform.claude.com/docs/en/api/rate-limits).
- Latency: no millisecond or tokens/second figures are published in the pages above, only the relative label. I did **not** measure it. Treat "p50 <= 1.5 s" as unproven until a spike with a real key (see section 7).

### 3.2 DeepL

- Free API tier: 500,000 characters/month (DeepL pricing page says "API pricing includes a free tier of 500,000 characters, with paid pricing based on a base price and per-million-character usage"). Source: [DeepL Pro page](https://www.deepl.com/en/pro-api?cta=header-pro-api/), fetched 2026-10-30.
- Pro API rate: **not confirmed from an official page** (the DeepL pages I could fetch omit the figure; support article returned 403). Several third-party sites quote $5.49/month base + $25 per 1M characters; one aggregator quotes a different figure. I use $25/M as an unverified assumption, flagged. Source: web search results (low-grade evidence).
- Billing counts Unicode code points, so one Chinese character = 1 billed char. Source: [Usage limits](https://developers.deepl.com/docs/resources/usage-limits).
- Request limit 128 KiB; `text` is an array, no item cap listed, results return in input order. `context` param is not translated and **not billed**; `glossary_id`, `model_type` (`quality_optimized` / `prefer_quality_optimized` / `latency_optimized`), optional `source_lang` auto-detect. Source: [Translate API reference](https://developers.deepl.com/api-reference/translate/request-translation).
- No free-form style/tone prompt, so gaming slang depends on glossary entries only.

### 3.3 Google

- Cloud Translation NMT: $20 per 1M characters above 500,000 free per month (free amount is a $10 monthly credit shared by Basic/Advanced; not for formatted documents). Translation LLM: $10/1M input chars + $10/1M output chars. Source: [Cloud Translation pricing](https://cloud.google.com/translate/pricing), text extracted from the live page 2026-10-30.
- Auth requires a GCP project and billing (my inference from product shape; not re-verified this session). Heavier first-run for a non-developer than a single API key.

### 3.4 Offline

- quickmt-zh-en: ~200M-param transformer, CC-BY-4.0, FLORES-200 devtest zh->en BLEU 30.0, chrF2 58.42, COMET22 86.72. Same table: opus-mt-zh-en 22.99 / 53.98 / 84.6; NLLB-600M 26.02 / 55.27 / 85.1; NLLB-1.3B 28.61 / 57.43 / 86.22. Times are for 1012 sentences on an RTX 4070S, batch 32 (1.10 s, 3.73 s, 21.69 s, 37.55 s). The card has `verified: false` for the metrics (self-reported). FLORES is encyclopaedic/news text, not chat. Source: [quickmt-zh-en](https://huggingface.co/quickmt/quickmt-zh-en), HF API `lastModified` 2026-04-13.
- A third-party personal test found NLLB better than Opus-MT for conversational text and Opus-MT failing on a harder batch; anecdotal. Source: [whynothugo.nl](https://whynothugo.nl/journal/2025/11/02/translation-models-between-english-and-chinese/) (via search snippet).
- No source found with CPU latency for any of these on zh->en. Classic NMT does not follow a "keep usernames, expand slang from glossary" instruction at all.

## 4. Local measurements

None completed. I wrote `docs/research/scratch/bench_quickmt.py` (CPU int8 CTranslate2 latency on 20 chat-like lines) but `uv` is not installed on this machine's PATH, and installing tools globally is forbidden by my rules, so **it was not run**. No numbers are claimed. Machine seen: 32 logical CPUs (via `nproc`).
Not measured either: Anthropic latency (needs a key), Chinese token counts (needs the token-counting endpoint).

## 5. Design recommendations

### 5.1 Request shape
- One request per batch. Input: JSON list `[{"i":1,"t":"..."}]` using short integer ids (not hashes) to cut tokens and mismatches. Output via `output_config.format` json_schema: `{"translations":[{"i":int,"en":string}]}`, `additionalProperties:false`. Keep the schema constant forever (changing it drops the prompt cache and costs a grammar compile).
- Validate the response: every id present exactly once. Missing ids get re-queued; a whole-batch failure falls back to the next provider. Never trust order.
- Effort: set `effort: "low"` explicitly, and test `thinking: {"type":"disabled"}` on Haiku 5.5. Decide by golden-set results. Thinking left at default `medium` can add hundreds of billed output tokens per call and latency (see cost sensitivity).
- `max_tokens`: output is tiny (about 26 tokens/line); set `max_tokens` = lines x 80 + 256 so nothing truncates. Truncation breaks JSON.
- Treat `stop_reason` of `refusal` and `max_tokens` as `TransientError`/fallback triggers. Treat 429 with `enforced_spend_limit_reached` as `PermanentError` for the month (no retry; tell the user), unlike a normal 429 with `retry-after`.

### 5.2 Batching
- Chat arrives in bursts when OCR sees a scroll (several new lines per frame). The spec's `batch_max_wait_ms = 400` mostly yields batch size 1 in a slow room and 2-5 in a busy one. Larger waits hurt the p50 <= 1.5 s target. Recommendation: flush when `>= batch_max_lines` OR 400 ms after the first queued line, whichever first; but also **flush immediately when the first line is a UI-label burst** (many lines at once). Do not hold lines to fill a batch.
- Marginal value of batching is smaller than one might think: with Haiku 5.5 the fixed per-request cost is mostly the cached prefix (about $0.000024) so batching mainly saves latency/RPM, not money. See sensitivity.

### 5.3 Context and glossary
- System prompt (static, cached): style rules from SPEC section 6, output rules, and the full glossary (`docs/glossary/zh_gaming.yaml`) rendered deterministically (sorted, no timestamps). Place `cache_control` on the end of it. Aim for it to exceed 512 tokens, otherwise it is silently not cached (check `cache_read_input_tokens`).
- Any glossary edit changes the prefix, forcing one cache write (about $0.0002 at 1,500 tokens on Haiku). Include `glossary_version` in the SQLite key as the SPEC says.
- Context (last K lines, source + English) goes in the user message **after** the cache breakpoint; it changes every call so do not cache it. K = 10 costs about 360 tokens (about $0.00004 on Haiku). Marking context lines as "already translated, for reference only" avoids retranslation.
- Large glossaries: only the prefix cost grows, at $0.01/MTok on cache hit, so a 5,000-token glossary costs about $0.00005 per request when hot. Cheap on Haiku; on Sonnet 5.5 about 10x.
- Keep the 5-minute TTL (write 1.25x). A 1-hour TTL (2x write) only pays off if a room has gaps >5 min repeatedly; the break-even is tiny either way. Make `cache_ttl` a config value.
- Redact long digit runs before sending (SPEC rule) and send only text. Anthropic data-retention terms for API traffic were **not** researched here (see risks).

### 5.4 Fallback chain
Anthropic -> DeepL (free tier 500,000 chars/month, covers about 28 busy hours or 185 light hours per month by my numbers) -> null (show original, status amber). Optional later: local quickmt adapter as "offline" provider, after a CPU benchmark and a chat-slang quality check.

## 6. Cost model

### 6.1 Assumptions (all stated, all adjustable; nothing here is measured)

| Parameter | Light room | Busy room | Why |
|---|---|---|---|
| New unique lines/min reaching the translator (after dedupe + SQLite cache) | 3 | 20 | assumption; "active chat hour" = 60 continuous minutes |
| Lines/hour | 180 | 1,200 | x60 |
| Chinese chars per line (sender name excluded) | 15 | 15 | assumption; chat lines are short; UI labels are 2-6 |
| Source tokens per line | 20 | 20 | 15 chars x ~1.3 tokens/char (new tokenizer, about 30% more than old; guess; verify with count_tokens) |
| JSON wrapper per line (in) | 12 | 12 | ids/braces; assumption |
| English output tokens per line (incl. JSON) | 26 | 26 | about 12 words + wrapper; assumption |
| Effective batch size (lines/request) | 1.5 | 4 | bursts from scrolling; assumption |
| Requests/hour | 120 | 300 | lines / batch |
| Static prefix (system + glossary) | 1,500 tok | 1,500 tok | assumption; well above 512 minimum |
| Prefix cache hit rate | 95% | 95% | 5-min TTL stays warm at 1 req per 12-30 s; 5% misses = rewrites after gaps/glossary edits (priced at 5m write) |
| Context lines (K=10) | 360 tok | 360 tok | 10 x (20 src + 16 en); uncached |
| Fixed output overhead / request | 10 tok | 10 tok | wrapper |
| Thinking | off or `low`, about 0 billed | same | see sensitivity for default |

Per request, Haiku 5.5 (units: USD x 1e-6; price per MTok equals micro-dollars per token):
- Cached prefix: 1,500 x (0.95 x 0.01 + 0.05 x 0.125) = 1,500 x 0.01575 = **23.6**
- Uncached input = context + batch lines x (20 + 12). Light: (360 + 1.5x32 = 408) x 0.10 = **40.8**. Busy: (360 + 4x32 = 488) x 0.10 = **48.8**
- Output. Light: (1.5 x 26 + 10 = 49) x 0.50 = **24.5**. Busy: (4 x 26 + 10 = 114) x 0.50 = **57.0**
- Per request: light **88.9**, busy **129.4**. Per hour: light 120 x 88.9 = **$0.0107**; busy 300 x 129.4 = **$0.0388**.

Same method, Sonnet 5.5 ($2 / $2.50 write / $0.10 read / $10):
- Light: prefix 1,500 x (0.95x0.10 + 0.05x2.50 = 0.22) = 330; input 408 x 2 = 816; output 49 x 10 = 490; total 1,636 per request, x120 = **$0.196/h**.
- Busy: 330 + 488 x 2 (976) + 114 x 10 (1,140) = 2,446, x300 = **$0.734/h**.

DeepL / Google (characters only, 15 chars/line): light 2,700 chars/h, busy 18,000 chars/h.
- Google NMT at $20/M: light $0.054/h, busy $0.36/h (before the free 500k).
- DeepL Pro at $25/M (unverified): light $0.068/h, busy $0.45/h, plus the base fee (about $5.49/month, unverified).

### 6.2 Results

| Engine | Light room (180 lines/h) | Busy room (1,200 lines/h) | Per 120 h/month (4 h/day), light / busy |
|---|---|---|---|
| Haiku 5.5, caching, thinking off/low | **$0.011/h** | **$0.039/h** | $1.28 / $4.66 |
| Haiku 5.5, no caching (same assumptions) | $0.026/h | $0.077/h | $3.10 / $9.20 |
| Haiku 5.5, default thinking (+300 output tokens/request) | $0.029/h | $0.084/h | $3.44 / $10.06 |
| Haiku 5.5, busy room but batch size 1 (1,200 req/h) | n/a | $0.097/h | n/a / $11.64 |
| Sonnet 5.5, caching | $0.196/h | $0.734/h | $23.5 / $88.1 |
| Google NMT $20/M | $0.054/h | $0.36/h | $0 (324k chars, inside the free 500k) / about $33 ((2.16M - 0.5M) x $20/M) |
| DeepL Pro $25/M (unverified) + base | $0.068/h | $0.45/h | about $8.2+5.5 / $54+5.5 |
| DeepL API Free | $0 for up to 500k chars/month | about 27.8 busy hours/month, then blocked | $0 within quota |

(No-cache calc: light (1,500 + 408) x 0.10 + 24.5 = 215.3 x 120 = $0.0258; busy (1,500 + 488) x 0.10 + 57 = 255.8 x 300 = $0.0767. Thinking row adds 300 x 0.50 = 150 per request. Batch-1 busy row: uncached (360+32) x 0.10 = 39.2, prefix 23.6, output (26+10) x 0.5 = 18, total 80.8 x 1,200 = $0.097.)

### 6.3 What the numbers say
- Haiku 5.5 is roughly 10x cheaper per line than DeepL/Google for chat-length lines ($32e-6 vs $375e-6 per busy-room line) because those bill per character and Haiku bills per token at $0.10/MTok. The old assumption "LLM is the expensive option" is no longer true for Haiku-class pricing.
- Caching saves about 60% at these sizes, but even uncached Haiku is under 10 cents/hour busy. The bigger cost levers are: leaving thinking at default (2.2x), and using Sonnet (19x).
- `max_requests_per_hour = 600` in the spec would throttle a busy room at batch size 1 (1,200 req/h). Use a limit that reflects batch behaviour, or raise to about 1,500, and rely on a spend guard instead.
- RPM limits are irrelevant (5-20 RPM vs 1,000 RPM Start tier). A bigger practical risk is the first-days Evaluation tier and the spend-cap 429 described above.
- Sensitivity not modelled: if Chinese tokens are 2 per char instead of 1.3, uncached input rises about 35% and Haiku busy cost goes to roughly $0.045/h. Still negligible.

## 7. Recommendation

**Primary: Anthropic `claude-haiku-5-5`**, structured outputs (json_schema), static system prompt + glossary cached, context lines uncached after the breakpoint, `effort: "low"` (or thinking disabled; select by golden test), SQLite text cache in front. Expected cost about $0.01-0.04 per active hour (about $1-5/month at 4 h/day).
**Fallback 1: DeepL API Free** (500k chars/month, glossary + `context`), then `null`.
**Optional upgrade path, config only:** `claude-sonnet-5-5` for the explain mode and the Phase-6 reply composer (where tone and creativity matter and volume is low), or as a user-selectable "quality" model. Never as the live default (about 19x cost).
**Optional later adapter:** local quickmt-zh-en via CTranslate2 for "works offline" mode, only if a CPU benchmark and a chat-slang check justify it.

Config suggestions for the architect (values live in `default.toml`, not code): `model = "claude-haiku-5-5"` (already right), add `effort = "low"`, `thinking = "default"|"disabled"`, `cache_ttl = "5m"`, `batch_max_lines`, `max_requests_per_hour` raised, `monthly_budget_usd` soft limit with a tray warning, `startup_check_models = true` using the Models API list endpoint.

Spikes needed before the contract is frozen (all need the user's key, run by the user or the translation-engineer with keyring, never printed):
1. 100-line zh sample through Haiku 5.5 at `low` vs thinking disabled vs default: record p50/p95 latency, `usage` token fields, and golden-set quality vs Sonnet 5.5.
2. `count_tokens` on 50 real chat lines to replace my 1.3 tokens/char guess.
3. Confirm `cache_read_input_tokens > 0` on the second call, and that the json_schema does not break caching across calls.

## 8. Rejected

- **Sonnet 5.5 as default:** works, but 19x Haiku's cost and cannot disable thinking; keep as optional.
- **Haiku 4.5:** 10x price of Haiku 5.5, 4,096-token cache minimum (the glossary might not even qualify); only a rollback option if 5.5 misbehaves.
- **Opus 5.5 / Fable 5.1:** $4-$10 input per MTok and slower; no benefit for short chat lines.
- **Message Batches API:** 50% off but asynchronous (up to hours); incompatible with real-time lines.
- **Google NMT / Translation LLM as primary:** 10x costlier per line than Haiku, needs a GCP project and billing, no style/glossary-by-prompt control; keep only as a possible third adapter.
- **DeepL Pro as primary:** per-character pricing makes it dearer than Haiku and slang handling is glossary-only; good as fallback on the free tier.
- **NLLB (600M/1.3B):** slow in the only benchmark available, non-commercial licence flag, large; not lightweight.
- **Local LLM (Qwen etc.):** needs GPU/GBs of RAM, violates "lightweight CPU-only"; no benchmark found.
- **opus-mt-zh-en:** lowest quality of the offline set tested (BLEU 22.99).

## 9. Risks and unknowns

1. **Haiku 5.5 is 3 weeks old.** Real latency, slang quality and refusal rate on gaming chat are unmeasured. Mitigation: golden set run on Haiku vs Sonnet before approval; model ID in config; fallback to DeepL; keep Sonnet/Haiku 4.5 selectable.
2. **Safeguard refusals** (`stop_reason: "refusal"`, no server-side fallback, repeat refusals likely). Chat can contain adult or violent text. Mitigation: treat as per-batch failure, split the batch and retry per line once, then fall back and mark the line "[not translated]".
3. **Thinking tokens.** Default-on adaptive thinking quietly multiplies output cost and latency, and "answer directly" prompts do not stop it. Mitigation: explicit `effort` and the thinking setting in config; log `usage` per call.
4. **Cache silently not applying** (prefix below 512 tokens, any volatile text in the prefix, changed schema or effort). Mitigation: log `cache_read_input_tokens`; contract test that two identical-prefix calls hit cache; deterministic glossary rendering.
5. **Spend-cap 429 without `retry-after`** and Evaluation-tier limits for brand-new orgs (numbers unpublished). Mitigation: distinguish `enforced_spend_limit_reached`, do not retry, show the amber status; soft monthly budget in config.
6. **DeepL Pro price unverified** and the support article was blocked (403). Free-tier fallback numbers rely on the official "500,000 characters" statement only. Re-check before documenting costs to the user.
7. **Token estimates are assumptions** (1.3 tokens/char, line length, lines/min, batch sizes). Costs scale linearly; even 5x worse keeps Haiku under $0.25/h busy.
8. **Privacy/retention** of Anthropic/DeepL/Google API traffic not researched. The spec sends only redacted text, but the user chats may contain other people's messages; need a short privacy note and a "disable cloud translation" switch. Open for a follow-up research item.
9. **Offline mode** has no CPU benchmark and no chat-quality evidence; quickmt metrics are self-reported on FLORES. Treat as exploratory.
10. **Docs drift.** All model IDs, prices and limits above are as of 2026-10-30. The startup Models-API check in SPEC section 5 stays essential.

## 10. Sources (all checked 2026-10-30)

- Anthropic, Models overview: https://platform.claude.com/docs/en/about-claude/models/overview (redirected from docs.claude.com)
- Anthropic, Claude Haiku 5.5: https://platform.claude.com/docs/en/models/haiku-5-5/overview
- Anthropic, Pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Anthropic, Prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Anthropic, Structured outputs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- Anthropic, Effort: https://platform.claude.com/docs/en/build-with-claude/effort
- Anthropic, Prompting Claude Haiku 5.5: https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-haiku-5-5
- Anthropic, Rate limits: https://platform.claude.com/docs/en/api/rate-limits
- DeepL, Pro/API page: https://www.deepl.com/en/pro-api?cta=header-pro-api/
- DeepL, Usage limits: https://developers.deepl.com/docs/resources/usage-limits
- DeepL, Translate API reference: https://developers.deepl.com/api-reference/translate/request-translation
- DeepL support, usage count and billing (403 for me; cited via search snippet only): https://support.deepl.com/hc/en-us/articles/360020685720
- Google, Cloud Translation pricing: https://cloud.google.com/translate/pricing
- quickmt-zh-en model card: https://huggingface.co/quickmt/quickmt-zh-en
- Personal zh/en model comparison (anecdotal): https://whynothugo.nl/journal/2025/11/02/translation-models-between-english-and-chinese/
- Third-party DeepL Pro price quotes (weak evidence, unverified): search results for "DeepL API Pro pricing per million characters", e.g. https://langbly.pages.dev/blog/deepl-api-pricing-guide
