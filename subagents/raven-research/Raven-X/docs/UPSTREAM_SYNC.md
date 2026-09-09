# Upstream provider-layer sync ledger

Raven-X's `raven/providers/**` is a fork of upstream
[Raven](https://github.com/EverMind-AI/Raven). This file records where the fork
stands relative to upstream, so the next alignment is a diff against a pinned
commit instead of archaeology.

## Sync state

| Field | Value |
|---|---|
| Last aligned to | upstream `5edcda9` (2026-08-19) |
| Previous fork base | `6683483^` (2026-07-23, located by blob matching — never recorded at the time) |
| Aligned scope | `raven/providers/**` plus the consumer seams it forces (config schema, `cli/_helpers`, `token_wise` cache/pricing, `update_providers`, onboard/status/TUI/semconv surfaces) |

Next alignment procedure:

```bash
git -C <upstream-clone> diff 5edcda9..HEAD -- raven/providers/ tests/
```

then re-check every entry in the two tables below.

## Excluded upstream modules

Deliberately not ported. Do not "complete" the port by adding them without a
decision.

| Module | Why excluded |
|---|---|
| `providers/pool.py`, `providers/binding.py`, `providers/per_model_provider.py` | Conversation-level model-binding product feature; Raven-X batches bind one model per arm at launch, the feature adds a second model-resolution path the measurement must not have |
| `providers/lazy.py` (`LazyProvider`) | TUI startup-latency optimization; nothing in this fork wired it (batches build the provider eagerly), so it was ported dead and then removed (2026-08-19) |
| `capabilities.py` vision/image half (`supports_vision`, `vision_verdict`, `supports_image_tool_result`, `image_placeholder_text`) + `ProviderSpec.vision_override` / `.image_tool_result_override` | Image-in-tool-result product feature; no tool in this fork emits image blocks, so every probe was dead. Only `wire_overrides` is kept |
| `ErrorClassification.should_drop_tool_images` + the `tool_image_unsupported` classify branch | Recovery path for the image feature above; without an image sender it could never fire |
| `base.parse_llm_error` | Upstream's CLI diagnosis renderer consumes it; this fork has no such surface. `format_llm_error` (the writer) is kept — the canonical error shape is still asserted in tests by direct string match |
| `config.schema.ModelOverlay` + `ProviderConfig.model_overlay` + `catalog.describe(overlay=...)` | Model-picker labelling feature; no surface in this fork passes an overlay |
| `rates.effective_context_window` | Front-door not called here: `AgentLoop` resolves via `resolve_context_window` and applies its own configured-window precedence |
| `rates.warm_catalog_in_background` / `openrouter_input_modalities` / `_cached_catalog_only` | Existed to feed `supports_vision`; dead with it. The fetch still writes `input_modalities` into the cache row so the disk shape matches upstream |

Deliberately ported but left unwired (kept, not deleted — the writers are live
and the fields are useful log diagnostics):

- `LLMResponse.truncated` / `.max_tokens`, `RunMeta` / `TruncationInfo` /
  `truncation.flag_truncation`: both chat paths *write* these, but the refusal
  consumer upstream has (`Tool.truncation_hint`, registry-side refusal of a
  cut tool call) is deliberately not wired — wiring it would change batch-arm
  tool dispatch and is a separate labelled measurement round.

## Local deviations from upstream

Preserved on purpose; each is measurement-critical. Re-apply (and re-verify)
on every future sync.

| Deviation | Where | Why |
|---|---|---|
| `GenerationSettings.repetition_penalty`, forwarded via `extra_body` in both `chat` and `chat_stream` | `providers/base.py`, `providers/litellm_provider.py` | sglang/vLLM arms tune it; upstream has no field |
| `requestTimeoutSeconds` config key → `GenerationSettings.timeout` | `config/schema.py`, `cli/_helpers.py` | batch launcher sets per-arm timeouts; a timeout cap is censoring, keep it explicit |
| OpenRouter `routing` pin on `ProviderConfig` | `config/schema.py`, `cli/_helpers.py` | upstream deleted it; batches depend on provider-routing pinning to keep the served snapshot fixed |
| Reasoning-off wire override dropped when the user pinned `reasoningEffort` | `cli/_helpers.py` | the unconditional upstream override would silently cancel a pinned effort setting |
| `context_window_tokens` fallback stays 131_072 with a warning | `config/schema.py` | effective-window changes invalidate cross-model elision comparisons |
| Loop clamp cap falls back to 4096 when `GenerationSettings.max_tokens` is unset | `agent/loop/main.py` | upstream default became `None`; `or 0` would disable the reactive clamp |
| `refresh_models_dev_snapshot.py`: tarball downloaded by resolved sha (not branch name), 1 MiB gate runs before the write, inheritance resolver does not cache cycle-truncated rows | `scripts/refresh_models_dev_snapshot.py` | three upstream bugs fixed locally (provenance race, repo §7 gate, silent stale cache); candidates to send upstream |

### Known upstream quirks, ported as-is (deliberately not fixed here)

- `minimax_oauth.login()` holds the region's file lock across the whole
  device-code wait (up to the vendor deadline), so a concurrent `get_token`
  refresh in the same region blocks up to portalocker's 600s timeout. Narrow
  (needs a simultaneous interactive login and an active refresh) and the wide
  lock is also what serializes concurrent logins; not worth deviating in a
  live auth path.
- The same poll sleeps one full interval (~6s) before its first token check,
  so a user who approves quickly still waits. Cosmetic.
- The token poll exchanges by `user_code` + PKCE verifier rather than RFC
  8628's `device_code` -- that is MiniMax's own protocol, not a porting bug
  (hermes-agent and OpenClaw poll with the identical body); see the comment
  at the poll site.

### litellm upgrade procedure

`litellm==1.85.0` is an exact pin because this fork depends on three private
surfaces: `llms.chatgpt.authenticator`, `llms.github_copilot.authenticator`
(monkeypatched method names in `providers/chatgpt_token.py`), and
`provider_list` (snapshotted in `providers/litellm_provider_names.py`).
To bump the pin:

1. regenerate `litellm_provider_names.py` against the new version;
2. re-verify the two stubbed method names in `chatgpt_token.py`
   (`_login_device_code`, `_wait_for_access_token`) still exist -- the
   hasattr guard fails loudly if not;
3. run `tests/test_provider_resolution_invariants.py` (provider-list equality
   guard) and `tests/test_provider_chatgpt_token.py`.

## Measurement notes (for the next batch launched from this build)

- Any batch from this branch needs a **fresh anchor pair**: `chat_stream` now
  reads generation settings (was literal `max_tokens=4096` / `temperature=0.7`),
  and wire ids changed shape — batch configs need zero edits, but the reference
  frame moved.
- Direct-vendor wire ids are now prefixed: the request LiteLLM sees for
  `claude-sonnet-5` is `anthropic/claude-sonnet-5` (same route, new spelling in
  traces/logs — grep patterns keying on the bare id must be updated).
- Prompt-cache placement now falls back by wire family when a provider spec is
  silent; the `custom` gateway still resolves to **no** `cache_control`
  (sglang arms unchanged — verified).
- An arm pinning `maxTokens` above the model's resolved output ceiling is
  silently clamped by `send_max_tokens`. The ceiling is per model, not a
  floor: a trusted low catalogue row wins (`deepseek/deepseek-chat` resolves
  to 8192, so a 16384 pin sends 8192), and only an *untrusted or missing* row
  falls back to 16384. In-use arms (8192) are unaffected, but before pinning
  higher on any model, check `rates.resolve_max_output_tokens(model)` first.

## Commit-layer note

The alignment landed as layered commits for review granularity, atomic by
layer rather than by runnable state: the providers-core commit (`c7ccf4d`)
does not run standalone -- it removes module-level
`registry.supports_prompt_caching` while two `token_wise` consumers still
import it, fixed one commit later in the consumer-seams layer. Deliberate
(this repo keeps a single squashed history and does not bisect); only the tip
is suite-green.

## Test-layer notes

- Upstream contract/invariant tests adopted under `tests/` (provider catalog,
  wire-model baseline, resolution invariants, endpoints/rotor, rates, etc.).
  `tests/test_provider_catalog.py` is edited: the per-model-provider cases are
  removed with the excluded modules.
- `tests/test_agent_flow_dr.py` is the flow/anchor contract and is not part of
  any sync; it must stay green untouched.
