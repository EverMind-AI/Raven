# Per-message content addressing for llm.input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Fully executed. This document is the historical planning record,
not an open work order: do not re-execute the checkboxes below. Five places
where the shipped code is ahead of the plan text, each found by running it:

- **Task 1, canonical serialization.** The plan specified `sort_keys=True`. The
  round-trip test caught it on the first run: sorting stores a key-reordered
  copy, and `TEXT_FIELDS` carry key order inside a serialized string, so the
  round trip stopped being exact. Measured over 406,357 recorded occurrences,
  sorting changed the distinct count by zero (8,837 either way), so it was
  removed. The constraint block and the Task 1 code above carry the final rule.
- **Task 1's own test.** `test_address_is_independent_of_key_order` pinned the
  behaviour being removed; it is now
  `test_the_address_follows_the_bytes_not_the_semantics`, plus a new
  `test_round_trip_preserves_key_order_inside_content` aimed at the regression.
- **Task 6, the preceding commit's tests.** The plan named only the `not_a_list`
  assertion. Both tests that commit added assert v1 shape, so both were
  converted rather than deleted, preserving every assertion that still has
  meaning: `test_llm_input_payload_carries_the_call_identity_verbatim`,
  `test_llm_input_payload_gives_messages_one_constant_type`, and
  `test_the_system_field_takes_the_first_system_message_even_when_it_is_empty`
  (that last one records a deliberate inversion of the v1 rule).
- **Task 6, a dead import.** Moving `coerce_text` in Task 1 left `semconv`
  importing it; once Task 6 stopped lifting text, the import became unused and
  was removed. Text coercion now lives entirely on the read side.
- **Task 6 Step 6, the real-data check.** Driving a live turn would spend
  provider credit, so the check instead replays six real recorded
  conversations (989-999 messages each) through `semconv.llm_call`, the actual
  extractor path. Result: every round trip exact, span attributes present,
  content 23.80 MiB against 729.3 KiB of shells (97.01% per record; 4.68 MiB
  on disk against 23.80 MiB, 80.3%, for a six-artifact sample where the
  message corpus is amortized over only six records).

**Goal:** Store each distinct model-input message once on disk and have the
`llm.input` artifact reference it, taking the largest recorded artifact from
32.906 MiB to 0.063 MiB (99.8%) and store growth from ~4.3 GiB/day to
~65 MiB/day, without any change to `replay.py`, `cassette.py` or `app.js`.

**Architecture:** A new `raven/tracing/artifact_v2.py` owns the whole
`audit.artifact.v2` wire format - canonical serialization, the `{"$msg": sha1}`
envelope, reference validation, and resolution. `TraceStore.address_items`
publishes messages under `audit-artifacts/_messages/` reusing the existing
blob machinery; `semconv` builds a shell of references. Two resolution points
turn a shell back into a v1 payload: `bundle.py` when packing a trajectory,
and `server.js:readArtifact` when the viewer parses one. Because both sit at
boundaries that already reformat, every downstream consumer is untouched.

**Tech Stack:** Python 3.12 (stdlib only inside `raven/tracing/`), Node
(the bundled viewer), pytest.

**Spec:** `docs/specs/2026-09-10-llm-input-message-addressing-design.md`

## Global Constraints

- **The `historyMessages` drop must land first.** This plan replaces
  `_llm_input_payload` wholesale, so it applies either way, but every figure
  in the spec is measured against the post-drop payload.
- **`raven/tracing/` is stdlib-only.** `store.py`'s docstring states the
  package must stay self-contained for a clean `pip install`. `artifact_v2.py`
  inherits that: no third-party imports, no imports from elsewhere in `raven`.
- **The task order is reader-first on purpose.** Tasks 1-5 teach every reader
  to understand v2; only Task 6 makes the writer emit it. No intermediate
  commit may leave artifacts on disk that a reader cannot resolve. Do not
  reorder.
- **Canonical message serialization:**
  `json.dumps(message, ensure_ascii=False, default=str)` - key order preserved,
  NOT sorted. Sorting breaks the exact round trip (the text fields carry key
  order inside a serialized string) and was measured to reduce the distinct
  count by zero over 406,357 occurrences. Changing this re-addresses every
  message ever stored.
- **Envelope key is `"$msg"`; format discriminator is
  `"artifactFormat": "audit.artifact.v2"`.**
- **A reference's sha1 must be validated as 40 lowercase hex characters before
  any path is built from it**, on both the Python and the JavaScript side.
- **Source language is English** (AGENTS.md 1.3): comments, docstrings, test
  names, string constants. Comments only where the logic is non-obvious or a
  constraint is hidden (AGENTS.md 1.1) - do not annotate what the code says.
- **Tests run as `uv run pytest --all-extras ...`** (AGENTS.md 5.4). A bare
  `uv run pytest` silently drops ~4300 tests. In a worktree without its own
  `.venv`, run the main checkout's interpreter from the worktree directory
  instead: `<main>/.venv/bin/python -m pytest ...` - imports resolve to the
  worktree because cwd precedes the editable install on `sys.path`; verify
  once with `python -c "import raven; print(raven.__file__)"`.
- **`pytest` is configured with `xdist`.** Pass `-n0` for tests that spawn the
  Node viewer, and `-p no:randomly` when asserting on order.
- **Commit steps are not pre-authorized.** AGENTS.md 3.4: "'Commit per phase'
  written in a plan is not pre-authorization." Each commit step means *report
  and stop*; commit only when the user says so. Branch name and base follow
  AGENTS.md 2.1/2.2 and must be confirmed with the user before cutting.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `raven/tracing/artifact_v2.py` (new) | The entire v2 wire format: serialization, envelope, validation, resolution, the text-field rule. One file because JavaScript mirrors it and a cross-language test compares them. |
| `raven/tracing/store.py` (modify) | `address_items`: publish messages under `_messages/`, return references. Reuses `_publish_blob` / `_blob_is_intact` / `_repair_blob`. |
| `raven/tracing/spans.py` (modify) | `address_items` facade: `_get_store()` plus the bare `except Exception` every tracing entry point has. |
| `raven/observability/semconv.py` (modify) | `_llm_input_payload` builds a v2 shell; `llm_call` records `content_bytes`. `_coerce_text` moves to `artifact_v2`. |
| `raven/trajectory/bundle.py` (modify) | `_import_artifact` resolves a v2 shell instead of copying it; missing addresses reach the manifest. |
| `raven/cli/tracing_viewer/server.js` (modify) | `readArtifact` resolves after `JSON.parse`, so `app.js` is untouched. |
| `tests/test_tracing_artifact_v2.py` (new) | Format-module unit tests and the round-trip invariant. |
| `tests/test_tracing_api.py` (modify) | `address_items` write-path tests; the frozen v2 shell key set. |
| `tests/test_trajectory_bundle.py` (modify) | Bundle resolution, placeholder, manifest. |
| `tests/integration/test_tracing_viewer_e2e.py` (modify) | Viewer resolution and the cross-language round trip. |

---

### Task 1: The v2 format module

