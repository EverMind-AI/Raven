# Tracing Standard API — v1 (draft)

The contract between **raven** (and any other adopter) and **raven-tracing**.

Principle: *tracing owns the standard, the app adopts it.* raven-tracing defines
what a span is, which fields each span kind carries, and how it renders. An app
(raven) instruments itself by calling one small, stable facade — `trace.span(...)`
— at points it chooses. Neither side depends on the other's internals; the only
coupling is this API's version + the semantic conventions below.

This mirrors the OpenTelemetry model (library defines the API + data model; the
app does manual instrumentation), so the same discipline applies: the API is
tiny and slow-moving, the SDK behind it (storage, viewer, on-disk format) iterates
freely without touching adopters.

Related: the on-disk record shape (`audit.span.v1`) is defined in
`raven/tracing/spans.py` (`build_span`) and summarized in §2; this document is
the **write-side** standard that produces those records.

---

## 1. Public API

One import, one primary call:

```python
from raven_tracing import trace

with trace.span("llm.call", {"llm.provider": provider, "llm.model": model}) as s:
    resp = do_call(...)
    s.set({"llm.usage.total_tokens": resp.usage.total, "llm.finish_reason": resp.finish_reason})
```

### `trace.span(name, attributes=None, *, kind=None, **kw) -> Span`

A context manager that opens a span on enter and finalizes + records it on exit.

- `name` — dotted semantic name, `<domain>.<verb>` (e.g. `llm.call`). Drives the
  default `kind` and the viewer's label/rendering (see §2).
- `attributes` — a mapping of fully-qualified dotted keys (the standard form,
  e.g. `{"llm.provider": p, "llm.model": m}`). Standard keys are dotted, so a
  mapping is the primary form; `**kw` accepts bare keys for convenience (stored
  verbatim, no auto-namespacing — the attribute namespace can differ from the
  name domain, e.g. `session.turn` carries `turn.*`).
- `kind` — optional override of the coarse category
  (`session|model|tool|subagent|skill|memory|plugin`). Default derived from the
  name's domain; pass explicitly for custom nodes (§3).

Nesting is automatic via `contextvars`: a span opened while another is active
becomes its child; context survives `await` and is snapshotted onto tasks. The
root of a turn is a `session.turn` span; everything else nests beneath it.

### `Span` handle

| method | effect |
|---|---|
| `s.set(attrs=None, **kw)` | merge attributes onto the span (dotted-key mapping and/or bare kwargs) |
| `s.artifact(key, payload, *, kind="json")` | persist a large payload out-of-line; attach `<key>.artifact_path/_sha1/_bytes` + a truncated `preview`. Use for prompts / tool IO / recall results. |
| `_spans.address_items(items)` | content-address a list of messages under `audit-artifacts/_messages/`; returns `{"$msg": sha1}` references to embed in a payload. See `audit.artifact.v2` below. |
| `s.event(name)` | append a timeline event `{time, name}` |
| `s.error(exc)` | mark `status = ERROR` (done automatically if the block raises) |

Read-only: `s.trace_id`, `s.span_id`, `s.name`.

### Module helpers

| call | returns |
|---|---|
| `trace.enabled()` | whether recording is on (config/env) |
| `trace.current()` | the active `TraceCtx` or `None` |
| `trace.use_context(ctx)` | context manager: re-enter a context captured with `current()`, for a span opened outside the turn that scheduled the work (a queue drained by a long-lived worker) |

### Hard guarantees (why an adopter is safe)

1. **No-op when off.** If disabled (config) or no SDK backend is active,
   `trace.span(...)` yields a no-op handle: no I/O, near-zero overhead, the
   `with` block runs normally.
2. **Never breaks the caller.** The facade swallows *its own* failures (bad
   attribute, disk error, SDK bug) and logs at debug level. It re-raises the
   *application's* exception unchanged (after recording `status=ERROR`). A
   tracing bug can never alter or crash the host's control flow.
3. **Import-safe.** Importing `raven_tracing` and calling the API must succeed
   even with no config present.

### `@trace.instrument(...)` — the decorator (primary adopter mechanism)

Adopters instrument a method by annotating it — the body is untouched, so this
does not change core logic (only adds an observation wrapper)::

    @trace.instrument("llm.call", extract=semconv.llm_call)
    async def chat_with_retry(self, ...): ...

`trace.instrument(name, *, kind=None, seed=None, on_open=None, extract=None)`
wraps a sync **or** async method:

- `extract(span, bound_args, result, exc)` — runs in `finally` (input captured
  even on error); fills final attributes/artifacts. `bound_args` is the call's
  arguments by name; `result` is the return (`None` on error); `exc` the raised
  exception (`None` on success). The standard extractors live in
  :mod:`raven.tracing.semconv` (`llm_call`, `tool_call`, `memory_*`, …).
