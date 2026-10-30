# ADR-0002: Contracts version 1.0.0

- Status: **Proposed**, pending user approval at Checkpoint 2
- Date: 2026-10-31
- Code: `src/gloss/core/contracts/`

## Context

Everything external sits behind a contract, and contracts freeze at Checkpoint 2. After that, a change needs the user's unlock and may touch every adapter. So 1.0.0 must cover what ADR-0001 and the engineers' briefs already require. SPEC section 3 was a draft. It left several things undecided: where translator token usage goes, which space bboxes use, how an overlay learns a line's position, how an adapter gets its settings and key, and when models load.

## Options

1. Keep the SPEC draft and add fields only for the six ADR-0001 items.
2. Finalise the draft, and also close the gaps the known adapters would otherwise hit in Phase 3.
3. Put every internal stage, including ChangeDetector and TextTracker, behind ports.

## Decision

Option 2. The six ADR-0001 items:

| Item | Contract |
|---|---|
| Stable monitor IDs | `MonitorInfo.monitor_id`, from the Windows device name, plus `CaptureSource.list_monitors()` |
| Optional freshness | `Frame.fresh = True` |
| Per-line failure | `TranslationResult.status` and `failure`. A failed line does not raise |
| Token usage | `TranslationResponse.usage` as `TokenUsage` |
| Unknown confidence | `TextBlock.confidence: float | None` |
| Low-quality marker | `TextBlock.low_quality`, carried by `TranslationItem` and `RenderItem` |

Changes from the SPEC draft, with the reason for each:

| Change | Reason |
|---|---|
| `Translator.translate` returns `TranslationResponse`, not a list | Usage belongs to the call, not to a line, and must survive partial failure |
| A translator makes one provider call and never retries | Retry, split and breaker live in one place, the TranslationService, and every billed call is reported |
| `open()` and `close()` on every adapter | Model load errors surface at a known moment, and profiles and `quickmt` can unload |
| `CaptureTarget` passed to `CaptureSource.open` | SPEC's `cfg` was undefined. Region and window change at runtime |
| Three named coordinate spaces | `TextBlock.bbox` is frame space, `RenderItem.bbox` is desktop space |
| `RenderItem` replaces bare results in `Renderer.show` | The overlay needs each line's position, and a batch can mix frames |
| `Renderer.set_status(health, banner)` | The panel shows the status dot and the permanent low-quality banner |
| `AdapterContext` for every constructor | Adapters get plain settings and secrets without importing `core` or `services` |
| `SecretStore`, `TranslationCache`, `SpendLedger` ports | CLAUDE.md rule 1 puts storage behind an interface, and adapters may not import keyring wrappers |
| `TextBlock.needs_translation`, `group_id` | The OCR brief asks for noise lines to be marked and message lines to be grouped |
| `TranslationResult.rough`; `status="passthrough"` | The interface labels `quickmt` and `null` output without knowing provider names |
| `ContextLine` and `TranslationItem` replace `str` and tuple | Context carries source and English, and items gain `low_quality` |
| Tuples, not lists | Frozen objects stay immutable all the way down |

`Frame` compares by identity, because comparing numpy arrays with `==` raises an error. It checks its shape on creation.

ChangeDetector, TextTracker, BudgetGuard and the HotkeyService stay out of the contracts. They have no external engine to swap, so freezing them would only slow Phase 3.

Decisions from the contract test review (2026-10-31):

| Question | Decision |
|---|---|
| How are storage ports tested, with no registry entry? | **Accept the test-side convention:** a class under `gloss.services` sets `CONTRACT_PORT` to `secret`, `cache` or `ledger`, and offers a classmethod `for_contract_test(path)`. Config never chooses these, so a registry entry would add machinery with no user. A `SecretStore` must use a throwaway backend there, so tests can never touch the user's real key |
| Deleting a missing secret | Does nothing and must not raise. `get` returns None for a missing secret. The first-run flow can then remove a key without checking first |
| A requested language the engine cannot read | `ports.py` now states it. `open()` raises `AdapterUnavailable` for something missing on this PC, and `PermanentError` for a code the engine can never read, such as `zz`. Skipping the language silently is not allowed |
| Type of `AdapterContext.secrets` | **`SecretStore | None`, not `Any`.** `AdapterContext` moved from `models.py` to `ports.py`, which avoids a circular import. It is still imported from `gloss.core.contracts` |
| Which translators send text off the PC? | A translator that stays on the PC declares the class attribute `LOCAL_ONLY = True`. Missing means it may send text off the PC, so a new adapter is treated as cloud until it says otherwise. `resolve_chain(..., local_only=True)` drops all others for the translate port, primary included. `null` and `quickmt` must declare it in their own class body, because it is not inherited: a cloud translator built on a local one must not pass as local. `local_only` is required for the translate port: leaving it out raises `TypeError`, so a forgotten argument cannot fail open. The pipeline passes `not config.privacy.cloud_translation`. One method stays, with the argument unused by other ports, so OCR and capture calls need no change. The check happens when called, not in the type signature |
| What may storage ports raise? | Only `GlossError` subclasses. Backend exceptions are wrapped |
| Is `ContextLine` redacted? | Yes, both texts, like `TranslationItem.text`. The unredacted original is only for local display |

## Consequences

- Translator adapters must set SDK retries to 0.
- Translator adapters return failed lines in truncation and refusal cases. The TranslationService does the batch split.
- All capture adapters must share one way of producing monitor IDs. This is a helper in `services/`, which does not break the no-adapter-imports-adapter rule.
- The registry imports every adapter module at startup, so heavy libraries must be imported inside `open()`.
- Some `.claude/agents/` text is now out of date (see open items in the Phase 2 report). Those files are the user's to change.

## Revisit when

- An adapter cannot meet a port without a workaround.
- Exclusive fullscreen or HDR capture needs metadata that `Frame` lacks.
- The Phase-6 reply composer or explain mode needs a new port.