**Files:**
- Create: `raven/tracing/artifact_v2.py`
- Modify: `raven/observability/semconv.py:115-125` (move `_coerce_text` out)
- Test: `tests/test_tracing_artifact_v2.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces: `ARTIFACT_FORMAT: str`, `MESSAGES_DIR_NAME: str`, `REF_KEY: str`,
  `TEXT_FIELDS: tuple[str, ...]`, `message_text(Any) -> str`,
  `message_sha1(Any) -> str`, `make_ref(str) -> dict[str, str]`,
  `ref_sha1(Any) -> str | None`, `messages_dir(Path) -> Path`,
  `message_path(Path, str) -> Path`, `is_v2(Any) -> bool`,
  `coerce_text(Any) -> str`, `placeholder(str) -> dict[str, str]`,
  `resolve_payload(Any, Path, *, on_missing: Callable[[str], Any] | None = None) -> Any`.

**A decision this task must implement, with its reason.** `semconv._coerce_text`
serializes non-string content with `json.dumps(value, ensure_ascii=False)`,
whose default separators carry spaces: `{"a": 1}`. JavaScript's
`JSON.stringify` produces `{"a":1}`. A multimodal `prompt` (content is a list
of blocks) would therefore resolve to different bytes in Python and in the
viewer - the exact silent divergence Task 5 exists to catch. So `coerce_text`
moves into this module and switches to `separators=(",", ":")`, which
`JSON.stringify` matches exactly. The visible effect is spacing inside a JSON
dump shown for non-string prompt content; the gain is a language-neutral
format rule.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tracing_artifact_v2.py`:

```python
"""Contract tests for the audit.artifact.v2 reference format.

The round-trip test is the invariant the whole design rests on: addressing a
v1 payload and resolving the result must return the original, byte for byte.
"""

from __future__ import annotations

import json

import pytest

from raven.tracing import artifact_v2 as v2


def _msg(role: str, content):
    return {"role": role, "content": content}


def test_the_address_follows_the_bytes_not_the_semantics():
    """Key order is part of the address, deliberately - see ``message_text``."""
    a = {"role": "user", "content": "hi"}
    b = {"content": "hi", "role": "user"}
    assert v2.message_sha1(a) != v2.message_sha1(b)


def test_distinct_messages_get_distinct_addresses():
    assert v2.message_sha1(_msg("user", "a")) != v2.message_sha1(_msg("user", "b"))


def test_ref_sha1_accepts_a_well_formed_envelope():
    sha1 = "0" * 40
    assert v2.ref_sha1(v2.make_ref(sha1)) == sha1


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not a dict",
        {},
        {"$msg": "../../etc/passwd"},
        {"$msg": "ABCDEF" + "0" * 34},
        {"$msg": "0" * 39},
        {"$msg": "0" * 41},
        {"$msg": 1234},
        {"$msg": "0" * 40, "extra": 1},
        {"role": "user", "content": "a real message"},
    ],
)
def test_ref_sha1_rejects_everything_else(value):
    assert v2.ref_sha1(value) is None


def test_coerce_text_is_json_stringify_compatible():
    assert v2.coerce_text(None) == ""
    assert v2.coerce_text("plain") == "plain"
    # No spaces after ':' or ',' - JSON.stringify produces exactly this, and
    # the viewer must resolve a multimodal prompt to the same bytes.
    assert v2.coerce_text([{"type": "text", "text": "x"}]) == '[{"type":"text","text":"x"}]'


def test_resolve_leaves_a_v1_payload_untouched():
    v1 = {"provider": "p", "messages": [_msg("user", "hi")]}
    assert v2.resolve_payload(v1, "/nonexistent") is v1


def test_round_trip_returns_the_original_payload(tmp_path):
    msgs = [
        _msg("system", "you are raven"),
        _msg("user", "first"),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "name": "grep"}]},
        _msg("user", [{"type": "text", "text": "latest"}]),
    ]
    v1 = {
        "provider": "openrouter",
        "providerClass": "LiteLLMProvider",
        "model": "openrouter/x",
        "systemPrompt": v2.coerce_text(msgs[0]["content"]),
        "prompt": v2.coerce_text(msgs[3]["content"]),
        "messages": msgs,
        "tools": [{"function": {"name": "grep"}}],
    }

    refs = []
    for m in msgs:
        sha1 = v2.message_sha1(m)
        path = v2.message_path(tmp_path, sha1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(v2.message_text(m), encoding="utf-8")
        refs.append(v2.make_ref(sha1))
    shell = {
        "artifactFormat": v2.ARTIFACT_FORMAT,
        "provider": "openrouter",
        "providerClass": "LiteLLMProvider",
        "model": "openrouter/x",
        "systemPrompt": refs[0],
        "prompt": refs[3],
        "messages": refs,
        "tools": [{"function": {"name": "grep"}}],
    }

    resolved = v2.resolve_payload(shell, tmp_path)
    assert json.dumps(resolved, ensure_ascii=False, sort_keys=True) == json.dumps(
        v1, ensure_ascii=False, sort_keys=True
    )


def test_a_missing_blob_becomes_a_placeholder_at_the_same_index(tmp_path):
    kept = _msg("user", "kept")
    sha1 = v2.message_sha1(kept)
    path = v2.message_path(tmp_path, sha1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(v2.message_text(kept), encoding="utf-8")
    gone = "f" * 40

    seen = []
    resolved = v2.resolve_payload(
        {
            "artifactFormat": v2.ARTIFACT_FORMAT,
            "messages": [v2.make_ref(gone), v2.make_ref(sha1)],
        },
        tmp_path,
        on_missing=lambda h: (seen.append(h), {"role": "unknown", "content": "gone"})[1],
    )

    assert seen == [gone]
    assert len(resolved["messages"]) == 2
    assert resolved["messages"][1] == kept


def test_an_inlined_message_survives_resolution(tmp_path):
    inlined = _msg("user", "written inline because its blob failed")
    resolved = v2.resolve_payload(
        {"artifactFormat": v2.ARTIFACT_FORMAT, "messages": [inlined]}, tmp_path
    )
    assert resolved["messages"] == [inlined]


def test_text_fields_resolve_to_text_not_to_the_message_object(tmp_path):
    msg = _msg("system", "you are raven")
    sha1 = v2.message_sha1(msg)
    path = v2.message_path(tmp_path, sha1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(v2.message_text(msg), encoding="utf-8")

    resolved = v2.resolve_payload(
        {"artifactFormat": v2.ARTIFACT_FORMAT, "systemPrompt": v2.make_ref(sha1)}, tmp_path
    )
    assert resolved["systemPrompt"] == "you are raven"


def test_resolution_drops_the_format_discriminator(tmp_path):
    resolved = v2.resolve_payload(
        {"artifactFormat": v2.ARTIFACT_FORMAT, "provider": "p"}, tmp_path
    )
    assert "artifactFormat" not in resolved
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `<main>/.venv/bin/python -m pytest tests/test_tracing_artifact_v2.py -n0`
Expected: collection error, `ModuleNotFoundError: No module named 'raven.tracing.artifact_v2'`

- [ ] **Step 3: Write the module**

Create `raven/tracing/artifact_v2.py`:

```python
"""The ``audit.artifact.v2`` reference format: address, validate, resolve.

One module owns the whole wire shape because the bundled viewer mirrors it in
JavaScript (``raven/cli/tracing_viewer/server.js``) and the two must not
drift - a cross-language round-trip test compares this module's output against
the viewer's. The canonical serialization, the envelope key, the reference
validation and the per-field resolution rules all live here and nowhere else.

Stdlib only, like :mod:`raven.tracing.store`, so the tracing package stays
installable on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

ARTIFACT_FORMAT = "audit.artifact.v2"
MESSAGES_DIR_NAME = "_messages"
REF_KEY = "$msg"

# Fields whose reference resolves to the message's text rather than to the
# message object. ``app.js`` calls ``.match()`` on these, so handing it an
# object throws.
TEXT_FIELDS = ("systemPrompt", "prompt")

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def message_text(message: Any) -> str:
    """The canonical serialization a message's address is taken over.

    Key order is preserved, not sorted. Sorting would let two orderings of one
    message share a blob, but it also makes resolution hand back a reordered
    copy - and :data:`TEXT_FIELDS` carry that order baked inside a serialized
    string, so the round trip would stop being exact. Over 406,357 recorded
    message occurrences, sorting changed the distinct count by zero: messages
    are built by the same code path every turn, so the trade bought nothing.

    ``default=str`` differs from :func:`coerce_text`, which has no fallback
    encoder: a message carrying something unserializable is stored with that
    value coerced, where v1 would have stringified the whole content. Such a
    message cannot reach a provider, so the divergence is unreachable.

    This is part of the v2 contract: changing it re-addresses every message
    ever stored.
    """
    return json.dumps(message, ensure_ascii=False, default=str)


def message_sha1(message: Any) -> str:
    return hashlib.sha1(message_text(message).encode("utf-8")).hexdigest()


def make_ref(sha1: str) -> dict[str, str]:
    return {REF_KEY: sha1}


def ref_sha1(value: Any) -> str | None:
    """The address ``value`` references, or None when it is not a reference.

    Rejects anything but 40 lowercase hex characters before a caller can build
    a path from it: a shell is data, and ``../`` in this position would
    otherwise reach outside the message store.
    """
    if not isinstance(value, dict) or len(value) != 1:
        return None
    sha1 = value.get(REF_KEY)
    if not isinstance(sha1, str) or not _SHA1_RE.match(sha1):
        return None
    return sha1


def messages_dir(artifacts_dir: Path | str) -> Path:
    return Path(artifacts_dir) / MESSAGES_DIR_NAME


def message_path(artifacts_dir: Path | str, sha1: str) -> Path:
    return messages_dir(artifacts_dir) / sha1[:2] / f"{sha1}.json"


def is_v2(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("artifactFormat") == ARTIFACT_FORMAT


def coerce_text(value: Any) -> str:
    """The text a v1 ``systemPrompt`` / ``prompt`` field carried.

    ``separators`` is not cosmetic: JavaScript's ``JSON.stringify`` emits no
    space after ``:`` or ``,``, and the viewer resolves these same fields.
    Python's default separators would make a multimodal prompt resolve to
    different bytes on the two sides.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def placeholder(sha1: str) -> dict[str, str]:
    return {"role": "unknown", "content": f"[message blob missing: {sha1}]"}