- `seed(bound_args) -> dict` — returns `session_key` / `channel` / `chat_id` to
  open a *root* span (a turn) whose identity every child inherits.
- `on_open(span, bound_args)` — runs right after open, before the body; used to
  record input and `span.checkpoint()` an in-progress root for live viewing.

Extra `Span` methods used by extractors: `span.retype(name, kind)` (a `tool.call`
that turns out to be a `skill.read`), `span.cancel()` (drop a conditional span,
e.g. `skill.inject` only when something was injected), `span.checkpoint()`,
`span.elapsed_ms()`. Pass `detached=True` for a leaf marker that does NOT become
the active parent — required for cancellable spans so a child that opened before
the cancel doesn't dangle off an unemitted span.

Every span family is instrumented this way — including `subagent.run`: a
subagent runs the same decorated primitives (`chat_with_retry` / `tools.execute`),
so its spans are captured by those decorators and nest under the `subagent.run`
node automatically via the contextvars snapshot `asyncio.create_task` takes at
spawn. No monkeypatch is used anywhere.

---

## 2. Semantic conventions (standard span kinds)

`kind` is a single-word category (drives node coloring/grouping). `name` is the
`<domain>.<verb>` identifier (drives the label + rendering). Attributes are
namespaced by domain. Adopters SHOULD populate the "required" columns; "optional"
adds richer rendering.

| name | kind | required | optional attributes |
|---|---|---|---|
| `session.turn` | `session` | — | `turn.input_preview`, `turn.output_preview`, `turn.in_progress`, `turn.capabilities.{tools,plugins,skills}` |
| `llm.call` | `model` | `llm.provider`, `llm.model` | `llm.provider_class`, `llm.finish_reason`, `llm.call_id`, `llm.invocation_source`, `llm.usage.{input,output,total,cache_read,cache_write}_tokens`, `llm.usage.cost_total`, `llm.request_{bytes,images,image_bytes}`, `llm.http_status`, `llm.served_by`, `llm.response_id`; artifacts `llm.input` (`audit.artifact.v2`: message references + tools, plus `request` = the messages+tools payload size and picture count, not the provider-final body, and `generation` = what the call asked the model for; see below), `llm.output` (plus `call` = the transport record) |
| `tool.call` | `tool` | `tool.name` | `tool.call_id`, `tool.duration_ms`, `tool.error`; artifacts `tool.input` (params), `tool.output` (result) |
| `subagent.run` / `subagent.call` | `subagent` | — | `subagent.id`, `subagent.label`, `subagent.task`, `subagent.session_id`, `subagent.parent_trace_id`, `subagent.parent_span_id`, `subagent.trace_id`, `subagent.status` |
| `skill.read` / `skill.inject` | `skill` | — | `skill.name`, `skill.id`, `skill.source`, `skill.path`, `skill.scripts_dir` (present => a runnable bundle was materialized, vs instructions-only), `skill.read.via_tool` (`use_skill`/`read_skill`/`read_file`), `skill.inject.{names,count,via}` |
| `memory.recall` / `.store` / `.feedback` / `.extract` / `.consolidate` / `.profile_refresh` | `memory` | — | `memory.scope`, `memory.hits`, `memory.message_count`, `memory.kind`, `memory.deposit_summary`, `memory.deposit_status`, `memory.surface`, `memory.sections_rewritten`; artifacts per op |
| `plugin.load` / `tracing.bootstrap` | `plugin` | — | `plugin.name`, `plugin.contribution`, `plugin.id` |

### Artifact formats

An artifact is either a plain JSON payload (v1, no discriminator) or
`audit.artifact.v2`, which carries `"artifactFormat": "audit.artifact.v2"` and
replaces each message with a `{"$msg": "<sha1>"}` reference to
`audit-artifacts/_messages/<sha1[:2]>/<sha1>.json`. Only `llm.input` is written
this way; every other kind stays v1, and a v1 artifact already on disk is never
rewritten. A shell's `request` and `generation` are carried verbatim: neither
holds message content, so referencing them would cost an indirection and save
nothing.

`raven/tracing/artifact_v2.py` is the format's single authority - canonical
serialization, envelope, reference validation, resolution - because the bundled
viewer mirrors it in JavaScript and a cross-language round-trip test compares
the two. A consumer that resolves a shell must apply two rules: `messages`
resolves to the list of message objects, while `systemPrompt` and `prompt`
resolve to their message's `content` coerced to text. A reference whose sha1 is
not 40 lowercase hex characters is data, not an address, and is passed through
untouched; a shell may also carry a raw message inline where its blob could not
be written.

For `llm.input`, `artifact_sha1` and `artifact_bytes` describe the shell, not
the conversation it references; `llm.request_bytes` is the request-size signal,
measured from the messages themselves and so unaffected by the shell. The shell
hashes over the references it lists, so tamper evidence localizes to one
message rather than only proving the payload changed.

