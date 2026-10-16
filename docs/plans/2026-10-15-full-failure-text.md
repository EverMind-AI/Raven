# Full Failure Text Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A failed turn's row in the web client expands to the whole failure text (scrubbed, capped), live and on replay, while every 200-character text stays as it is.

**Architecture:** The agent loop builds the long account (`raven/agent/loop/failure_text.py`: unbounded, credentials scrubbed with the call record's scrubber, capped at the call record's body ceiling) and puts it on the exception it lets out as `full_detail`; the kernel lane only copies that string onto `TurnFailed.full_detail`, because `raven/spine` imports no other raven package. The RPC outlet sends it as `ErrorEventPayload.full_detail`, `_save_broken_turn` files it as `turn_ended.full_detail`, and the page's `NoteView` gains an expand toggle that shows it.

**Tech Stack:** Python 3.12 (asyncio, dataclasses, pytest), import-linter, OpenRPC JSON schema with generated TypeScript clients, React 18 + vitest (`ui-web`).

**Spec:** `docs/specs/2026-10-15-turn-continuation-design.md` -- this plan is its first pull request (Delivery item 1: section 5, the expand half of section 6, the `full_detail` fields of the contract surface). Turn Continuation (Delivery item 2) gets its own plan once this one has landed.

## Global Constraints

- The bounded texts do not change: `TURN_FAILURE_TEXT_MAX = 200` (`raven/spine/events.py`), `LLM_ERROR_DETAIL_MAX = 200` (`raven/providers/base.py`), `describe_failure`'s output, the marker's `turn_ended.reason`.
- The long account is scrubbed with `raven/providers/call_record.py`'s scrubber (made public as `scrub_credentials`), then with `raven.security.redact.redact` (the shapes a crash message carries), and capped at `call_record.MAX_BODY_CHARS` (8,000). No new cap constant.
- `raven/spine` imports no other raven package and stays under `LINE_CEILING = 2_000` lines (`tests/test_kernel_budget.py`; 1,531 at `a71b488e6`).
- `rpc-schema/openrpc.json` is the contract source; `raven/rpc/models.py` mirrors it by hand; both `src/rpc/generated.ts` files are regenerated, never edited.
- New web client strings: `gui.transcript.error_expand` = en "Show full error" / zh "展开完整错误"; `gui.transcript.error_collapse` = en "Hide" / zh "收起". They live in `i18n/messages.json`; the TUI copy is regenerated.
- Source language: English for code, comments and docstrings (AGENTS.md 1); CJK only inside `i18n/messages.json` (already a CJK carrier) and the regenerated `ui-tui/src/i18n/messages.generated.ts`.
- Every new Python file opens with an English module docstring (AGENTS.md 1.1). Comments only where they say why.
- Python tests run as `uv run --frozen --python 3.12 --all-extras pytest ... -q -p no:randomly`, never bare `pytest` (AGENTS.md 4, 5.4). Vitest runs as `node node_modules/vitest/vitest.mjs run <paths>` from `ui-web/`, never through `npx`.
- Branch `feat/full_failure_text`, cut from `origin/main` at `a71b488e6`, in its own worktree. Commands below write that worktree's path as `$WT` and the main checkout's, whose `node_modules` the worktree links, as `$MAIN`.
- No commit and no push without the owner's explicit word (AGENTS.md 3.4). Each task ends by staging its files; the commit is Task 8, one commit, Conventional Commits.

## Review Focus