def resolve_payload(
    payload: Any,
    artifacts_dir: Path | str,
    *,
    on_missing: Callable[[str], Any] | None = None,
) -> Any:
    """A v2 shell as its v1 equivalent; any other payload returned unchanged.

    ``on_missing`` supplies the stand-in for an address whose blob is gone and
    is called once per distinct missing address, so a caller can record them.
    A message is never dropped: consumers compare message-list length and
    position, so omitting one shifts every later message and turns a locatable
    gap into a spurious full divergence.
    """
    if not is_v2(payload):
        return payload
    missing = placeholder if on_missing is None else on_missing
    cache: dict[str, Any] = {}

    def load(sha1: str) -> Any:
        if sha1 not in cache:
            try:
                cache[sha1] = json.loads(message_path(artifacts_dir, sha1).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cache[sha1] = missing(sha1)
        return cache[sha1]

    def one(value: Any) -> Any:
        sha1 = ref_sha1(value)
        return load(sha1) if sha1 is not None else value

    out = {key: value for key, value in payload.items() if key != "artifactFormat"}
    if isinstance(out.get("messages"), list):
        out["messages"] = [one(item) for item in out["messages"]]
    for field in TEXT_FIELDS:
        if field in out:
            resolved = one(out[field])
            out[field] = coerce_text(
                resolved.get("content") if isinstance(resolved, dict) else resolved
            )
    return out
```

- [ ] **Step 4: Point `semconv` at the moved helper**

In `raven/observability/semconv.py`, delete `_coerce_text` (lines 115-125) and
add to the existing imports:

```python
from raven.tracing.artifact_v2 import coerce_text as _coerce_text
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `<main>/.venv/bin/python -m pytest tests/test_tracing_artifact_v2.py tests/test_tracing_api.py -n0`
Expected: PASS

- [ ] **Step 6: Report and stop (commit only when the user says so)**

```bash
git add raven/tracing/artifact_v2.py raven/observability/semconv.py tests/test_tracing_artifact_v2.py
git commit -m "feat(tracing): add the audit.artifact.v2 reference format"
```

---

### Task 2: `address_items` on the write path

**Files:**
- Modify: `raven/tracing/store.py` (add `messages_dir` attribute in `__init__`, add `address_items`)
- Modify: `raven/tracing/spans.py` (add the `address_items` facade)
- Test: `tests/test_tracing_api.py`

**Interfaces:**
- Consumes: `artifact_v2.message_text`, `message_sha1`, `make_ref`, `message_path`, `messages_dir`.
- Produces: `TraceStore.messages_dir: Path`,
  `TraceStore.address_items(items: list[Any]) -> list[Any]`,
  `spans.address_items(items: list[Any]) -> list[Any]` (returns `items`
  unchanged if the store cannot be reached).

**Why a separate method and not `persist_artifact(address_fields=...)`:** the
sha1 is computed inside the store, so a caller cannot fill a reference in
beforehand, and the store cannot know which field aliases which element.
`address_items` returns the reference list, and the caller aliases `refs[i]` -
the same object, so there is no second hashing site to drift.

**What is reused and what is not:** `_publish_blob`, `_blob_is_intact` and
`_repair_blob` are reused verbatim against the new directory. `_materialize`
is not: it exists to hard-link an artifact path onto a blob, and a message
blob has no second path - it is itself the only file. That is why the
hard-link ceiling cannot be reached here.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracing_api.py`:

```python
def test_address_items_publishes_one_blob_per_distinct_message(trace_dir):
    from raven.tracing import artifact_v2 as v2

    store = _store_mod.TraceStore(trace_dir)
    first = {"role": "system", "content": "sys"}
    second = {"role": "user", "content": "hi"}

    refs = store.address_items([first, second, first])

    assert [v2.ref_sha1(r) for r in refs] == [
        v2.message_sha1(first),
        v2.message_sha1(second),
        v2.message_sha1(first),
    ]
    written = sorted(p.name for p in store.messages_dir.rglob("*.json"))
    assert written == sorted([f"{v2.message_sha1(first)}.json", f"{v2.message_sha1(second)}.json"])


def test_address_items_writes_beside_blobs_never_inside(trace_dir):
    """Inside ``_blobs/`` the corpus would be swept: compact removes any blob
    whose link count is 1, and a message blob's is permanently 1."""
    store = _store_mod.TraceStore(trace_dir)
    store.address_items([{"role": "user", "content": "hi"}])

    assert store.messages_dir.parent == store.artifacts_dir
    assert store.messages_dir.name != store.blobs_dir.name
    written = list(store.messages_dir.rglob("*.json"))
    assert len(written) == 1
    assert store.blobs_dir not in written[0].parents


def test_a_message_blob_is_never_hard_linked(trace_dir):
    store = _store_mod.TraceStore(trace_dir)
    store.address_items([{"role": "user", "content": "hi"}])

    blob = next(store.messages_dir.rglob("*.json"))
    assert blob.stat().st_nlink == 1


def test_address_items_inlines_a_message_it_cannot_store(trace_dir, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(_store_mod.TraceStore, "_publish_blob", _boom)
    store = _store_mod.TraceStore(trace_dir)
    message = {"role": "user", "content": "hi"}

    refs = store.address_items([message])

    assert refs == [message], "the message itself, so the record stays complete"
    assert not list(store.messages_dir.rglob("*.json")) if store.messages_dir.exists() else True


def test_address_items_repairs_a_mutated_message_blob(trace_dir):
    from raven.tracing import artifact_v2 as v2

    store = _store_mod.TraceStore(trace_dir)
    message = {"role": "user", "content": "hi"}
    store.address_items([message])
    blob = v2.message_path(store.artifacts_dir, v2.message_sha1(message))
    blob.write_text("tampered", encoding="utf-8")

    store_2 = _store_mod.TraceStore(trace_dir)
    store_2.address_items([message])

    assert blob.read_text(encoding="utf-8") == v2.message_text(message)


def test_spans_address_items_returns_the_input_when_the_store_fails(trace_dir, monkeypatch):
    def _boom():
        raise RuntimeError("no store")

    monkeypatch.setattr(_spans, "_get_store", _boom)
    items = [{"role": "user", "content": "hi"}]

    assert _spans.address_items(items) == items
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `<main>/.venv/bin/python -m pytest tests/test_tracing_api.py -k address_items -n0`
Expected: FAIL with `AttributeError: 'TraceStore' object has no attribute 'address_items'`

- [ ] **Step 3: Add `messages_dir` and `address_items` to the store**

In `raven/tracing/store.py`, add the import:

```python
from . import artifact_v2
```

In `TraceStore.__init__`, after `self.blobs_dir = ...`:

```python
self.messages_dir = artifact_v2.messages_dir(self.artifacts_dir)
```

Then, after `persist_artifact`:

```python
    def address_items(self, items: list[Any]) -> list[Any]:
        """Publish each item under ``_messages/``; return ``{"$msg": sha1}`` refs.

        An item whose blob cannot be written comes back verbatim, so one
        filesystem failure costs that item its sharing rather than the record.
        The published blob is never hard-linked: it is the only file holding
        that content, and the shell references it by name from its JSON text.
        That is why these live beside ``_blobs/`` and not inside it -
        :mod:`raven.tracing.compact` sweeps a blob whose link count is 1.
        """
        refs: list[Any] = []
        for item in items:
            try:
                text = artifact_v2.message_text(item)
                sha1 = artifact_v2.message_sha1(item)
                blob = artifact_v2.message_path(self.artifacts_dir, sha1)
                if not blob.exists():
                    self._publish_blob(blob, text, sha1)
                if blob.exists() and not self._blob_is_intact(blob, sha1):
                    self._repair_blob(blob, text, sha1)
                refs.append(artifact_v2.make_ref(sha1))
            except OSError:
                refs.append(item)
        return refs
```

- [ ] **Step 4: Add the `spans` facade**

In `raven/tracing/spans.py`, after `persist_artifact`:

```python
def address_items(items: list[Any]) -> list[Any]:
    try:
        return _get_store().address_items(items)
    except Exception:  # noqa: BLE001
        return items
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `<main>/.venv/bin/python -m pytest tests/test_tracing_api.py tests/test_tracing_compact.py -n0`
Expected: PASS. `test_tracing_compact.py` stays green here, which the plan read
as proof that `compact` was unaffected. It is not: that suite has no
`_messages/` tree in any fixture, so it cannot see the interaction. Review
found `_iter_day_dirs` reading the message store as an artifact tree; the
regression now lives in that file as
`test_the_message_store_is_not_an_artifact_tree`.

- [ ] **Step 6: Report and stop (commit only when the user says so)**

```bash
git add raven/tracing/store.py raven/tracing/spans.py tests/test_tracing_api.py
git commit -m "feat(tracing): publish addressed messages under _messages"
```

---

### Task 3: Resolve at the bundle boundary

**Files:**
- Modify: `raven/trajectory/bundle.py:45-76` (`_import_artifact`), `:177-189` (manifest)
- Test: `tests/test_trajectory_bundle.py`

**Interfaces:**
- Consumes: `artifact_v2.is_v2`, `resolve_payload`, `ARTIFACT_FORMAT`, `make_ref`, `message_sha1`, `message_text`, `message_path`.
- Produces: manifest key `"missing_messages": list[str]` alongside the
  existing `"missing_artifacts"`.

**The property this task must not break:** the bundle holds v1-shaped
artifacts, so `replay.py` and `cassette.py` never see v2. Step 5 asserts that
by running their existing suites *unmodified*. If passing requires editing a
line in either, the boundary-resolution design has failed and the answer is to
revisit the design, not the tests.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trajectory_bundle.py`. The file's own fixtures are
`state` and `workspace`; its helpers are `_write_log`, `_span`, `_read_lines`;
its entry point is `tbundle.collect_bundle`.

```python
def _v2_artifact(state, messages, name="llm-input.json"):
    """A v2 shell on disk with its message blobs, as the writer leaves it."""
    from raven.tracing import artifact_v2 as v2

    artifacts = state / "logs" / "audit-artifacts"
    refs = []
    for message in messages:
        sha1 = v2.message_sha1(message)
        blob = v2.message_path(artifacts, sha1)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_text(v2.message_text(message), encoding="utf-8")
        refs.append(v2.make_ref(sha1))
    shell = {
        "artifactFormat": v2.ARTIFACT_FORMAT,
        "model": "openrouter/x",
        "systemPrompt": refs[0],
        "prompt": refs[-1],
        "messages": refs,
        "tools": None,
    }
    path = state / "logs" / "audit-artifacts" / "llm.input" / "2026-08-20" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(shell, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


_V2_MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
]


def test_a_bundled_v2_artifact_is_written_out_resolved(state, workspace, tmp_path):
    source = _v2_artifact(state, _V2_MESSAGES)
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="llm.call", attrs={"llm.input.artifact_path": str(source)})],
    )

    bundle_dir = tbundle.collect_bundle(
        "trace-1", out_dir=tmp_path / "out", state_dir=state, workspace=workspace
    )

    packed = json.loads((bundle_dir / "artifacts" / source.name).read_text(encoding="utf-8"))
    assert "artifactFormat" not in packed
    assert packed["messages"] == _V2_MESSAGES
    assert packed["systemPrompt"] == "sys"
    assert packed["prompt"] == "hi"


def test_a_bundled_v2_artifact_reads_after_the_live_store_is_gone(state, workspace, tmp_path):
    """The bundle's whole promise: it survives being copied to another machine."""
    source = _v2_artifact(state, _V2_MESSAGES)
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="llm.call", attrs={"llm.input.artifact_path": str(source)})],
    )

    bundle_dir = tbundle.collect_bundle(
        "trace-1", out_dir=tmp_path / "out", state_dir=state, workspace=workspace
    )
    moved = tmp_path / "elsewhere"
    shutil.move(str(bundle_dir), str(moved))
    shutil.rmtree(state)

    packed = json.loads((moved / "artifacts" / source.name).read_text(encoding="utf-8"))
    assert packed["messages"] == _V2_MESSAGES


def test_a_missing_message_blob_becomes_a_placeholder_and_reaches_the_manifest(
    state, workspace, tmp_path
):
    from raven.tracing import artifact_v2 as v2

    source = _v2_artifact(state, _V2_MESSAGES)
    gone = v2.message_sha1(_V2_MESSAGES[1])
    v2.message_path(state / "logs" / "audit-artifacts", gone).unlink()
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="llm.call", attrs={"llm.input.artifact_path": str(source)})],
    )

    bundle_dir = tbundle.collect_bundle(
        "trace-1", out_dir=tmp_path / "out", state_dir=state, workspace=workspace
    )

    packed = json.loads((bundle_dir / "artifacts" / source.name).read_text(encoding="utf-8"))
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(packed["messages"]) == 2, "position is preserved; a message is never dropped"
    assert packed["messages"][0] == _V2_MESSAGES[0]
    assert gone in packed["messages"][1]["content"]
    assert manifest["missing_messages"] == [gone]


def test_a_v1_artifact_is_still_copied_byte_for_byte(state, workspace, tmp_path):
    raw = '{"messages": [{"role": "user", "content": "hi"}]}'
    source = _make_artifact(state, "v1.json", raw)
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", name="tool.call", attrs={"tool.output.artifact_path": str(source)})],
    )

    bundle_dir = tbundle.collect_bundle(
        "trace-1", out_dir=tmp_path / "out", state_dir=state, workspace=workspace
    )

    assert (bundle_dir / "artifacts" / "v1.json").read_text(encoding="utf-8") == raw
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `<main>/.venv/bin/python -m pytest tests/test_trajectory_bundle.py -n0`
Expected: FAIL - `artifactFormat` is still present in the packed artifact, and
`manifest["missing_messages"]` raises `KeyError`

- [ ] **Step 3: Resolve inside `_import_artifact`**

In `raven/trajectory/bundle.py`, add the import:

```python
from raven.tracing import artifact_v2
```

Change `_import_artifact`'s signature to take the message store root and a
sink for missing addresses, and replace the copy with a resolve-or-copy:

```python
def _import_artifact(
    source: str,
    artifacts_dir: Path,
    copied: dict[str, str],
    missing: list[str],
    names: dict[str, str],
    store_artifacts: Path,
    missing_messages: list[str],
) -> str | None:
    """Copy one referenced file into the bundle; return its relative path.

    A v2 shell is resolved on the way in, so the bundle holds v1-shaped
    artifacts and stays readable on a machine that has no message store. That
    is the bundle's whole promise; keeping references here would break it.
    """
    if source in copied:
        return copied[source]
    if source in missing:
        return None
    src = Path(source)
    if not src.is_file():
        missing.append(source)
        return None
    name = src.name
    counter = 1
    while name in names and names[name] != source:
        name = f"{counter}-{src.name}"
        counter += 1
    names[name] = source
    target = artifacts_dir / name
    if not _copy_resolved(src, target, store_artifacts, missing_messages):
        shutil.copy2(src, target)
    rel = f"{_ARTIFACTS_DIR}/{name}"
    copied[source] = rel
    return rel


def _copy_resolved(src: Path, target: Path, store_artifacts: Path, missing_messages: list[str]) -> bool:
    """Write ``src`` to ``target`` resolved; False when it is not a v2 shell.

    Unreadable or unparseable input answers False so the caller falls back to
    a byte copy: a file this cannot understand is still worth packing.
    """
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not artifact_v2.is_v2(payload):
        return False

    def record(sha1: str):
        if sha1 not in missing_messages:
            missing_messages.append(sha1)
        return artifact_v2.placeholder(sha1)

    resolved = artifact_v2.resolve_payload(payload, store_artifacts, on_missing=record)
    target.write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
    return True
```

At the call site (`bundle.py:155`), thread the two new arguments through.
Declare `missing_messages: list[str] = []` beside the existing `missing` list,
and pass `resolved_state / "logs" / "audit-artifacts"` as `store_artifacts`:

```python
                rel = _import_artifact(
                    value,
                    artifacts_dir,
                    copied,
                    missing,
                    names,
                    resolved_state / "logs" / "audit-artifacts",
                    missing_messages,
                )
```

- [ ] **Step 4: Add the manifest key**

In the `manifest` dict, after `"missing_artifacts": missing,`:

```python
            "missing_messages": missing_messages,
```

Add the key to the module docstring's bundle-layout comment as well, where
`manifest.json` is described.

- [ ] **Step 5: Run the tests, including the untouched consumer suites**

Run:
```
<main>/.venv/bin/python -m pytest tests/test_trajectory_bundle.py \
  tests/test_trajectory_replay.py tests/test_trajectory_cassette.py \
  tests/test_trajectory_regressions.py tests/test_cli_trajectory_commands.py -n0
```
Expected: PASS, with **no edits to the replay, cassette or regression tests**.
Confirm with `git diff --name-only` that none of those three files appear.

- [ ] **Step 6: Report and stop (commit only when the user says so)**

```bash
git add raven/trajectory/bundle.py tests/test_trajectory_bundle.py
git commit -m "feat(trajectory): resolve v2 artifacts when packing a bundle"
```

---

### Task 4: Resolve in the viewer

**Files:**
- Modify: `raven/cli/tracing_viewer/server.js:1040-1058`
- Test: `tests/integration/test_tracing_viewer_e2e.py`

**Interfaces:**
- Consumes: the on-disk format only. No Python import; this is the mirror of
  `artifact_v2.py` and must implement the same rules.
- Produces: `readArtifact` returns `{path, content, parsed}` where `parsed` is
  v1-shaped. `content` keeps the raw shell text.

**The trap this task exists to avoid:** `app.js:1232` takes
`parsed.systemPrompt || ... || ''` and calls `.match()` on it. A resolved
message *object* is truthy and has no `.match`, so `systemPrompt` and `prompt`
must resolve to text, not to the object.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_tracing_viewer_e2e.py`. Its `_viewer(state_dir)`
context manager yields a **port**, and `_get(port, route)` returns parsed JSON;
the route is `/api/artifact?path=<absolute path>` (`server.js:1318`).

```python
def _v2_shell(state_dir, messages, *, extra_messages=()):
    """A v2 shell plus its message blobs under the viewer's state dir."""
    from raven.tracing import artifact_v2 as v2

    artifacts = state_dir / "logs" / "audit-artifacts"
    refs = []
    for message in messages:
        sha1 = v2.message_sha1(message)
        blob = v2.message_path(artifacts, sha1)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_text(v2.message_text(message), encoding="utf-8")
        refs.append(v2.make_ref(sha1))
    payload = {
        "artifactFormat": v2.ARTIFACT_FORMAT,
        "provider": "openrouter",
        "model": "openrouter/x",
        "systemPrompt": refs[0],
        "prompt": refs[-1],
        "messages": [*refs, *extra_messages],
        "tools": [{"function": {"name": "grep"}}],
    }
    path = artifacts / "llm.input" / "2026-08-01" / "in.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path, payload


def test_the_viewer_resolves_a_v2_artifact_to_v1_shape(tmp_path):
    messages = [
        {"role": "system", "content": "you are raven"},
        {"role": "user", "content": [{"type": "text", "text": "latest"}]},
    ]
    shell, _ = _v2_shell(tmp_path, messages)

    with _viewer(tmp_path) as port:
        got = _get(port, f"/api/artifact?path={shell}")

    assert got["parsed"]["messages"] == messages
    assert got["parsed"]["systemPrompt"] == "you are raven"
    assert isinstance(got["parsed"]["prompt"], str), "app.js calls .match() on this"
    assert got["parsed"]["prompt"] == '[{"type":"text","text":"latest"}]'
    assert "artifactFormat" not in got["parsed"]


def test_the_viewer_renders_a_placeholder_for_a_message_blob_that_is_gone(tmp_path):
    from raven.tracing import artifact_v2 as v2

    messages = [
        {"role": "system", "content": "you are raven"},
        {"role": "user", "content": "hi"},
    ]
    shell, _ = _v2_shell(tmp_path, messages)
    gone = v2.message_sha1(messages[1])
    v2.message_path(tmp_path / "logs" / "audit-artifacts", gone).unlink()

    with _viewer(tmp_path) as port:
        got = _get(port, f"/api/artifact?path={shell}")

    resolved = got["parsed"]["messages"]
    assert len(resolved) == 2, "position is preserved; a message is never dropped"
    assert gone in resolved[1]["content"]


def test_the_viewer_leaves_a_reference_that_is_not_a_sha1_alone(tmp_path):
    from raven.tracing import artifact_v2 as v2

    artifacts = tmp_path / "logs" / "audit-artifacts"
    shell = artifacts / "llm.input" / "2026-08-01" / "in.json"
    shell.parent.mkdir(parents=True, exist_ok=True)
    shell.write_text(
        json.dumps(
            {
                "artifactFormat": v2.ARTIFACT_FORMAT,
                "messages": [{"$msg": "../../../etc/passwd"}, {"$msg": "0" * 39}],
            }
        ),
        encoding="utf-8",
    )

    with _viewer(tmp_path) as port:
        got = _get(port, f"/api/artifact?path={shell}")

    assert got["parsed"]["messages"] == [
        {"$msg": "../../../etc/passwd"},
        {"$msg": "0" * 39},
    ], "an invalid reference is data, not an address: pass it through untouched"


def test_a_v1_artifact_is_returned_unchanged(tmp_path):
    artifacts = tmp_path / "logs" / "audit-artifacts"
    shell = artifacts / "llm.input" / "2026-08-01" / "v1.json"
    shell.parent.mkdir(parents=True, exist_ok=True)
    v1 = {"messages": [{"role": "user", "content": "hi"}], "systemPrompt": "sys"}
    shell.write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")

    with _viewer(tmp_path) as port:
        got = _get(port, f"/api/artifact?path={shell}")

    assert got["parsed"] == v1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `<main>/.venv/bin/python -m pytest tests/integration/test_tracing_viewer_e2e.py -m integration -n0 -k v2 or unchanged`
Expected: the four new tests FAIL - `parsed.messages` still holds
`{"$msg": ...}` envelopes and `parsed.systemPrompt` is still a dict.
`test_a_v1_artifact_is_returned_unchanged` passes already; that is correct, it
guards the path this task must not disturb.

- [ ] **Step 3: Mirror the format in `server.js`**

In `raven/cli/tracing_viewer/server.js`, above `readArtifact`:

```javascript
// Mirror of raven/tracing/artifact_v2.py. A cross-language round-trip test
// compares the two, so any rule changed there changes here in the same commit.
const ARTIFACT_FORMAT_V2 = 'audit.artifact.v2';
const MESSAGES_DIR = path.join(ARTIFACTS_DIR, '_messages');
const MSG_REF_KEY = '$msg';
const TEXT_FIELDS = ['systemPrompt', 'prompt'];
const SHA1_RE = /^[0-9a-f]{40}$/;

function refSha1(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const keys = Object.keys(value);
  if (keys.length !== 1 || keys[0] !== MSG_REF_KEY) return null;
  const sha1 = value[MSG_REF_KEY];
  return typeof sha1 === 'string' && SHA1_RE.test(sha1) ? sha1 : null;
}

function coerceText(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  try {
    // JSON.stringify emits no space after ':' or ',', which is exactly what
    // artifact_v2.coerce_text produces via separators=(',', ':').
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function resolvePayload(payload) {
  if (!payload || typeof payload !== 'object' || payload.artifactFormat !== ARTIFACT_FORMAT_V2) {
    return payload;
  }
  const cache = new Map();
  const load = (sha1) => {
    if (!cache.has(sha1)) {
      const file = path.join(MESSAGES_DIR, sha1.slice(0, 2), `${sha1}.json`);
      let value;
      try {
        value = JSON.parse(fs.readFileSync(file, 'utf8'));
      } catch {
        value = { role: 'unknown', content: `[message blob missing: ${sha1}]` };
      }
      cache.set(sha1, value);
    }
    return cache.get(sha1);
  };
  const one = (value) => {
    const sha1 = refSha1(value);
    return sha1 === null ? value : load(sha1);
  };

  const out = {};
  for (const [key, value] of Object.entries(payload)) {
    if (key !== 'artifactFormat') out[key] = value;
  }
  if (Array.isArray(out.messages)) out.messages = out.messages.map(one);
  for (const field of TEXT_FIELDS) {
    if (field in out) {
      const resolved = one(out[field]);
      const content =
        resolved && typeof resolved === 'object' && !Array.isArray(resolved)
          ? resolved.content
          : resolved;
      out[field] = coerceText(content);
    }
  }
  return out;
}
```

Then in `readArtifact`, change the parse to resolve:

```javascript
  let parsed = null;
  try {
    parsed = resolvePayload(JSON.parse(content));
  } catch {}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `<main>/.venv/bin/python -m pytest tests/integration/test_tracing_viewer_e2e.py -m integration -n0`
Expected: PASS, all pre-existing tests in the file included

- [ ] **Step 5: Report and stop (commit only when the user says so)**

```bash
git add raven/cli/tracing_viewer/server.js tests/integration/test_tracing_viewer_e2e.py
git commit -m "feat(cli): resolve v2 artifacts in the tracing viewer"
```

---

### Task 5: The cross-language round-trip gate

**Files:**
- Test: `tests/integration/test_tracing_viewer_e2e.py`

**Interfaces:**
- Consumes: `artifact_v2.resolve_payload` (Task 1) and the viewer's
  `resolvePayload` (Task 4).
- Produces: nothing. This task adds only a test.

**Why it is its own task:** it is the only assertion that can catch a
divergence between the two implementations, and it can only be written once
both exist. A divergence is otherwise silent - the viewer renders, the bundle
packs, and what replay compares is simply not what a human saw. The fixture
deliberately includes non-string content, because that is where Python's and
JavaScript's default JSON separators differ.

- [ ] **Step 1: Write the test**

Append to `tests/integration/test_tracing_viewer_e2e.py`:

```python
def test_python_and_the_viewer_resolve_a_shell_identically(tmp_path):
    """The one gate on 'one rule, two implementations, two languages'.

    Includes non-string content on purpose: Python's default JSON separators
    carry spaces and JavaScript's do not, so a text field built from a
    multimodal message is where the two would first disagree.
    """
    from raven.tracing import artifact_v2 as v2

    artifacts = tmp_path / "logs" / "audit-artifacts"
    messages = [
        {"role": "system", "content": "you are raven"},
        {"role": "user", "content": "plain"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "name": "grep"}]},
        {"role": "user", "content": [{"type": "text", "text": "latest"}, {"type": "image"}]},
    ]
    refs = []
    for message in messages:
        blob = v2.message_path(artifacts, v2.message_sha1(message))
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_text(v2.message_text(message), encoding="utf-8")
        refs.append(v2.make_ref(v2.message_sha1(message)))
    inlined = {"role": "user", "content": "this one was never addressed"}
    shell_payload = {
        "artifactFormat": v2.ARTIFACT_FORMAT,
        "provider": "openrouter",
        "model": "openrouter/x",
        "systemPrompt": refs[0],
        "prompt": refs[3],
        "messages": [*refs, inlined, {"$msg": "not-a-sha1"}],
        "tools": [{"function": {"name": "grep"}}],
    }
    shell = artifacts / "llm.input" / "2026-09-10" / "in.json"
    shell.parent.mkdir(parents=True, exist_ok=True)
    shell.write_text(json.dumps(shell_payload, ensure_ascii=False), encoding="utf-8")

    from_python = v2.resolve_payload(shell_payload, artifacts)
    with _viewer(tmp_path) as port:
        from_viewer = _get(port, f"/api/artifact?path={shell}")["parsed"]

    assert json.dumps(from_python, ensure_ascii=False, sort_keys=True) == json.dumps(
        from_viewer, ensure_ascii=False, sort_keys=True
    )
```

- [ ] **Step 2: Run it**

Run: `<main>/.venv/bin/python -m pytest tests/integration/test_tracing_viewer_e2e.py -m integration -n0 -k identically`
Expected: PASS. A failure here is a real format divergence, not a flaky test -
diff the two JSON dumps and fix whichever side departs from
`artifact_v2.py`, which is the authority.

- [ ] **Step 3: Report and stop (commit only when the user says so)**

```bash
git add tests/integration/test_tracing_viewer_e2e.py
git commit -m "test(tracing): gate the v2 resolver against the viewer's mirror"
```

---

### Task 6: Emit v2 from the writer

**Files:**
- Modify: `raven/observability/semconv.py` (`_llm_input_payload`, and `llm_call` / the streaming variant at `:629` and `:648`)
- Test: `tests/test_tracing_api.py`

**Interfaces:**
- Consumes: `spans.address_items` (Task 2), `artifact_v2.ARTIFACT_FORMAT`.
- Produces: `llm.input` artifacts in v2 shape; span attribute
  `llm.input.content_bytes: int`.

**This task is last on purpose.** Every reader already understands v2, so no
commit in this plan leaves artifacts on disk that nothing can resolve.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracing_api.py`:

```python
def test_llm_input_payload_v2_key_set_is_frozen():
    """The v2 shell's keys are a gate, as the v1 key set was.

    Nothing may restate a slice of ``messages``: a second serialization of the
    same turns is what this format exists to remove.
    """
    from raven.observability import semconv
    from raven.tracing import artifact_v2 as v2

    payload = semconv.llm_input_payload(
        "openrouter",
        "openrouter/x",
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        None,
    )

    assert payload["artifactFormat"] == v2.ARTIFACT_FORMAT
    assert set(payload) == {
        "artifactFormat",
        "provider",
        "providerClass",
        "model",
        "systemPrompt",
        "prompt",
        "messages",
        "tools",
    }


def test_llm_input_payload_aliases_the_text_fields_onto_message_refs(trace_dir):
    from raven.observability import semconv
    from raven.tracing import artifact_v2 as v2

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "user", "content": "latest"},
    ]

    payload = semconv.llm_input_payload("openrouter", "openrouter/x", msgs, None)

    assert payload["messages"][0] == v2.make_ref(v2.message_sha1(msgs[0]))
    assert payload["systemPrompt"] == payload["messages"][0]
    assert payload["prompt"] == payload["messages"][2]


def test_a_recorded_llm_input_round_trips_back_to_the_v1_payload(trace_dir):
    from raven.observability import semconv
    from raven.tracing import artifact_v2 as v2

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "latest"},
    ]

    payload = semconv.llm_input_payload("openrouter", "openrouter/x", msgs, None)
    resolved = v2.resolve_payload(payload, _spans._get_store().artifacts_dir)

    assert resolved["messages"] == msgs
    assert resolved["systemPrompt"] == "sys"
    assert resolved["prompt"] == "latest"