Two resolution points turn a shell back into its v1 equivalent:
`raven/trajectory/bundle.py` when packing a trajectory (so a bundle carries no
dependency on the message store) and `readArtifact` in the bundled viewer's
`server.js` (so the UI never sees a reference). Consumers downstream of either
- replay, cassette minimization, the viewer's UI - read v1 shapes and need no
knowledge of v2.

**Provider labeling:** `llm.provider` is the *logical backend* the call routes to
(e.g. `openrouter`), derived from the model's gateway prefix; `llm.provider_class`
is the concrete class (e.g. `LiteLLMProvider`) when it differs. `llm.served_by` is a
third thing: the backend the *response* names as having served the call, present only
when the upstream says so, which behind a gateway that fans out is the only way to tell
which one answered.

**The transport record.** `llm.output`'s `call` object holds what the exchange did rather
than what the model said: `http_status`, `served_by`, `served_model`, `response_id`,
`headers`, and `body` -- the last filled only when the call delivered nothing, so a
usable answer is never stored twice. It is built solely by
`raven.providers.call_record`, which caps the body and the headers and replaces the value
of any credential-named header; nothing else may construct one. A request's own bytes are
never copied into a record: `llm.input`'s `request` object counts them
(`bytes` on the wire, `images`, `imageBytes` decoded) and image payloads are counted, not
logged.

Naming rules:
- `name` = `<domain>.<verb>`, lowercase dotted.
- attribute keys = `<domain>.<field>`, matching the span's domain.
- kind is a closed vocabulary: `session|model|tool|subagent|skill|memory|plugin`.

---

## 3. Custom nodes

Any adopter (or plugin) may record a custom span — no registration required:

```python
with trace.span("raven.sentinel.tick", {"sentinel.reason": r}, kind="plugin") as s:
    s.set({"sentinel.fired": n})
```

Rules:
- Use an **owned namespace** for `name` (`raven.<subsystem>.<verb>`) to avoid
  clashing with the standard names in §2.
- Pass `kind` explicitly (falls back to a generic node kind otherwise).
- The viewer renders unknown names generically (title from `name`, subtitle from
  a chosen attribute). For bespoke rendering, ship a **descriptor** entry
  (`descriptors/*.json`, keyed by `name`) — the viewer's rendering standard,
  shipped under `raven/tracing/viewer/descriptors/`.

---

## 4. Adopter integration contract (raven)

1. Declare `raven-tracing` as a default dependency (default-on extra
   `raven[tracing]`), so it ships with raven and survives `uv tool upgrade`.
2. `from raven_tracing import trace` at instrumentation sites; wrap the operation
   in `with trace.span(...)`. Instrumentation lives in the app's own code, moves
   with refactors, and is visible in diffs (no external monkeypatch to silently
   break).
3. Enable/disable via `[tracing].enabled` (raven config) or `RAVEN_TRACING=0`
   (env override). The API no-ops when disabled.
4. The app never imports the SDK internals (storage/viewer) — only the facade.

There is no monkeypatch / auto-instrumentation path: all instrumentation is the
explicit `@trace.instrument` annotations in raven's own source, so it moves with
the code and shows up in diffs (never silently breaks on a refactor).

---

## 5. Versioning & governance

- The API + semantic conventions are versioned together as `standard-api.v1`,
  independent of the app.
- **Additive** changes (new optional attribute, new span name/kind) → minor bump,
  backward compatible.
- **Breaking** changes (rename/remove an attribute or the API signature) → major
  bump + a migration note; adopters pin a supported range and warn (not silently
  degrade) on mismatch.
- A conformance snapshot test (frozen span names + required fields) guards the
  contract in CI; changing it without a version bump fails the build.
- On-disk record format is versioned separately as `audit.span.v1`
  (defined in `raven/tracing/spans.py`); the two move independently.

---

## Status

In-tree, complete. Every span family (turn / llm / tool / memory / skill.inject /
plugin.load / subagent) is instrumented with `@trace.instrument` on raven's own
methods; there is no monkeypatch and no `instrument.install()` — the auto-probe
module was removed. `semconv.py` owns the standard attribute/artifact builders.

Remaining for the standalone-OSS phase (P4), none blocking in-tree use:
- Make the import optional: raven core hard-imports `raven.tracing` at module load
  (decorators applied at class-definition time), so it must ship with raven; a
  no-op fallback shim is needed before tracing can be a truly optional extra.
- Move the raven-specific extractors (`semconv.py`) to raven's side; keep only the
  generic API + schema + viewer in the standalone package.
- Drop raven branding from the standalone package (`FRAMEWORK`, `RAVEN_*` env,
  `~/.raven` paths) and the `raven.config`/`raven.token_wise` soft imports.
- Freeze `standard-api.v1`; publish `raven-tracing`; raven default-depends on it.