1. **A body that is one enormous line** (minified JSON, a base64 blob, no spaces): the expanded block must wrap and scroll inside the row, never widen the transcript. Pinned in Task 7 (the real web client, 6,000-character single-token body, horizontal overflow of `#scroll` must be 0).
2. **A provider that echoes a credential back** (the request's `Authorization` header, an `sk-...` key) in its error body: the long account must not carry it, while the row still opens. Pinned in Task 2 (unit), Task 3 (marker) and Task 7 (the real web client).
3. **A session written before this change**: its marker has a 200-character `reason` and no `full_detail`. The row must still open, to the 200 characters it has. Pinned in Task 5.
4. **A failure the short text already says in full** (`ValueError("boom")`): no toggle, no second copy on the marker. Pinned in Task 2 (`attach_full_detail` returns None) and Task 5 (no toggle).
5. **The live row and the replayed row of one failure**: same text, same toggle, same expanded content, so a reload changes nothing a reader saw. Pinned in Task 5.

---

### Task 1: The lane carries a runner's long account

**Files:**
- Modify: `raven/spine/turn.py:19-27` (`AnswerlessTurnError`)
- Modify: `raven/spine/events.py:108-125` (`TurnFailed`)
- Modify: `raven/spine/scheduler.py:24-46` (new `full_detail_of` after `describe_failure`) and `raven/spine/scheduler.py:509-524` (the `TurnFailed` built in `_run_turn`'s `except Exception` arm)
- Test: `tests/test_spine_scheduler_lane.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `AnswerlessTurnError(*args, full_detail: str | None = None)` with attribute `full_detail: str | None`.
  - `TurnFailed.full_detail: str | None = None` (last field).
  - `raven.spine.scheduler.full_detail_of(exc: BaseException) -> str | None`.

- [ ] **Step 1: Prepare the worktree**

```bash
cd "$WT"
/usr/bin/git branch -m feat/continue_failed_turn feat/full_failure_text
/usr/bin/git branch --show-current          # expect: feat/full_failure_text
uv sync --frozen --all-extras --python 3.12
.venv/bin/python -V                          # expect: Python 3.12.x
uv run --frozen --python 3.12 --all-extras pytest tests/test_spine_scheduler_lane.py tests/test_spine_events.py tests/test_agent_loop_broken_turn.py tests/test_agent_loop_llm_error_patience.py tests/test_agent_loop_run_emit.py tests/test_rpc_spine.py tests/test_llm_call_record.py tests/test_rpc_schema_match.py tests/test_kernel_budget.py tests/test_kernel_closure.py -q -p no:randomly
```

Expected: all pass. Write down any failing test id: it is pre-existing and must still be the only failure at the end.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_spine_scheduler_lane.py`:

```python
async def test_a_runners_long_account_rides_the_failure():
    """The kernel cannot build the long form -- it imports no other raven
    package, so it has nothing to scrub with -- so it copies what the runner
    put on the exception, and nothing else."""

    class AttachingRunner:
        async def run(self, req, emit, drain) -> TurnOutcome:
            exc = RuntimeError("short")
            exc.full_detail = "RuntimeError: the whole story " + "x" * 500
            raise exc

    events, sink = _collector()
    lane = Lane(runner=AttachingRunner(), pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    await lane.submit(_req())
    failed = next(e for e in events if isinstance(e, TurnFailed))
    assert failed.error == "RuntimeError: short"
    assert failed.full_detail == "RuntimeError: the whole story " + "x" * 500


async def test_an_answerless_turn_carries_its_long_account():
    class ReportingRunner:
        async def run(self, req, emit, drain) -> TurnOutcome:
            raise AnswerlessTurnError(
                "Error calling LLM (rate_limit@openrouter): Provider returned error",
                full_detail="the provider's whole body",
            )

    events, sink = _collector()
    lane = Lane(runner=ReportingRunner(), pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    await lane.submit(_req())
    failed = next(e for e in events if isinstance(e, TurnFailed))
    assert failed.reported is True
    assert failed.error == "Error calling LLM (rate_limit@openrouter): Provider returned error"
    assert failed.full_detail == "the provider's whole body"


async def test_a_crash_with_no_long_account_carries_none():
    events, sink = _collector()
    lane = Lane(runner=FailingRunner(), pools=OriginPools(user=1, system=1), sink=sink, conversation_id="c")
    await lane.submit(_req())
    failed = next(e for e in events if isinstance(e, TurnFailed))
    assert failed.full_detail is None


def test_only_a_non_empty_string_counts_as_a_long_account():
    from raven.spine.scheduler import full_detail_of

    exc = ValueError("boom")
    assert full_detail_of(exc) is None
    exc.full_detail = 42
    assert full_detail_of(exc) is None
    exc.full_detail = "   "
    assert full_detail_of(exc) is None
    exc.full_detail = "the long one"
    assert full_detail_of(exc) == "the long one"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --frozen --python 3.12 --all-extras pytest tests/test_spine_scheduler_lane.py -q -p no:randomly -k "long_account or no_long_account"`
Expected: FAIL -- `TypeError: AnswerlessTurnError() takes no keyword arguments` / `AttributeError: 'TurnFailed' object has no attribute 'full_detail'` / `ImportError: cannot import name 'full_detail_of'`.

- [ ] **Step 4: Implement**

In `raven/spine/turn.py`, replace the `AnswerlessTurnError` class with:

```python
class AnswerlessTurnError(Exception):
    """A runner's own report that the turn it ran ended with no answer.

    Raised where an ordinary exception would misreport the failure: the runner
    has already put it into words a reader can act on (the model call it gave
    up on, and why), so ``describe_failure`` passes the text through unchanged
    instead of prefixing it with this class name, and the ``turn_ended`` marker
    that says so is filed before the failure leaves the loop.

    ``full_detail`` is the same report at length, for a reader who expands the
    row. The runner builds and scrubs it -- the kernel imports nothing to do
    that with -- and the lane copies it onto ``TurnFailed.full_detail``.
    """

    def __init__(self, *args: object, full_detail: str | None = None) -> None:
        super().__init__(*args)
        self.full_detail = full_detail
```

In `raven/spine/events.py`, extend `TurnFailed` (docstring paragraph plus the last field):

```python
@dataclass(frozen=True)
class TurnFailed:
    """A turn that ended without an answer, and what it is reported by.

    ``reported`` says the runner itself worded ``error`` -- it raised an
    ``AnswerlessTurnError``, whose message is the report a reader should see --
    rather than the text being whatever a crash carried. In-process only: the
    wire frame stays one ``error``, and the distinction exists so a consumer
    deciding what to show a stranger (the gateway's channel reply) can quote a
    report and refuse to quote a crash.

    ``full_detail`` is the failure's long account, for a client that lets a
    reader expand the row: built, scrubbed and capped by the runner, which sets
    it on the exception it lets out. None when the runner set none. ``error``
    stays the bounded text every other consumer reads.
    """

    error: str
    cancelled: bool
    conversation_id: str | None = None
    turn_id: str = ""
    reported: bool = False
    full_detail: str | None = None
```

In `raven/spine/scheduler.py`, add after `describe_failure`:

```python
def full_detail_of(exc: BaseException) -> str | None:
    """The long account a runner put on ``exc``, for ``TurnFailed.full_detail``.

    Read, never built: the kernel imports no other raven package, so it cannot
    scrub a crash's message itself. A runner that wants the row expandable sets
    a ``full_detail`` string on the exception it lets out; anything else --
    absent, blank, not a string -- is no account at all.
    """
    value = getattr(exc, "full_detail", None)
    return value if isinstance(value, str) and value.strip() else None
```

and add the field to the `TurnFailed` built in `_run_turn`'s `except Exception as exc:` arm:

```python
            failed = TurnFailed(
                error=describe_failure(exc),
                cancelled=False,
                conversation_id=self._conversation_id,
                turn_id=turn_id,
                # The runner worded this one itself, so a consumer may quote it;
                # every other exception here is a crash whose message names
                # hosts and paths.
                reported=isinstance(exc, AnswerlessTurnError),
                full_detail=full_detail_of(exc),
            )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --frozen --python 3.12 --all-extras pytest tests/test_spine_scheduler_lane.py tests/test_spine_events.py tests/test_kernel_budget.py tests/test_kernel_closure.py -q -p no:randomly`
Expected: PASS, including the kernel budget (the package grows by about 25 lines) and the kernel closure (no new import).

- [ ] **Step 6: Stage**

```bash
/usr/bin/git add raven/spine/turn.py raven/spine/events.py raven/spine/scheduler.py tests/test_spine_scheduler_lane.py
```

---

### Task 2: The long-account builder

**Files:**
- Modify: `raven/providers/call_record.py:89-112` (`_scrub` becomes public `scrub_credentials`) and its `__all__`
- Create: `raven/agent/loop/failure_text.py`
- Test: `tests/test_agent_loop_failure_text.py` (new; the module is `raven/agent/loop/failure_text.py`, named like the other `test_agent_loop_*` files)

**Interfaces:**
- Consumes: `AnswerlessTurnError(..., full_detail=...)` and its `full_detail` attribute (Task 1); `raven.spine.scheduler.describe_failure` (existing).
- Produces:
  - `raven.providers.call_record.scrub_credentials(text: str) -> str`
  - `raven.agent.loop.failure_text.full_failure_text(text: str) -> str`
  - `raven.agent.loop.failure_text.exception_failure_text(exc: BaseException) -> str`
  - `raven.agent.loop.failure_text.response_failure_text(content: str | None, record: CallRecord | None) -> str | None`
  - `raven.agent.loop.failure_text.attach_full_detail(exc: BaseException) -> str | None` (sets `exc.full_detail` to the same value)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_loop_failure_text.py`:

```python
"""The long account of a failed turn: unbounded, scrubbed, capped, and attached
only when it says more than the 200-character text already does."""

from __future__ import annotations

from raven.agent.loop.failure_text import (
    attach_full_detail,
    exception_failure_text,
    full_failure_text,
    response_failure_text,
)
from raven.contracts.llm_provider import CallRecord
from raven.providers.call_record import MAX_BODY_CHARS, REPLACEMENT
from raven.spine.turn import AnswerlessTurnError

KEY = "sk-or-v1-" + "a" * 40


def test_the_long_account_keeps_what_the_short_one_cuts():
    assert full_failure_text("upstream said: " + "x" * 600) == "upstream said: " + "x" * 600


def test_a_credential_in_the_account_is_scrubbed():
    text = full_failure_text(f"401 from the gateway, Authorization: Bearer {KEY}")
    assert KEY not in text
    assert REPLACEMENT in text


def test_the_account_is_capped_at_the_call_record_body_ceiling():
    text = full_failure_text("y" * (MAX_BODY_CHARS * 2))
    assert len(text) == MAX_BODY_CHARS
    assert text.endswith("...")


def test_an_exception_is_named_the_way_the_lane_names_it():
    assert exception_failure_text(ValueError("boom")) == "ValueError: boom"
    assert exception_failure_text(TimeoutError()) == "TimeoutError"


def test_an_answerless_turn_prefers_its_own_long_account():
    exc = AnswerlessTurnError("Error calling LLM (rate_limit@openrouter): Provider returned error", full_detail="the body")
    assert exception_failure_text(exc) == "the body"


def test_a_failed_call_reads_as_its_line_then_its_body():
    record = CallRecord(http_status=429, body='{"error":{"metadata":{"raw":"rate-limited upstream"}}}')
    text = response_failure_text("Error calling LLM (rate_limit@openrouter): Provider returned error", record)
    assert text == (
        "Error calling LLM (rate_limit@openrouter): Provider returned error\n\n"
        '{"error":{"metadata":{"raw":"rate-limited upstream"}}}'
    )


def test_a_body_the_line_already_quotes_is_not_repeated():
    record = CallRecord(body="no first byte")
    line = "Error calling LLM (first_byte_timeout): no first byte"
    assert response_failure_text(line, record) == line


def test_a_call_with_nothing_to_say_has_no_account():
    assert response_failure_text(None, None) is None
    assert response_failure_text("  ", CallRecord()) is None


def test_attached_only_when_it_says_more_than_the_short_text():
    short = ValueError("boom")
    assert attach_full_detail(short) is None
    assert short.full_detail is None

    long = ValueError("z" * 500)
    attached = attach_full_detail(long)
    assert attached == "ValueError: " + "z" * 500
    assert long.full_detail == attached


def test_a_second_attach_files_the_same_text():
    exc = AnswerlessTurnError("Error calling LLM (server@x): 503", full_detail="503 body " + "b" * 400)
    first = attach_full_detail(exc)
    assert attach_full_detail(exc) == first


def test_a_foreign_full_detail_is_never_passed_on_unscrubbed():
    exc = RuntimeError("boom " + "c" * 300)
    exc.full_detail = f"leaked {KEY}"
    attached = attach_full_detail(exc)
    assert attached == "RuntimeError: boom " + "c" * 300
    assert KEY not in (exc.full_detail or "")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen --python 3.12 --all-extras pytest tests/test_agent_loop_failure_text.py -q -p no:randomly`
Expected: FAIL -- `ModuleNotFoundError: No module named 'raven.agent.loop.failure_text'`.

- [ ] **Step 3: Make the call record's scrubber public**

In `raven/providers/call_record.py`, rename the function and its one caller:

```python
def scrub_credentials(text: str) -> str:
    """``text`` with the credential shapes a response body can carry replaced."""
    for pattern in _BODY_SECRETS:
        text = pattern.sub(lambda m: m.group(1) + REPLACEMENT, text)
    return text
```

```python
    text = scrub_credentials(text)
```

and add `"scrub_credentials",` to `__all__` between `"keep_headers",` and `"stream_body",`. Confirm nothing else used the old name:

Run: `rtk proxy grep -rn "call_record._scrub\|import _scrub" raven tests`
Expected: no output.

- [ ] **Step 4: Create the module**

Create `raven/agent/loop/failure_text.py`:

```python
"""The long form of a failed turn's account, for a reader who expands its row.

A failed turn already has a short account: the lane bounds it to 200 characters
(``raven.spine.events.bound_failure_text``) because a chat reply, a cron record
and the session marker's ``reason`` all carry it. This module builds the long
form beside it: the same words unbounded, with the credential shapes a provider
body can carry replaced, capped at the ceiling a call record's body already has.

The kernel cannot build it -- ``raven.spine`` imports no other raven package --
so the runner does, and hands it to the lane as a ``full_detail`` attribute on
the exception it lets out (``attach_full_detail``). The lane copies it onto
``TurnFailed.full_detail``; ``_save_broken_turn`` files it on the marker.
"""

from __future__ import annotations

from raven.contracts.llm_provider import CallRecord
from raven.providers.call_record import MAX_BODY_CHARS, scrub_credentials
from raven.spine.scheduler import describe_failure
from raven.spine.turn import AnswerlessTurnError

_ELLIPSIS = "..."


def full_failure_text(text: str) -> str:
    """``text`` with credentials scrubbed, capped at ``MAX_BODY_CHARS``."""
    text = scrub_credentials(text.strip())
    if len(text) <= MAX_BODY_CHARS:
        return text
    return text[: MAX_BODY_CHARS - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def exception_failure_text(exc: BaseException) -> str:
    """``exc``'s account worded the way ``describe_failure`` words it, without its 200-character cut."""
    name = type(exc).__name__
    if isinstance(exc, AnswerlessTurnError):
        return full_failure_text(exc.full_detail or str(exc).strip() or name)
    text = str(exc).strip()
    if not text:
        return name
    if text.startswith(f"{name}:") or text.startswith(f"{name} ") or text == name:
        return full_failure_text(text)
    return full_failure_text(f"{name}: {text}")


def response_failure_text(content: str | None, record: CallRecord | None) -> str | None:
    """A failed model call's long account: its content, then the body its call record kept.

    The content is the canonical sentence, whose detail was cut where it was
    built; the body is the provider's own response (``call_record.from_exception``),
    already scrubbed and capped. Either may be missing.
    """
    head = (content or "").strip()
    body = ((record.body if record is not None else None) or "").strip()
    if body and body not in head:
        head = f"{head}\n\n{body}" if head else body
    return full_failure_text(head) if head else None


def attach_full_detail(exc: BaseException) -> str | None:
    """Hand the lane ``exc``'s long account when it says more than the short one.

    Set as ``exc.full_detail`` -- None when the bounded text already is the whole
    account, so the row offers nothing to open -- and returned. Recomputed from
    the exception on every call, so the turn path's call and the runner's file the
    same text, and an attribute some library put there is never passed on.
    """
    long = exception_failure_text(exc)
    detail = long if long != describe_failure(exc) else None
    try:
        setattr(exc, "full_detail", detail)
    except (AttributeError, TypeError):
        pass
    return detail
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --frozen --python 3.12 --all-extras pytest tests/test_agent_loop_failure_text.py tests/test_llm_call_record.py -q -p no:randomly`
Expected: PASS.

Run: `uv run --frozen --python 3.12 lint-imports`
Expected: every contract kept (the new module is in `raven.agent`, which may import `raven.providers` and `raven.spine`).

- [ ] **Step 6: Stage**

```bash
/usr/bin/git add raven/providers/call_record.py raven/agent/loop/failure_text.py tests/test_agent_loop_failure_text.py
```

---

### Task 3: The turn path files and hands on the long account

**Files:**
- Modify: `raven/agent/loop/_shared.py:210-230` (`LoopOutcome.error_full`)
- Modify: `raven/agent/loop/turn_path.py` -- imports (near line 98); `_run_agent_loop`'s `error_detail` init (line 804), its model-error give-up branch (line 1786), its salvage reset (line 2087) and its `LoopOutcome(...)` (line 2097); `_process_message`'s raise (line 2689) and its `AnswerlessTurnError` / `Exception` arms (lines 2710-2730); `_save_broken_turn` (lines 2828-2910)
- Modify: `raven/agent/loop/main.py:991-999` (`run_turn`'s `except BaseException` arm) and its imports
- Test: `tests/test_agent_loop_broken_turn.py`, `tests/test_agent_loop_llm_error_patience.py`, `tests/test_agent_loop_run_emit.py`

**Interfaces:**
- Consumes: `attach_full_detail`, `response_failure_text` (Task 2); `AnswerlessTurnError(..., full_detail=...)` (Task 1).
- Produces:
  - `LoopOutcome.error_full: str | None = None`
  - `_save_broken_turn(..., *, status, reason=None, full_detail=None, inbound_original=None)` writing `turn_ended.full_detail` when `full_detail` is truthy.
  - Every exception `AgentLoop.run_turn` lets out (other than a cancel) carries `full_detail` (a string or None).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_loop_broken_turn.py`:

```python
@pytest.mark.asyncio
async def test_a_long_crash_keeps_its_whole_account_on_the_marker(workspace, monkeypatch):
    """The marker's ``reason`` stays at 200 characters for every reader it has;
    ``full_detail`` beside it keeps what the cut removed, scrubbed, for a client
    that lets the reader open the row."""
    agent = _agent(workspace, DyingProvider([LLMResponse(content="unused", finish_reason="stop")]))
    secret = "sk-or-v1-" + "b" * 40
    message = "the engine room flooded: " + "w" * 400 + f" key {secret}"

    async def explode(*args: Any, **kwargs: Any):
        raise RuntimeError(message)

    monkeypatch.setattr(agent, "_run_agent_loop", explode)
    with pytest.raises(RuntimeError) as raised:
        await agent._process_message(_make_msg("hello"))

    ended = _persisted(workspace)[-1]["turn_ended"]
    assert len(ended["reason"]) == 200 and ended["reason"].endswith("...")
    assert ended["full_detail"].startswith("RuntimeError: the engine room flooded: " + "w" * 400)
    assert secret not in ended["full_detail"]
    assert raised.value.full_detail == ended["full_detail"]


@pytest.mark.asyncio
async def test_a_short_crash_files_no_second_copy(workspace, monkeypatch):
    agent = _agent(workspace, DyingProvider([LLMResponse(content="unused", finish_reason="stop")]))

    async def explode(*args: Any, **kwargs: Any):
        raise RuntimeError("the engine room flooded")

    monkeypatch.setattr(agent, "_run_agent_loop", explode)
    with pytest.raises(RuntimeError):
        await agent._process_message(_make_msg("hello"))

    assert "full_detail" not in _persisted(workspace)[-1]["turn_ended"]
```

Append to `tests/test_agent_loop_llm_error_patience.py`:

```python
class _RefusedWithABody(LLMProvider):
    """One error response whose call record kept the provider's whole body."""

    def __init__(self, body: str):
        super().__init__(api_key="test")
        self._body = body

    def get_default_model(self) -> str:
        return "stub"

    _CHAT_RETRY_DELAYS = ()  # the provider's own ladder, exhausted at once

    async def chat(self, *args, **kwargs):
        from raven.contracts.llm_provider import CallRecord

        return LLMResponse(
            content="Error calling LLM (rate_limit@openrouter): Provider returned error",
            finish_reason="error",
            error_classification=ErrorClassification("rate_limit", retryable=False),
            call_record=CallRecord(http_status=429, body=self._body),
        )

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover - the non-stream path is under test
        raise NotImplementedError


@pytest.mark.asyncio
async def test_a_call_the_ladder_gave_up_on_keeps_the_body_its_record_kept(workspace):
    """The canonical sentence is the bounded report; the body behind it -- which
    provider refused, from which pool, and what to do about it -- rides the
    failure as its long account."""
    body = (
        '{"error":{"code":429,"metadata":{"raw":"z-ai/glm-5.3-flash is temporarily rate-limited upstream",'
        '"provider_name":"Z.AI","limit_source":"upstream_provider_shared_pool"}}}'
    )
    agent = _agent(workspace, _RefusedWithABody(body), delays=())

    with pytest.raises(AnswerlessTurnError) as failed:
        await _turn(agent)

    assert str(failed.value) == "Error calling LLM (rate_limit@openrouter): Provider returned error"
    assert failed.value.full_detail == f"{failed.value}\n\n{body}"
```

Append to `tests/test_agent_loop_run_emit.py`:

```python
async def test_run_turn_hands_the_lane_the_long_account_of_a_crash(tmp_path):
    """Every agent turn's failure leaves through run_turn, so that is where the
    exception gets the long account the kernel cannot build -- including a
    crash raised before the turn path's own arms could file a marker."""
    loop = AgentLoop(provider=_FakeChatProvider([]), workspace=tmp_path)
    _stub_edges(loop)
    message = "the setup fell over: " + "q" * 400

    async def _crashes(req, emit, drain, **kwargs):
        raise RuntimeError(message)

    loop._run_turn = _crashes
    with pytest.raises(RuntimeError) as raised:
        await loop.run_turn(_req("hi"), _EmitCollector(), _drain)

    assert raised.value.full_detail == f"RuntimeError: {message}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen --python 3.12 --all-extras pytest tests/test_agent_loop_broken_turn.py tests/test_agent_loop_llm_error_patience.py tests/test_agent_loop_run_emit.py -q -p no:randomly -k "whole_account or second_copy or body_its_record or long_account_of_a_crash"`
Expected: FAIL -- `KeyError: 'full_detail'` on the marker, `AttributeError` on `full_detail`, and `assert None == ...` on the ladder test.

- [ ] **Step 3: Give `LoopOutcome` the long account**

In `raven/agent/loop/_shared.py`, add to the `LoopOutcome` docstring, after the `error` paragraph:

```text
    ``error_full`` is the same failure at length, when the loop has more than
    the bounded ``error`` says: the failed call's content unbounded, then the
    response body its call record kept. The caller raises it with the error
    so a reader who expands the row can read it.
```

and the field after `error`:

```python
    error_full: str | None = None
```

- [ ] **Step 4: Fill it where the Model-Error Ladder gives up**

In `raven/agent/loop/turn_path.py`, add the import after `from raven.spine.turn import AnswerlessTurnError`:

```python
from raven.agent.loop.failure_text import attach_full_detail, response_failure_text
```

Initialize it beside `error_detail` (line 804):

```python
        status = "completed"
        error_detail: str | None = None
        error_full: str | None = None
```

In the give-up branch (line 1786):

```python
                    status = "error"
                    error_detail = _llm_failure_detail(clean, verdict)
                    error_full = response_failure_text(clean, response.call_record)
                    break
```

In the salvage branch where `error_detail = None` (line 2087), clear it too:

```python
                error_detail = None
                error_full = None
```

and build the outcome with it (line 2097):

```python
        outcome = LoopOutcome(status=status, error=error_detail, error_full=error_full)
```

- [ ] **Step 5: Raise with it, attach it, file it**

In `_process_message` (line 2689):

```python
                    if outcome.error:
                        raise AnswerlessTurnError(outcome.error, full_detail=outcome.error_full)
```

In the `except AnswerlessTurnError as exc:` arm:

```python
            self._save_broken_turn(
                session,
                all_msgs,
                persist_from,
                None,
                streamed,
                status="failed",
                reason=str(exc),
                full_detail=attach_full_detail(exc),
            )
            raise
```

In the `except Exception as exc:` arm:

```python
            self._save_broken_turn(
                session,
                live["messages"],
                persist_from,
                None,
                streamed,
                status="failed",
                # Bounded the way the lane bounds the same crash for its event:
                # an arbitrary exception message is filed in a session a reader
                # and a model both read back.
                reason=bound_failure_text(str(exc)),
                full_detail=attach_full_detail(exc),
            )
            raise
```

In `_save_broken_turn`, add the keyword after `reason`:

```python
        status: str,
        reason: str | None = None,
        full_detail: str | None = None,
        inbound_original: str | None = None,
```

add one sentence to its docstring's marker bullet (after "...(see ``_marker_failure``)."):

```text
          ``turn_ended.full_detail`` is the long account beside the bounded
          ``reason``, filed only when there is more to it.
```

and write it on the marker:

```python
            marker: dict[str, Any] = {
                "role": "assistant",
                "content": f"(turn {word})",
                "turn_ended": {
                    "status": status,
                    **({"reason": reason} if reason else {}),
                    **({"full_detail": full_detail} if full_detail else {}),
                },
            }
```

- [ ] **Step 6: Attach it where every agent turn's failure leaves**

In `raven/agent/loop/main.py`, add the import beside the other `raven.agent.loop` imports:

```python
from raven.agent.loop.failure_text import attach_full_detail
```

and in `run_turn`'s `except BaseException as exc:` arm, before `failure = exc`:

```python
        except BaseException as exc:
            # Recorded, never handled: the turn's error belongs to the caller.
            # Held only so the run record below can say why the turn ended --
            # the one record an operator reads to diagnose a failure used to
            # claim ``failed`` and give no reason, while the finalization
            # branch saved the full text for a *successful* turn whose compile
            # step erred.
            if isinstance(exc, Exception):
                attach_full_detail(exc)
            failure = exc
            raise
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --frozen --python 3.12 --all-extras pytest tests/test_agent_loop_broken_turn.py tests/test_agent_loop_llm_error_patience.py tests/test_agent_loop_run_emit.py tests/test_agent_loop_empty_recovery.py tests/test_agent_loop_failure_text.py -q -p no:randomly`
Expected: PASS, the existing tests in these files included (the bounded `str(exc)` assertions in `test_agent_loop_llm_error_patience.py` are unchanged).

- [ ] **Step 8: Stage**

```bash
/usr/bin/git add raven/agent/loop/_shared.py raven/agent/loop/turn_path.py raven/agent/loop/main.py tests/test_agent_loop_broken_turn.py tests/test_agent_loop_llm_error_patience.py tests/test_agent_loop_run_emit.py
```

---

### Task 4: The wire carries it

**Files:**
- Modify: `raven/rpc/spine.py:516-541` (`RpcOutlet.emit_error`) and `raven/rpc/spine.py:626-645` (the `TurnFailed` branch of the sink)
- Modify: `rpc-schema/openrpc.json` (`components.schemas.ErrorEvent.properties.payload.properties`, `components.schemas.TranscriptTurnEnded.properties`)
- Modify: `raven/rpc/models.py:779-792` (`ErrorEventPayload`), `raven/rpc/models.py:3392-3396` (`TranscriptTurnEnded`)
- Regenerate: `ui-web/src/rpc/generated.ts`, `ui-tui/src/rpc/generated.ts`
- Test: `tests/test_rpc_spine.py`

**Interfaces:**
- Consumes: `TurnFailed.full_detail` (Task 1); exceptions carrying `full_detail` (Task 3).
- Produces:
  - `RpcOutlet.emit_error(conversation_id, code, message, reason, detail="", target=None, turn_id="", full_detail="")`; the `error` payload gains `full_detail` when non-empty.
  - Wire: `error.payload.full_detail?: string`; `TranscriptTurnEnded.full_detail?: string`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rpc_spine.py`:

```python
async def test_outlet_emit_error_includes_the_long_account_when_present():
    emitter = FakeEmitter()
    outlet = RpcOutlet("tui", emitter)
    await outlet.emit_error("tui:c1", -32099, "turn_failed", "internal", "short", full_detail="the long one")
    await outlet.emit_error("tui:c1", -32099, "turn_failed", "internal", "short")
    assert emitter.emitted[0][1]["payload"]["full_detail"] == "the long one"
    assert "full_detail" not in emitter.emitted[1][1]["payload"]


async def test_a_failed_turn_sends_the_runners_long_account():
    class _BoomLoop:
        async def run_turn(self, req, emit, drain, *, stream, usage_sink=None) -> TurnOutcome:
            exc = RuntimeError("boom")
            exc.full_detail = "RuntimeError: boom, at length"
            raise exc

    emitter = FakeEmitter()
    scheduler, hub, turn_ids, teardown = build_rpc_spine(_BoomLoop(), emitter)
    try:
        handle = scheduler.submit(TurnRequest(origin=Origin.USER, source=_src(), text="hi", conversation="tui:c1"))
        await handle.result()
    finally:
        await teardown()

    payload = emitter.emitted[-1][1]["payload"]
    assert payload["detail"] == "RuntimeError: boom"
    assert payload["full_detail"] == "RuntimeError: boom, at length"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen --python 3.12 --all-extras pytest tests/test_rpc_spine.py -q -p no:randomly -k "long_account"`
Expected: FAIL -- `TypeError: emit_error() got an unexpected keyword argument 'full_detail'` and `KeyError: 'full_detail'`.

- [ ] **Step 3: Implement the outlet**

In `raven/rpc/spine.py`, extend `emit_error`:

```python
    async def emit_error(
        self,
        conversation_id: str,
        code: int,
        message: str,
        reason: str,
        detail: str = "",
        target: dict[str, str] | None = None,
        turn_id: str = "",
        full_detail: str = "",
    ) -> None:
```

add to its docstring:

```text
        ``full_detail`` is the failure's long account beside the bounded
        ``detail``, sent only when the runner had more to say.
```

and to its payload, after `detail`:

```python
        if full_detail:
            payload["full_detail"] = full_detail
```

In the sink's `TurnFailed` branch, pass it:

```python
            if not event.cancelled:
                await outlet.emit_error(
                    event.conversation_id,
                    _TURN_FAILED_CODE,
                    "turn_failed",
                    "internal",
                    event.error or "",
                    target,
                    # The ending turn's own id, for the same reason
                    # ``emit_complete`` above takes it rather than reading the
                    # lane slot: the slot may hold a client turn that has not
                    # started yet.
                    turn_id=event.turn_id,
                    full_detail=event.full_detail or "",
                )
```

- [ ] **Step 4: Extend the contract**

Edit `rpc-schema/openrpc.json` with a script so its formatting is untouched:

```bash
.venv/bin/python - <<'EOF'
import json
from pathlib import Path

path = Path("rpc-schema/openrpc.json")
raw = path.read_text(encoding="utf-8")
doc = json.loads(raw)
schemas = doc["components"]["schemas"]
def long_account(sibling):
    return (
        f"The failure's long account beside the bounded {sibling}, for a reader who expands the row: "
        "the runner's own text, credentials scrubbed, capped at 8000 characters. Absent when the "
        "bounded text already is the whole account."
    )
payload = schemas["ErrorEvent"]["properties"]["payload"]["properties"]
items = list(payload.items())
at = [k for k, _ in items].index("detail") + 1
items.insert(at, ("full_detail", {"type": "string", "description": long_account("detail")}))
schemas["ErrorEvent"]["properties"]["payload"]["properties"] = dict(items)
schemas["TranscriptTurnEnded"]["properties"]["full_detail"] = {"type": "string", "description": long_account("reason")}
out = json.dumps(doc, indent=2, ensure_ascii=False) + ("\n" if raw.endswith("\n") else "")
path.write_text(out, encoding="utf-8")
EOF
/usr/bin/git diff --stat rpc-schema/openrpc.json
```

Expected: about 8 insertions, 0 deletions. If the stat shows more, the dump changed the file's formatting: restore it (`/usr/bin/git checkout rpc-schema/openrpc.json`) and insert the two properties by hand at the same places.

In `raven/rpc/models.py`, mirror both:

```python
class ErrorEventPayload(_Strict):
    code: int
    turn_id: str = Field(
        "",
        description=(
            "The turn this failure belongs to. Empty when the emitter did not know it; "
            "a consumer that correlates a request to a turn must not treat an empty value as its own."
        ),
    )
    message: str
    reason: Literal["cancelled_by_client", "internal"] | None = None
    detail: str | None = None
    full_detail: str | None = Field(
        None,
        description=(
            "The failure's long account beside the bounded detail, for a reader who expands the row: "
            "the runner's own text, credentials scrubbed, capped at 8000 characters. Absent when the "
            "bounded text already is the whole account."
        ),
    )
    target: DirectTarget | None = None
```

```python
class TranscriptTurnEnded(_Strict):
    """Why a turn's transcript stops where it does."""

    status: str = Field(..., description="'cancelled' (a person stopped it) or 'failed'.")
    reason: str | None = None
    full_detail: str | None = Field(
        None,
        description=(
            "The failure's long account beside the bounded reason, for a reader who expands the row: "
            "the runner's own text, credentials scrubbed, capped at 8000 characters. Absent when the "
            "bounded text already is the whole account."
        ),
    )
```

- [ ] **Step 5: Regenerate both clients**

```bash
ln -sfn "$MAIN/ui-web/node_modules" ui-web/node_modules
ln -sfn "$MAIN/ui-tui/node_modules" ui-tui/node_modules
cmp "$MAIN/ui-web/package-lock.json" ui-web/package-lock.json && cmp "$MAIN/ui-tui/package-lock.json" ui-tui/package-lock.json && echo "lockfiles match"
node ui-web/scripts/gen-rpc-client.mjs
node ui-tui/scripts/gen-rpc-types.mjs
node ui-web/scripts/gen-rpc-client.mjs --check && node ui-tui/scripts/gen-rpc-types.mjs --check && echo "clients in sync"
grep -c "full_detail" ui-web/src/rpc/generated.ts ui-tui/src/rpc/generated.ts
```

Expected: "lockfiles match" (if not, stop: a differing lockfile needs its own install, not a link), "clients in sync", and at least 2 `full_detail` lines in each generated file. Both `node_modules` paths are gitignored (`.gitignore` lists `ui-web/node_modules` and `ui-tui/node_modules`), so the links never show up as changes.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen --python 3.12 --all-extras pytest tests/test_rpc_spine.py tests/test_rpc_schema_match.py tests/test_rpc_session.py tests/test_rpc_terminal.py -q -p no:randomly`
Expected: PASS.

- [ ] **Step 7: Stage**

```bash
/usr/bin/git add raven/rpc/spine.py raven/rpc/models.py rpc-schema/openrpc.json ui-web/src/rpc/generated.ts ui-tui/src/rpc/generated.ts tests/test_rpc_spine.py
```

---

### Task 5: The web client opens a failed row

**Files:**
- Modify: `ui-web/src/features/transcript/types.ts:209-217` (`NoteData`), `ui-web/src/features/transcript/types.ts:373` (`HistoryMessage.turn_ended`)
- Modify: `ui-web/src/features/transcript/store.ts:414-428` (`note`), `ui-web/src/features/transcript/store.ts:1712-1721` (the replayed marker)
- Modify: `ui-web/src/features/transcript/mount.tsx:135-138` (`note`)
- Modify: `ui-web/src/state/session/conversation.ts:91-108` (`noteRow`)
- Modify: `ui-web/src/state/session/stages.ts:259-260` (the `error` arm)
- Modify: `ui-web/src/features/transcript/TranscriptPage.tsx:1152-1172` (`NoteView`)
- Modify: `ui-web/src/styles/page.css:1986-1996` (`.tnote`)
- Modify: `i18n/messages.json` (two keys); regenerate `ui-tui/src/i18n/messages.generated.ts`
- Test: `ui-web/src/features/transcript/TranscriptPage.test.tsx`, `ui-web/src/state/session/pipeline.events.test.ts`

**Interfaces:**
- Consumes: `error.payload.full_detail` and `TranscriptTurnEnded.full_detail` in the generated types (Task 4).
- Produces:
  - `NoteData.full: string` (empty when there is none).
  - `note(lane, label, detail, opts?: { quiet?: boolean; retry?: (() => void) | null; full?: string | null })`, the same option on `mount.note` and `noteRow`.

- [ ] **Step 1: Add the strings**

```bash
.venv/bin/python - <<'EOF'
import json
from pathlib import Path

path = Path("i18n/messages.json")
raw = path.read_text(encoding="utf-8")
doc = json.loads(raw)
ui = doc["ui"]
added = {
    "gui.transcript.error_expand": {"en": "Show full error", "zh": "展开完整错误"},
    "gui.transcript.error_collapse": {"en": "Hide", "zh": "收起"},
}
items = list(ui.items())
at = [k for k, _ in items].index("gui.transcript.date_yesterday") + 1
for offset, pair in enumerate(added.items()):
    items.insert(at + offset, pair)
doc["ui"] = dict(items)
path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + ("\n" if raw.endswith("\n") else ""), encoding="utf-8")
EOF
/usr/bin/git diff --stat i18n/messages.json
node ui-tui/scripts/gen-i18n.mjs
node ui-tui/scripts/gen-i18n.mjs --check && echo "tui catalogue in sync"
```

Expected: `i18n/messages.json` shows 8 insertions, 0 deletions (restore and insert by hand if the dump reformatted the file), and "tui catalogue in sync".

- [ ] **Step 2: Write the failing tests**

Append inside the replay `describe` block of `ui-web/src/features/transcript/TranscriptPage.test.tsx` that holds "replays a turn that died on a model error as the red row the live page drew":

```tsx
  it('opens a failed row to the whole account the runner kept', () => {
    const reason = 'Error calling LLM (rate_limit@openrouter): Provider returned error'
    const full = `${reason}\n\n{"error":{"metadata":{"limit_source":"upstream_provider_shared_pool"}}}`
    act(() => {
      mount.history([
        { role: 'user', text: 'hello', timestamp: iso(Date.now() - 9000) },
        {
          role: 'assistant', text: '(turn failed: rate limited)',
          turn_ended: { status: 'failed', reason, full_detail: full }, timestamp: iso(Date.now() - 3000),
        },
      ])
    })
    const row = $$('.tnote')[0] as HTMLElement
    const toggle = row.querySelector('button.transcript-note-more') as HTMLButtonElement
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(toggle.textContent).toBe('en:gui.transcript.error_expand')
    expect(row.querySelector('.transcript-note-full')).toBeNull()

    act(() => { toggle.click() })
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(toggle.textContent).toBe('en:gui.transcript.error_collapse')
    expect(row.querySelector('.transcript-note-full')?.textContent).toBe(full)

    act(() => { toggle.click() })
    expect(row.querySelector('.transcript-note-full')).toBeNull()
  })

  it('offers no toggle when the row already says everything', () => {
    act(() => {
      mount.history([
        { role: 'user', text: 'hello', timestamp: iso(Date.now() - 9000) },
        {
          role: 'assistant', text: '(turn failed: boom)',
          turn_ended: { status: 'failed', reason: 'ValueError: boom' }, timestamp: iso(Date.now() - 3000),
        },
      ])
    })
    expect($$('.tnote')[0]?.querySelector('button.transcript-note-more')).toBeNull()
  })

  it('opens an older marker to the bounded reason it kept', () => {
    const reason = `RateLimitError: ${'x'.repeat(180)}...`
    act(() => {
      mount.history([
        { role: 'user', text: 'hello', timestamp: iso(Date.now() - 9000) },
        {
          role: 'assistant', text: '(turn failed: rate limited)',
          turn_ended: { status: 'failed', reason }, timestamp: iso(Date.now() - 3000),
        },
      ])
    })
    const row = $$('.tnote')[0] as HTMLElement
    act(() => { (row.querySelector('button.transcript-note-more') as HTMLButtonElement).click() })
    expect(row.querySelector('.transcript-note-full')?.textContent).toBe(reason)
  })

  it('draws the live row and the replayed row of one failure alike', () => {
    const reason = 'Error calling LLM (rate_limit@openrouter): Provider returned error'
    const full = `${reason}\n\nthe provider's whole body`
    act(() => { mount.note(mount.failedTurnLabel(), reason, { full }) })
    const live = $$('.tnote')[0] as HTMLElement
    act(() => { (live.querySelector('button.transcript-note-more') as HTMLButtonElement).click() })
    const liveText = [live.querySelector('.tx')?.textContent, live.querySelector('.transcript-note-full')?.textContent]

    act(() => {
      mount.history([
        { role: 'user', text: 'hello', timestamp: iso(Date.now() - 9000) },
        {
          role: 'assistant', text: '(turn failed: rate limited)',
          turn_ended: { status: 'failed', reason, full_detail: full }, timestamp: iso(Date.now() - 3000),
        },
      ])
    })
    /* The last row, whether the replay cleared the live one or drew after it. */
    const rows = $$('.tnote')
    const replayed = rows[rows.length - 1] as HTMLElement
    act(() => { (replayed.querySelector('button.transcript-note-more') as HTMLButtonElement).click() })
    expect([replayed.querySelector('.tx')?.textContent, replayed.querySelector('.transcript-note-full')?.textContent]).toEqual(liveText)
  })

  it('keeps each row\'s toggle its own', () => {
    const full = `first\n\n${'y'.repeat(300)}`
    act(() => {
      mount.note(mount.failedTurnLabel(), 'first', { full })
      mount.note(mount.failedTurnLabel(), 'second', { full: `second\n\n${'z'.repeat(300)}` })
    })
    const [one, two] = $$('.tnote') as HTMLElement[]
    act(() => { (one!.querySelector('button.transcript-note-more') as HTMLButtonElement).click() })
    expect(one!.querySelector('.transcript-note-full')).not.toBeNull()
    expect(two!.querySelector('.transcript-note-full')).toBeNull()
  })
```

Append inside the `describe` block of `ui-web/src/state/session/pipeline.events.test.ts` that holds "ends a died turn the way a stop does":

```ts
  it('hands the row the long account when the failure carries one', async () => {
    const h = await harness()
    h.park.lastAsk = 'send this again'

    h.dispatch({
      type: 'error',
      payload: { message: 'turn_failed', reason: 'internal', detail: 'short', full_detail: 'the whole account' },
    })

    expect(h.did('noteRow')).toEqual([['noteRow', 'FAILED_LABEL', 'short', ['full', 'retry']]])
  })
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd ui-web
node node_modules/vitest/vitest.mjs run src/features/transcript/TranscriptPage.test.tsx src/state/session/pipeline.events.test.ts -t "long account|opens a failed row|no toggle|older marker|alike|its own"
cd ..
```

Expected: FAIL -- no `button.transcript-note-more` in the row, and `['retry']` where `['full', 'retry']` is expected.

- [ ] **Step 4: Carry the text to the row**

`ui-web/src/features/transcript/types.ts`:

```ts
export interface NoteData {
  v: number
  id: number
  kind: 'note'
  label: string
  detail: string
  /* The account at length when the runner kept more than `detail` says; empty otherwise. */
  full: string
  quiet: boolean
  retry: (() => void) | null
}
```

```ts
  turn_ended?: { status?: string; reason?: string; full_detail?: string }
```

`ui-web/src/features/transcript/store.ts` (`note`):

```ts
export function note(lane: Lane, label: string, detail: string,
  opts?: { quiet?: boolean; retry?: (() => void) | null; full?: string | null } | null): NoteHandle {
  const seg: NoteData = {
    v: 0, id: nextId(), kind: 'note', label: String(label || ''), detail: String(detail || ''),
    full: String((opts && opts.full) || ''),
    quiet: !!(opts && opts.quiet), retry: (opts && opts.retry) || null,
  }
```

(the rest of the function is unchanged), and the replayed marker:

```ts
    if (m.role === 'assistant' && m.turn_ended) {
      sealTools()
      closeTurn(msOf(m.timestamp))
      const stopped = m.turn_ended.status === 'cancelled'
      /* Same promise as the live stop, checked the same way: a replayed turn
         whose whole content is this marker has no output above to keep. */
      const halted = turnKept(lane) ? 'gui.halted' : 'gui.halted_bare'
      const full = stopped ? '' : (m.turn_ended.full_detail || '')
      note(lane, stopped ? t(halted) : failedTurnLabel(),
        stopped ? '' : (m.turn_ended.reason || ''), { quiet: stopped, ...(full ? { full } : {}) })
      return
    }
```

`ui-web/src/features/transcript/mount.tsx`:

```ts
export function note(label: string, detail: string,
  opts?: { quiet?: boolean; retry?: (() => void) | null; full?: string | null } | null): NoteHandle {
  return store.note(mainLane(), label, detail, opts)
}
```

`ui-web/src/state/session/conversation.ts` (`noteRow`), option type and forward:

```ts
  opts?: { quiet?: boolean; retry?: (() => void) | null; full?: string | null; host?: unknown } | null
): NoteHandle {
  const o = opts ?? {}
  /* The row is an island segment; the handle keeps the two verbs the page still
     uses on it (noteSay's set, compressNow's remove). `host` needs no
     forwarding: the island's main lane IS the #stage transcript, and a
     delegated pane draws its own notes from its own record. */
  return drawNote(label, detail, {
    quiet: !!o.quiet,
    retry: typeof o.retry === 'function' ? o.retry : null,
    full: o.full || '',
  })
}
```

`ui-web/src/state/session/stages.ts` (`error` arm), replacing the `noteRow(...)` call:

```ts
    noteRow(p.message === 'turn_failed' ? transcript.failedTurnLabel() : (p.message || 'error'),
      p.detail || p.reason || '', {
        ...(rt.lastAsk ? { retry: () => send(rt.lastAsk) } : {}),
        ...(p.full_detail ? { full: p.full_detail } : {}),
      })
```

The existing `error`-arm expectations in `pipeline.events.test.ts` (`['retry']`) keep holding: both of those tests set `lastAsk` and send no `full_detail`. With neither, the arm now passes `{}` where it passed `null`; `noteRow` reads the two the same way (`opts ?? {}`).

- [ ] **Step 5: Draw the toggle**

The two new classes are namespaced (`transcript-note-more`, `transcript-note-full`) because `ui-web/scripts/check-class-namespace.mjs` pins the transcript domain's unprefixed classes and only lets that number shrink; an opened row is a grid so its actions stay on the first line.

`ui-web/src/features/transcript/TranscriptPage.tsx`, replacing `NoteView`:

```tsx
const NoteView = memo(function NoteView({ lane, seg }: { lane: Lane; seg: NoteData }): ReactElement {
  useSeg(lane, seg)
  const [open, setOpen] = useState(false)
  const brief = store.firstErrLine(seg.detail, 140) || seg.detail
  const whole = seg.full || seg.detail
  const tip = seg.detail ? `${seg.label} · ${seg.detail}` : seg.label
  const copied = whole ? `${seg.label} · ${whole}` : seg.label
  /* The row stays one line; what it cut opens under it -- in a tooltip it could
     be neither selected nor read on a phone. */
  const more = !!whole && whole !== brief
  const items = (): Array<{ label: string; fn: () => void }> => [
    { label: t('gui.answer.copy'), fn: () => copy(copied, t('gui.answer.copied')) },
  ]
  return (
    <div className={'tnote in' + (seg.quiet ? '' : ' bad') + (open ? ' open' : '')}
      title={open ? undefined : tip} ref={ctxRef(items)}>
      <span className="tx">{brief ? `${seg.label} · ${brief}` : seg.label}</span>
      {more ? (
        <button className="transcript-note-more" aria-expanded={open} onClick={() => setOpen((was) => !was)}>
          {open ? t('gui.transcript.error_collapse') : t('gui.transcript.error_expand')}
        </button>
      ) : null}
      {seg.retry ? (
        <button className="rt" onClick={() => {
          const retry = seg.retry
          const i = lane.segs.indexOf(seg)
          if (i >= 0) { lane.segs.splice(i, 1); store.nudge(lane) }
          retry?.()
        }}>{t('gui.retry')}</button>
      ) : null}
      {open ? <pre className="transcript-note-full">{whole}</pre> : null}
    </div>
  )
})
```

`ui-web/src/styles/page.css`, replacing the two `.tnote .rt` rules:

```css
/* The actions a failed row can offer. Plain text so the row stays a line:
   a bordered button would rebuild the card one corner at a time. */
.tnote .rt, .tnote .transcript-note-more { flex: none; color: inherit; opacity: .75; text-decoration: underline; text-underline-offset: 2px; }
.tnote .rt:hover, .tnote .transcript-note-more:hover { opacity: 1; }
/* An opened row is a grid (text, toggle, Retry, slack), not a wrapping flex
   line: its text is nowrap, so under flex-wrap it took a line to itself whenever
   it and the actions did not fit, and the toggle just clicked dropped below it.
   Here the text still shrinks to an ellipsis, the actions stay beside it, and the
   account takes the row under them, wrapping and scrolling inside it: a body can
   be one token thousands of characters long. */
.tnote.open { display: grid; grid-template-columns: minmax(0, max-content) max-content max-content 1fr; }
.tnote .transcript-note-full {
  grid-column: 1 / -1; min-width: 0; max-height: 320px; overflow: auto;
  margin: 2px 0 0; padding: 2px 0 2px 10px; border-left: 2px solid var(--line);
  font: inherit; color: inherit; white-space: pre-wrap; overflow-wrap: anywhere; user-select: text;
}
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd ui-web
node node_modules/vitest/vitest.mjs run src/features/transcript/TranscriptPage.test.tsx src/state/session/pipeline.events.test.ts scripts/gates/i18n-keys.test.mjs
node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json
node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json --listFiles | grep -c "$WT/ui-web/src/features/transcript/TranscriptPage.tsx"
cd ..
```

Expected: vitest PASS (the existing snapshot "keeps its rendered shape" included: it has no failed row); `tsc` exits 0; the `--listFiles` count is 1, which proves the compiler read the worktree's file and not the donor checkout's. If the `TranscriptPage` snapshot moved, read the diff: a change outside `.tnote` rows means the edit leaked.

- [ ] **Step 7: Stage**

```bash
/usr/bin/git add ui-web/src/features/transcript/types.ts ui-web/src/features/transcript/store.ts ui-web/src/features/transcript/mount.tsx ui-web/src/state/session/conversation.ts ui-web/src/state/session/stages.ts ui-web/src/features/transcript/TranscriptPage.tsx ui-web/src/styles/page.css i18n/messages.json ui-tui/src/i18n/messages.generated.ts ui-web/src/features/transcript/TranscriptPage.test.tsx ui-web/src/state/session/pipeline.events.test.ts
```

---

### Task 6: The record, and every gate

**Files:**
- Modify: `CONTEXT.md:813-824` (**AnswerlessTurnError**)
- Stage: `docs/specs/2026-10-15-turn-continuation-design.md`, `docs/plans/2026-10-15-full-failure-text.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a clean gate run to quote in the pull request.

- [ ] **Step 1: Record the field in the domain map**

In `CONTEXT.md`, the **AnswerlessTurnError** entry, after "...a vendor body is not history the model should spend context on or try to answer." insert:

```text
Beside that bounded report a failed turn may carry `full_detail`, the same account at length --
credentials scrubbed, capped at the call record's body ceiling. The runner builds it
(`agent/loop/failure_text.py`), since the kernel imports nothing to scrub with, and sets it on the
exception it lets out; the lane copies it onto `TurnFailed.full_detail`, and it is filed on the
marker as `turn_ended.full_detail`, only when the bounded text left something out.
```

- [ ] **Step 2: Run the Python gates**

```bash
uv run --frozen --python 3.12 --all-extras pytest tests/test_spine_scheduler_lane.py tests/test_spine_events.py tests/test_agent_loop_broken_turn.py tests/test_agent_loop_llm_error_patience.py tests/test_agent_loop_run_emit.py tests/test_agent_loop_empty_recovery.py tests/test_agent_loop_failure_text.py tests/test_rpc_spine.py tests/test_rpc_schema_match.py tests/test_rpc_session.py tests/test_rpc_terminal.py tests/test_llm_call_record.py tests/test_gateway_spine.py tests/test_cli_one_shot_spine.py tests/test_kernel_budget.py tests/test_kernel_closure.py tests/test_contracts_two_tier_ledger.py -q -p no:randomly
make lint-python lint-imports lint-types
uv run --frozen --python 3.12 --all-extras pytest -q -p no:randomly
```

Expected: the targeted run passes; the three lint targets exit 0. The full suite is not expected to be green on this box and must not be run with `-x`: at `afdc04f89` it was 2 failed / 25898 passed / 113 skipped in 5m11s, both failures pre-existing (`test_cli_serve_commands.py::TestTheSupervisorsStopOfItsGateway::test_a_stubborn_gateway_is_killed_with_its_group_in_one_step`, the #712 flake, and `test_install_script.py::test_resolve_node_dir_answers_each_case_it_exists_for`), plus the five proxy tests in `tests/test_config_update_providers.py` that fail whenever the box proxy is exported. Compare failing ids against those, not counts; a failing id outside them is this branch's until a run of the same id on a clean `origin/main` worktree fails too. A shutdown fault can report 139 after printing the summary -- read the printed summary, and if it is short of the expected count, re-run that file alone.

- [ ] **Step 3: Run the web client and contract gates**

```bash
cd ui-web
node node_modules/vitest/vitest.mjs run --reporter=dot
node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json
cd ..
node ui-web/scripts/gen-rpc-client.mjs --check
node ui-tui/scripts/gen-rpc-types.mjs --check
node ui-tui/scripts/gen-i18n.mjs --check
```

Expected: the generators and `tsc` exit 0. The whole `ui-web` vitest run includes the gate root (`ui-web/scripts/gates/`), which a `src`-scoped run would miss; two of its files have been red on `main` on this box, and the localStorage cases fail here under node v26 with or without the change. Any other failing file is this branch's until the same file fails on a clean `origin/main` worktree too.

- [ ] **Step 4: Run the repository gates over the uncommitted change**

```bash
/usr/bin/git add CONTEXT.md docs/specs/2026-10-15-turn-continuation-design.md docs/plans/2026-10-15-full-failure-text.md
PYTHONPATH=. uv run --frozen --python 3.12 --extra dev python scripts/check_source_language.py HEAD
PYTHONPATH=. uv run --frozen --python 3.12 --extra dev python scripts/check_large_files.py HEAD
/usr/bin/git status --short
```

Expected: both scripts exit 0. `HEAD` (a bare revision) diffs the working tree, staged and unstaged, against the branch tip, which is how the gates see uncommitted work; the Makefile's `origin/main..HEAD` would see nothing yet. The bare form has no base revision, so `check_source_language.py` also loses its "file already carried CJK" pass and flags the two new zh strings; judge it by the range form over a snapshot, and re-run `make check-source-language check-large-files` after the commit. `git status` lists only the files this plan staged.

---

### Task 7: The real web client shows it

A throwaway check outside the repository: a local OpenAI-compatible stub that refuses every call with a long body, an isolated `raven web` serving this worktree, and a headless browser.

**Files:**
- Create (outside the repo): `/tmp/ffx/stub/stub_llm.py`, `/tmp/ffx/home/config.json`, `/tmp/ffx/drive/check_full_error.py`

**Interfaces:**
- Consumes: the whole branch.
- Produces: `/tmp/ffx/expanded.png` and a pass/fail line.

- [ ] **Step 1: Build this worktree's web client**

```bash
cd "$WT/ui-web"
node node_modules/vite/bin/vite.js build
cd .. && .venv/bin/python ui-web/build.py
grep -c "gui.transcript.error_expand" ui-web/dist/index.html
```

Expected: a count of at least 1 (the key is a string literal the bundle keeps).

- [ ] **Step 2: Start the stub**

```bash
mkdir -p /tmp/ffx/stub /tmp/ffx/home /tmp/ffx/drive
cat > /tmp/ffx/stub/stub_llm.py <<'EOF'
"""An OpenAI-compatible endpoint that refuses every chat call with a long body."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = json.dumps({
    "error": {
        "message": "stub refusal",
        "type": "invalid_request_error",
        "echo": "Authorization: Bearer sk-or-v1-" + "s" * 48,
        "padding": "p" * 6000,
        "tail": "TAIL-MARKER",
    }
})


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, text):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, json.dumps({"object": "list", "data": [{"id": "stub", "object": "model"}]}))

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send(400, BODY)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", 18931), Handler).serve_forever()
EOF
cat > /tmp/ffx/home/config.json <<'EOF'
{
  "memory": {"backend": null},
  "providers": {"custom": {"apiBase": "http://127.0.0.1:18931/v1", "apiKey": "stub-key", "models": ["custom/stub"]}},
  "agents": {"defaults": {"provider": "custom", "model": "custom/stub"}}
}
EOF
cd /tmp/ffx/stub && python3 -I stub_llm.py &
```

Run it as a background task; then `curl -s http://127.0.0.1:18931/v1/models` must print the one-model list.

- [ ] **Step 3: Start an isolated `raven web` from the worktree**

```bash
cd /tmp/ffx && RAVEN_HOME=/tmp/ffx/home BROWSER=true \
  PYTHONPATH="$WT:$WT/plugins-dist/ppt-engine:$WT/plugins-dist/design-engine" \
  "$WT/.venv/bin/python" -m raven web --port 18932 --foreground
```

Run it as a background task from `/tmp/ffx` (never from a checkout: `-m` puts the cwd first on `sys.path`). Confirm it serves this worktree: `curl -s -o /dev/null -w '%{size_download}' http://127.0.0.1:18932/` equals `stat -c %s $WT/ui-web/dist/index.html`.

- [ ] **Step 4: Drive the web client**

```bash
cat > /tmp/ffx/drive/check_full_error.py <<'EOF'
"""Send one message, open the failed row, and check what it shows."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:18932"
token = json.loads(Path("/tmp/ffx/home/serve.json").read_text())["token"]
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(BASE)
    page.evaluate("t => fetch('/auth/exchange', {method: 'POST', body: JSON.stringify({token: t})})", token)
    page.goto(BASE)
    page.wait_for_selector("#ta", timeout=30000)
    page.wait_for_selector("#splash", state="detached", timeout=30000)
    assert not page.is_visible("#onb"), "the first-run wizard is still up: check config.json"
    page.fill("#ta", "say hi")
    page.keyboard.press("Enter")
    row = page.wait_for_selector(".tnote.bad", timeout=90000)
    toggle = row.query_selector("button.transcript-note-more")
    assert toggle is not None, "the failed row offers no toggle"
    toggle.click()
    shown = row.query_selector(".transcript-note-full").inner_text()
    assert "TAIL-MARKER" in shown, "the opened row stops short of the body's end"
    assert "sk-or-v1-" not in shown and "[redacted]" in shown, "the echoed credential reached the page"
    overflow = page.evaluate("() => { const s = document.querySelector('#scroll'); return s.scrollWidth - s.clientWidth }")
    assert overflow <= 1, f"the transcript scrolls sideways by {overflow}px"
    page.screenshot(path="/tmp/ffx/expanded.png")
    browser.close()
print("full failure text: ok")
EOF
cd /tmp/ffx/drive && env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  "$WT/.venv/bin/python" -I check_full_error.py
```

Expected: `full failure text: ok`. Open `/tmp/ffx/expanded.png` and look at it: one red row, the toggle reading "收起" or "Hide", the account below it in a bordered block.

- [ ] **Step 5: Stop everything**

Stop both background tasks, then confirm nothing kept the isolated home:

```bash
/usr/bin/grep -la "RAVEN_HOME=/tmp/ffx/home" /proc/[0-9]*/environ 2>/dev/null | head
ss -ltnp | grep -E ":1893[12] " || echo "ports free"
```

Expected: no environ match (other than a just-exited grep) and "ports free". The plain `grep` is a shell function here that cannot read `/proc/*/environ`; call the binary. Stopping the `raven web` launcher can leave its `raven gateway` child running: check for it and stop it directly.

---

### Task 7a: Carry the body a client library keeps on its exception

Added by controller ruling after Task 7's real web client check failed. With an OpenAI-compatible
endpoint that refuses with `{"error": {"message": "stub refusal", ...6 KB more...}}`, litellm
raises `litellm.BadRequestError` whose `str(exc)` is only `litellm.BadRequestError:
OpenAIException - stub refusal` (55 chars). The whole parsed body is on `exc.body` (a dict),
`exc.status_code` is 400, and `exc.response.text` is empty. The streamed call re-raises that
exception out of the loop, so `attach_full_detail` (which reads only `str(exc)`) files nothing
and the row offers no toggle. `call_record.from_exception` has the same blind spot: it falls back
to `str(exc)` when the response text is empty, so a non-streamed error response's call record
(and so `LoopOutcome.error_full`) loses the body too.

**Files:**
- Modify: `raven/providers/call_record.py` (new public `exception_body`, used by `from_exception`; add it to `__all__` in alphabetical place)
- Modify: `raven/agent/loop/failure_text.py` (`exception_failure_text` appends the body)
- Test: `tests/test_llm_call_record.py`, `tests/test_agent_loop_failure_text.py`

**Interfaces:**
- Produces: `raven.providers.call_record.exception_body(exc: BaseException) -> str | None`
- `exception_failure_text(exc)` is unchanged for an exception that carries no body, so
  `attach_full_detail` still returns None for a short plain crash.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_llm_call_record.py` (reuse that file's existing imports; add `import json` only if it is not there):

```python
class _ClientError(Exception):
    """The shape litellm/openai raise: a one-line message, a status, the parsed body, an empty response."""

    def __init__(self, message: str, body: object, status_code: int = 400) -> None:
        super().__init__(message)
        self.body = body
        self.status_code = status_code
        self.response = type("R", (), {"text": "", "headers": {}})()


def test_an_exception_body_is_read_when_the_response_kept_none():
    body = {"error": {"message": "stub refusal", "tail": "TAIL-MARKER"}}
    exc = _ClientError("litellm.BadRequestError: OpenAIException - stub refusal", body)
    assert call_record.exception_body(exc) == json.dumps(body, ensure_ascii=False, default=str)


def test_a_response_text_wins_over_the_parsed_body():
    exc = _ClientError("short", {"error": {"message": "x"}})
    exc.response = type("R", (), {"text": "the raw response", "headers": {}})()
    assert call_record.exception_body(exc) == "the raw response"


def test_a_plain_exception_has_no_body():
    assert call_record.exception_body(ValueError("boom")) is None
    plain = ValueError("boom")
    plain.body = {"not": "an http error"}
    assert call_record.exception_body(plain) is None


def test_a_record_built_from_a_client_error_keeps_its_body():
    body = {"error": {"message": "stub refusal", "tail": "TAIL-MARKER"}}
    record = call_record.from_exception(_ClientError("litellm.BadRequestError: OpenAIException - stub refusal", body), status=400)
    assert record.body is not None and "TAIL-MARKER" in record.body
```

(If `tests/test_llm_call_record.py` imports the module under another name than `call_record`,
use that name; do not add a second import of the same module.)

Append to `tests/test_agent_loop_failure_text.py`:

```python
class _ClientError(Exception):
    def __init__(self, message: str, body: object, status_code: int = 400) -> None:
        super().__init__(message)
        self.body = body
        self.status_code = status_code
        self.response = type("R", (), {"text": "", "headers": {}})()


def test_a_client_error_reads_as_its_message_then_its_body():
    body = {"error": {"message": "stub refusal", "echo": f"Authorization: Bearer {KEY}", "tail": "TAIL-MARKER"}}
    exc = _ClientError("litellm.BadRequestError: OpenAIException - stub refusal", body)
    text = exception_failure_text(exc)
    assert text.startswith("_ClientError: litellm.BadRequestError: OpenAIException - stub refusal\n\n")
    assert "TAIL-MARKER" in text
    assert KEY not in text


def test_a_client_error_is_handed_on_with_its_body():
    exc = _ClientError("litellm.BadRequestError: OpenAIException - stub refusal", {"error": {"tail": "TAIL-MARKER"}})
    attached = attach_full_detail(exc)
    assert attached is not None and "TAIL-MARKER" in attached


def test_a_body_the_message_already_quotes_is_not_repeated():
    body = "the whole body"
    exc = _ClientError("APIError: the whole body", body)
    assert exception_failure_text(exc).count("the whole body") == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen --python 3.12 --all-extras pytest tests/test_llm_call_record.py tests/test_agent_loop_failure_text.py -q -p no:randomly`
Expected: FAIL -- `AttributeError: module 'raven.providers.call_record' has no attribute 'exception_body'`, and the failure-text tests miss `TAIL-MARKER`.

- [ ] **Step 3: Read the body where the client library keeps it**

In `raven/providers/call_record.py`, add above `from_exception`:

```python
def exception_body(exc: BaseException) -> str | None:
    """The body a failed call's exception carries, or None.

    The response's own text first. When the client library kept no readable
    response -- litellm reduces an OpenAI-style ``{"error": {"message": ...}}``
    body to that message in ``str(exc)``, leaves the response empty, and keeps
    the parsed body on ``exc.body`` -- the parsed body, serialized. Read only
    from an HTTP error (one that names a status code), so an unrelated
    exception's ``body`` attribute is never mistaken for a provider's answer.
    Never raises: it runs while a turn is failing, and a lookup that threw
    would replace that failure with its own.
    """
    try:
        return _exception_body(exc)
    except Exception:  # noqa: BLE001 - see docstring
        return None


def _exception_body(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - a streamed body cannot be re-read; the parsed one still can
            text = None
        if isinstance(text, str) and text.strip():
            return text
    if getattr(exc, "status_code", None) is None:
        return None
    body = getattr(exc, "body", None)
    if body is None or body == "" or body == {} or body == []:
        return None
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - the record must never be the thing that fails
        return str(body)
```

and make `from_exception` use it (its docstring and the rest unchanged):

```python
    response = getattr(exc, "response", None)
    body = exception_body(exc)
    return CallRecord(
        http_status=status,
        headers=keep_headers(getattr(response, "headers", None) if response is not None else None),
        **_body_fields(body if body else str(exc)),
    )
```

(`json` is already imported by this module.)

- [ ] **Step 4: Append it to the long account**

In `raven/agent/loop/failure_text.py`, import it beside `scrub_credentials`
(`from raven.providers.call_record import MAX_BODY_CHARS, exception_body, scrub_credentials`) and
replace the non-`AnswerlessTurnError` half of `exception_failure_text` with:

```python
    text = str(exc).strip()
    if not text:
        head = name
    elif text.startswith(f"{name}:") or text.startswith(f"{name} ") or text == name:
        head = text
    else:
        head = f"{name}: {text}"
    body = (exception_body(exc) or "").strip()
    if body and body not in head:
        head = f"{head}\n\n{body}"
    return head if head == name else full_failure_text(head)
```

Keep the function's docstring, adding one sentence at its end: "An HTTP error's body (``call_record.exception_body``) follows the message, because a client library can keep the provider's whole answer there while its message holds one line."

With no body the result is byte-identical to today's: a bare class name is returned as is, and
every other text goes through `full_failure_text`, exactly as before.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --frozen --python 3.12 --all-extras pytest tests/test_llm_call_record.py tests/test_agent_loop_failure_text.py tests/test_agent_loop_broken_turn.py tests/test_agent_loop_llm_error_patience.py tests/test_agent_loop_run_emit.py tests/test_rpc_spine.py -q -p no:randomly`
Expected: PASS (the earlier tasks' tests in these files included). Also `uv run --frozen --python 3.12 lint-imports` and ruff check + format --check on the touched files.

Then re-run Task 7 Steps 2-5 against the unmodified stub (its body keeps `"message"`): the driver must print `full failure text: ok`.

---

### Task 8: Commit, on the owner's word

- [ ] **Step 1: Show the owner the staged change and the gate results, and ask for the word to commit.** Do not commit before it (AGENTS.md 3.4).

- [ ] **Step 2: Commit once the owner says so**

Write the message to a file outside the repo, then commit it:

```bash
cat > /tmp/ffx_commit_msg.txt <<'EOF'
feat(*): let a failed turn's row open to its whole error

A failed turn showed one line cut at 140 characters of a text the runtime
had already bounded to 200, so a provider's account (which pool refused,
what to do about it) never reached the page. The runner now builds the
long form beside the bounded text -- credentials scrubbed with the call
record's scrubber, capped at its body ceiling -- and hands it to the lane
on the exception, since the kernel imports nothing to scrub with. It rides
the error event and the turn's marker, and the page's failed row opens to
it. Every 200-character text is unchanged.
EOF
/usr/bin/git commit -F /tmp/ffx_commit_msg.txt
/usr/bin/git log -1 --format=%B | .venv/bin/python -c "import sys; t=sys.stdin.read(); bad=[c for c in t if ord(c)>0x7F]; print('non-ascii:', bad); sys.exit(1 if bad else 0)"
```

Expected: the commit lands on `feat/full_failure_text`; the ASCII check prints `non-ascii: []`. Push and the pull request wait for their own word (AGENTS.md 3.6, 3.7).