def test_llm_call_records_the_resolved_size(trace_dir):
    from raven.observability import semconv

    class _Resp:
        content = "hi"
        tool_calls: list = []
        usage = None
        finish_reason = "stop"
        reasoning_content = None
        thinking_blocks = None

    msgs = [{"role": "user", "content": "x" * 5000}]
    with trace.span("llm.call") as s:
        semconv.llm_call(s, {"self": None, "messages": msgs, "tools": None, "model": "openrouter/x"}, _Resp(), None)

    attrs = _spans_written(trace_dir)[0]["attributes"]
    assert attrs["llm.input.content_bytes"] > 5000
    assert attrs["llm.input.artifact_bytes"] < attrs["llm.input.content_bytes"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `<main>/.venv/bin/python -m pytest tests/test_tracing_api.py -k "v2_key_set or aliases or round_trips or resolved_size" -n0`
Expected: FAIL - `KeyError: 'artifactFormat'` and
`KeyError: 'llm.input.content_bytes'`

- [ ] **Step 3: Build the v2 shell**

Replace `_llm_input_payload`'s body in `raven/observability/semconv.py`:

```python
def _llm_input_payload(
    provider: str, model: str | None, messages: Any, tools: Any, provider_class: str | None = None
) -> dict:
    """Artifact payload for the model-input card, in ``audit.artifact.v2``.

    Each message is stored once under ``_messages/`` and referenced here, so a
    turn that appends one message no longer rewrites the whole conversation.
    ``systemPrompt`` and ``prompt`` alias their element of ``messages`` rather
    than restating its text - the same reference object, so there is no second
    address that could disagree.

    The referenced list is not what went on the wire: the provider adds or
    removes prompt-cache breakpoints on copies (``providers.prompt_cache``)
    after this is recorded. Neither the presence nor the absence of
    ``cache_control`` here says what was sent.
    """
    msgs = messages if isinstance(messages, list) else []
    refs = _spans.address_items(msgs)
    system_ref: Any = ""
    prompt_ref: Any = ""
    for index, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "system":
            system_ref = refs[index]
            break
    for index in range(len(msgs) - 1, -1, -1):
        m = msgs[index]
        if isinstance(m, dict) and m.get("role") == "user":
            prompt_ref = refs[index]
            break
    return {
        "artifactFormat": artifact_v2.ARTIFACT_FORMAT,
        "provider": provider,
        "providerClass": provider_class,
        "model": model,
        "systemPrompt": system_ref,
        "prompt": prompt_ref,
        "messages": refs,
        "tools": tools,
    }
```

Add the imports beside the existing ones:

```python
from raven.tracing import artifact_v2
from raven.tracing import spans as _spans
```

**Note the deliberate change in the system-message rule.** v1 took the first
*non-empty* system message. A reference has no notion of emptiness, so v2
takes the first system message. The distinction only shows when a provider
sends an empty leading system message; there is no reference to alias to for
"the first non-empty text", and inventing one would mean a second address.

**And a second deliberate change: `messages` is always a list.** v1 returned a
non-list `messages` argument verbatim, so a caller passing `None` got
`"messages": None`. v2 addresses `[]` instead and emits `"messages": []`, which
gives the field one constant type and spares both resolvers a special case. The
`llm.input` extractors always pass `bound.get("messages")` from a real call, so
this branch is unreachable in practice. Update the assertion the preceding
change left behind, in `tests/test_tracing_api.py`
(`test_llm_input_payload_lifts_system_and_latest_user_from_the_raw_list`):

```python
    not_a_list = semconv.llm_input_payload("p", "m", None, None)
    assert not_a_list["messages"] == [], "v2 gives the field one constant type"
    assert not_a_list["systemPrompt"] == ""
    assert not_a_list["prompt"] == ""
```

That assertion is correct for the preceding commit and only becomes wrong once
this one lands, so it changes here rather than there - each commit stays green
on its own.

- [ ] **Step 4: Record `content_bytes`**

Attributes reach a span through `span.set(attrs)` with a dict. Both `llm_call`
and `llm_call_stream` build that dict from `llm_attrs(...)`, so add one line to
each, before its `span.set(attrs)`.

In `llm_call`:

```python
    span.artifact("llm.input", llm_input_payload(pname, eff_model, messages, tools, provider_class))
    attrs = llm_attrs(result, pname, eff_model, provider_class)
    attrs["llm.input.content_bytes"] = _messages_bytes(messages)
    if span.invocation_source:
        attrs["llm.invocation_source"] = span.invocation_source
    span.set(attrs)
```

In `llm_call_stream`, the same line goes between `attrs = llm_attrs(...)` and
`attrs["llm.stream"] = True`:

```python
    attrs = llm_attrs(result, pname, eff_model, provider_class)
    attrs["llm.input.content_bytes"] = _messages_bytes(messages)
    attrs["llm.stream"] = True
```

Add the helper beside `_llm_input_payload`:

```python
def _messages_bytes(messages: Any) -> int:
    """Size of the conversation the shell references, which the shell no longer has.

    ``llm.input.artifact_bytes`` now measures the shell, tens of KiB where it
    used to be the whole request. This carries the number that attribute meant
    before, so a span still says how large the request was.
    """
    if not isinstance(messages, list):
        return 0
    return sum(len(artifact_v2.message_text(m).encode("utf-8")) for m in messages)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```
<main>/.venv/bin/python -m pytest tests/test_tracing_api.py \
  tests/test_tracing_artifact_v2.py tests/test_trajectory_bundle.py \
  tests/test_trajectory_replay.py tests/test_trajectory_cassette.py \
  tests/test_trajectory_regressions.py tests/test_tracing_compact.py \
  tests/test_cli_tracing_commands.py tests/test_cli_trajectory_commands.py \
  tests/test_agent_loop_truncation_paths.py -n0
```
Then the integration file:
```
<main>/.venv/bin/python -m pytest tests/integration/test_tracing_viewer_e2e.py -m integration -n0
```
Expected: PASS on both.

- [ ] **Step 6: Verify on real data**

Run a real turn and confirm the artifact it writes resolves back:

```bash
RAVEN_TRACING_DIR=/tmp/v2check <main>/.venv/bin/python -m raven agent -m "say hi"
<main>/.venv/bin/python - <<'EOF'
import glob, json
from pathlib import Path
from raven.tracing import artifact_v2 as v2
art = Path("/tmp/v2check/logs/audit-artifacts")
shell_path = sorted(glob.glob(str(art / "llm.input" / "*" / "*.json")))[-1]
shell = json.loads(Path(shell_path).read_text(encoding="utf-8"))
print("format:", shell.get("artifactFormat"))
print("shell bytes:", len(Path(shell_path).read_bytes()))
resolved = v2.resolve_payload(shell, art)
print("messages resolved:", len(resolved["messages"]))
print("systemPrompt is str:", isinstance(resolved["systemPrompt"], str))
assert all(v2.ref_sha1(m) is None for m in resolved["messages"]), "an unresolved ref remains"
EOF
```
Expected: format is `audit.artifact.v2`, the shell is orders of magnitude
smaller than the resolved content, and no reference survives resolution.

- [ ] **Step 7: Report and stop (commit only when the user says so)**

```bash
git add raven/observability/semconv.py tests/test_tracing_api.py
git commit -m "feat(observability): record llm.input as addressed messages"
```

---

## Lint and gates before any push

```bash
<main>/.venv/bin/python -m ruff check raven/ tests/
<main>/.venv/bin/python -m ruff format --check raven/ tests/
<main>/.venv/bin/python scripts/check_source_language.py origin/main...HEAD
make check-large-files
```

`check_source_language.py` is vacuous on an uncommitted tree - the range is
empty. Run it after committing, or scan added lines directly:
`git diff -U0 | grep '^+' | grep -P '[^\x00-\x7F]'` must print nothing.
