# A2A Protocol Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the host raven agent both faces of the A2A 1.0 protocol -- serving external A2A clients, and calling remote A2A agents -- while sub-agent processes get neither.

**Architecture:** `a2a-sdk` supplies the protocol core (`DefaultRequestHandler`, eleven methods) and we supply two small things: an `AgentExecutor` that turns an A2A task into one raven turn, and an aiohttp route layer that speaks the JSON-RPC binding. Outbound is a single tool on the host agent over a config section of its own. Both faces are refused in a sub-agent process, by two different gates, because only one of them passes through the tool registry.

**Tech Stack:** Python 3.12, `a2a-sdk` 1.1.2 (protobuf object model), aiohttp (server), pydantic (config), pytest.

**Spec:** `docs/specs/2026-09-13-a2a-protocol-design.md`

## Global Constraints

- **Repo rules:** `AGENTS.md` wins over anything here. Read it before the first edit.
- **Dependencies:** `uv` only. Add the SDK with `uv add a2a-sdk`. Never `pip install`, never hand-edit `pyproject.toml` dependency tables or `uv.lock`.
- **Tests:** always `uv run --frozen --all-extras pytest ...`, never bare `pytest`. A bare `uv run pytest` silently drops thousands of tests and still prints green.
- **Source language:** English only -- code, strings, log messages, test fixtures, comments. Enforced by `make check-source-language` on added lines.
- **Comments:** every new file gets a module docstring stating its purpose. Inline comments only for non-obvious logic or a hidden constraint; never to describe what the code does.
- **Commits:** Conventional Commits, all-English, ASCII-only (no em-dash, curly quotes, or ellipsis). Trailer `Co-authored-by: Claude (<your actual session model id>) <noreply@anthropic.com>` -- the real id, not copied from this document.
- **Commit authorization:** AGENTS.md 3.4 says a commit step written in a plan is **not** pre-authorization. Each task's commit step means "this is the commit boundary" -- ask the user before running it unless they have said to commit as you go.
- **Protocol version:** A2A 1.0 only. Methods are PascalCase (`SendMessage`, `GetTask`, `CancelTask`, `SendStreamingMessage`, `SubscribeToTask`). Task states are `TASK_STATE_*`. The Agent Card path is `/.well-known/agent-card.json`. A request with a missing or non-1.0 `A2A-Version` header gets `VersionNotSupportedError`.
- **Never leak internals across the trust boundary:** a raven turn that raises returns `InternalError` with no traceback text in the payload. The cause goes to this host's logs.
- **Gates before pushing:** `make lint check-commits check-large-files check-source-language`, plus the `pr-review-patterns` pre-submit sweep.

## Measured API facts

Verified 2026-09-14 against `a2a-sdk` 1.1.2 and this repo. Several of these contradict what
the SDK's own docs and naming suggest, so take them from here rather than from intuition,
and re-measure if the pinned version moves.

**`a2a-sdk`, outbound:**

| Fact | Consequence |
|---|---|
| `ClientFactory.__init__(config: ClientConfig \| None = None)` | there is no `httpx_client=` argument; it is a **field of `ClientConfig`** |
| `ClientFactory.create(card: AgentCard, ...)` | takes a card **object**, not a URL |
| `ClientFactory.create_from_url(url, ...)` is **async** | this is the one that takes a URL, and it must be `await`ed; `create` is sync, `create_from_url` is not |
| `create_from_url` appends the well-known suffix itself | pass `relative_card_path="/"` when the URL is already a complete card URL, or the fetch 404s on a doubled suffix |
| `Client.send_message(request: SendMessageRequest, *, context=None) -> AsyncIterator[StreamResponse]` | takes a **request**, not a `Message`, and is **iterated**, not awaited for one value |
| `Message` fields: `message_id, context_id, task_id, role, parts, metadata, extensions, reference_task_ids` | the field is `parts`, **not** `content` |
| `Part` fields: `text, raw, url, data, metadata, filename, media_type` | `Part(text=...)` is right; there is **no** `TextPart` export |
| `Role` values: `ROLE_UNSPECIFIED, ROLE_USER, ROLE_AGENT` | `Role.ROLE_USER` is right |

**`a2a-sdk`, inbound:**

| Fact | Consequence |
|---|---|
| `DefaultRequestHandler(agent_executor, task_store, agent_card, ...)` | `agent_card` is **required and positional-capable**; a two-argument call raises `TypeError` |
| `EventQueue.enqueue_event(event) -> None` is a **coroutine** despite the annotation | `await` it; the `-> None` is misleading, confirmed with `inspect.iscoroutinefunction` |
| There is **no** `enqueue_event_nowait` | scheduling from a sync callback needs `asyncio.create_task` |
| `enqueue_event` accepts only `Message \| Task \| TaskStatusUpdateEvent \| TaskArtifactUpdateEvent` | a plain dict is not enqueueable; build the protobuf event |
| `RequestContext` exposes `get_user_input(delimiter='\n') -> str`, and properties `message`, `task_id`, `context_id`, `current_task` | there is **no** `message_text` attribute |
| `AgentCard` / `AgentInterface` / `AgentCapabilities` / `AgentSkill` field names | as used in Task 5; verified against the protobuf descriptors |
| A fetched card's `supported_interfaces[].url` may name a **different origin** than the card | the credential was resolved for the card's origin; sending it to a card-declared origin is a leak. Require same-origin before attaching -- see Task 3 |

**This repo:**

| Fact | Consequence |
|---|---|
| `pytest-aiohttp` is **not installed** | the `aiohttp_client` / `aiohttp_server` fixtures do not exist. Use `from aiohttp.test_utils import TestClient, TestServer`, the pattern `tests/test_rpc_files.py` already uses |
| `asyncio_mode = "auto"` (pyproject.toml:426) | `@pytest.mark.asyncio` is unnecessary; an `async def test_` is collected as-is |
| `AgentLoop` / `WiringMixin` hold **no** whole `Config` | there is no `self.config`. Per-feature config arrives as its own constructor argument and attribute, the way `self.ask_user_config` does (`raven/agent/loop/main.py:320`) |
| `WsGateway.__init__(self)` takes no arguments and holds no `Config` | it exposes `self.agent_loop_factory`; anything A2A needs must be set on it the same way |
| `load_config` imports from `raven.config` **and** `raven.config.loader` | either is fine; tests use `from raven.config import load_config` |
| `Tool.parameters` is `@property @abstractmethod` (`raven/contracts/tool.py:274`), **not** a plain method | declare it `@property` and read it as `tool.parameters`, never `tool.parameters()`. `name` and `description` are properties too; only `execute` is a method |

---

### Task 1: Dependency and the `a2a` config section

**Files:**
- Modify: `pyproject.toml` (via `uv add`, not by hand)
- Modify: `raven/config/schema.py` (add classes near `AcpConfig` at line 785; add the field to `Config` after `acp: AcpConfig` at line 2187)
- Test: `tests/test_a2a_config.py`

**Interfaces:**
- Consumes: `Base` from `raven/config/schema.py:16` (pydantic `BaseModel` with a camelCase alias generator, `populate_by_name=True`).
- Produces: `A2aPeerConfig`, `A2aServerConfig`, `A2aConfig`; `Config.a2a: A2aConfig`.

- [ ] **Step 1: Add the SDK**

```bash
uv add a2a-sdk
```

Expected (measured): `pyproject.toml` gains `a2a-sdk>=1.1.2` and `uv.lock` gains **thirteen** entries -- `a2a-sdk`, the direct `protobuf`, `google-api-core`, `googleapis-common-protos`, `json-rpc`, `culsans`, and the transitive `google-auth`, `opentelemetry-api`, `proto-plus`, `pyasn1`, `pyasn1-modules`, `wrapt`, `aiologic`. Nothing is removed or downgraded; if anything is, stop and report it.

- [ ] **Step 2: Verify the SDK core imports without an ASGI stack**

```bash
uv run --frozen --all-extras python -c "
import a2a.server.request_handlers, a2a.server.agent_execution, a2a.server.tasks, a2a.server.events
from a2a.types import TaskState
print('core ok; TaskState values:', len(dict(TaskState.items())))
"
```

Expected: `core ok; TaskState values: 9`

- [ ] **Step 3: Write the failing test**

Create `tests/test_a2a_config.py`:

```python
"""The a2a config section: defaults, peer lookup keys, and camelCase wire keys."""

from raven.config.schema import A2aConfig, Config


def test_a2a_defaults_are_off_and_empty():
    cfg = A2aConfig()
    assert cfg.server.enabled is False
    assert cfg.server.token == ""
    assert cfg.peers == []


def test_config_carries_an_a2a_section():
    assert Config().a2a.server.enabled is False


def test_peer_accepts_camel_case_wire_keys():
    cfg = A2aConfig.model_validate(
        {
            "server": {"enabled": True, "token": "t0ken"},
            "peers": [{"origin": "https://peer.example.com", "authScheme": "bearer", "credential": "sekrit"}],
        }
    )
    assert cfg.server.enabled is True
    assert cfg.peers[0].origin == "https://peer.example.com"
    assert cfg.peers[0].auth_scheme == "bearer"
    assert cfg.peers[0].credential == "sekrit"
```

- [ ] **Step 4: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_config.py -v
```

Expected: FAIL, `ImportError: cannot import name 'A2aConfig'`.

- [ ] **Step 5: Add the config classes**

In `raven/config/schema.py`, after `AcpConfig` ends:

```python
class A2aPeerConfig(Base):
    """One remote A2A agent this host is allowed to call, and how to authenticate to it.

    Keyed by origin rather than by full card URL: the credential belongs to the
    host, not to one card path, and a peer that moves its card must not silently
    become an unauthenticated call.
    """

    origin: str
    auth_scheme: str = "bearer"
    credential: str = ""


class A2aServerConfig(Base):
    """The inbound A2A face. Off by default: it is a network surface for other
    people's agents, so running the gateway must not open it as a side effect."""

    enabled: bool = False
    token: str = ""
    path: str = "/a2a"


class A2aConfig(Base):
    """Both A2A faces. Neither touches the sub-agent roster -- a peer is reachable,
    not subordinate, so nothing here describes a process raven starts."""

    server: A2aServerConfig = Field(default_factory=A2aServerConfig)
    peers: list[A2aPeerConfig] = Field(default_factory=list)
```

Then in `Config`, immediately after `acp: AcpConfig = Field(default_factory=AcpConfig)`:

```python
    a2a: A2aConfig = Field(default_factory=A2aConfig)
```

- [ ] **Step 6: Run the test again**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_config.py -v
```

Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock raven/config/schema.py tests/test_a2a_config.py
git commit -m "feat(config): add the a2a section and the a2a-sdk dependency"
```

---

### Task 2: Peer credential resolution

**Files:**
- Create: `raven/a2a_client/__init__.py`
- Create: `raven/a2a_client/peers.py`
- Test: `tests/test_a2a_peers.py`

**Interfaces:**
- Consumes: `A2aConfig`, `A2aPeerConfig` from Task 1.
- Produces: `resolve_peer(config: A2aConfig, card_url: str) -> A2aPeerConfig | None` and `auth_headers(peer: A2aPeerConfig | None) -> dict[str, str]`.

This is the security boundary of the outbound half: it is the only place a credential is attached, and the model is never on this side of it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_a2a_peers.py`:

```python
"""Peer lookup is by origin, and a credential never travels to an unlisted host."""

import pytest

from raven.a2a_client.peers import auth_headers, resolve_peer
from raven.config.schema import A2aConfig

CONFIG = A2aConfig.model_validate(
    {"peers": [{"origin": "https://peer.example.com", "authScheme": "bearer", "credential": "sekrit"}]}
)


def test_known_origin_resolves_regardless_of_card_path():
    peer = resolve_peer(CONFIG, "https://peer.example.com/.well-known/agent-card.json")
    assert peer is not None and peer.credential == "sekrit"


def test_unknown_origin_resolves_to_none():
    assert resolve_peer(CONFIG, "https://evil.example.com/.well-known/agent-card.json") is None


def test_a_different_port_is_a_different_origin():
    assert resolve_peer(CONFIG, "https://peer.example.com:8443/agent-card.json") is None


def test_headers_carry_the_credential_for_a_known_peer():
    peer = resolve_peer(CONFIG, "https://peer.example.com/x")
    assert auth_headers(peer) == {"Authorization": "Bearer sekrit"}


def test_headers_are_empty_for_an_unknown_peer():
    assert auth_headers(None) == {}


@pytest.mark.parametrize("url", ["not-a-url", "", "file:///etc/passwd"])
def test_unparseable_or_non_http_urls_resolve_to_none(url):
    assert resolve_peer(CONFIG, url) is None
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_peers.py -v
```

Expected: FAIL, `ModuleNotFoundError: No module named 'raven.a2a_client'`.

- [ ] **Step 3: Implement**

Create `raven/a2a_client/__init__.py`:

```python
"""Outbound A2A: the host agent calling a remote A2A agent.

Named for symmetry with ``raven.acp_client``. Nothing here is a sub-agent: a
peer is an external agent this host may reach, not a process it starts.
"""
```

Create `raven/a2a_client/peers.py`:

```python
"""Which remote A2A agents this host may call, and the credential for each.

The only place an outbound credential is attached. The model supplies a card
URL and never holds a secret, so an origin absent from the configured list is
called with no credential rather than with someone else's.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from raven.config.schema import A2aConfig, A2aPeerConfig

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def resolve_peer(config: A2aConfig, card_url: str) -> A2aPeerConfig | None:
    """The configured peer whose origin matches `card_url`, or None."""
    origin = _origin_of(card_url)
    if origin is None:
        return None
    for peer in config.peers:
        if _origin_of(peer.origin) == origin:
            return peer
    return None


def auth_headers(peer: A2aPeerConfig | None) -> dict[str, str]:
    """Request headers carrying `peer`'s credential; empty for an unlisted peer."""
    if peer is None or not peer.credential:
        return {}
    if peer.auth_scheme == "bearer":
        return {"Authorization": f"Bearer {peer.credential}"}
    return {peer.auth_scheme: peer.credential}
```

- [ ] **Step 4: Run the test again**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_peers.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add raven/a2a_client/ tests/test_a2a_peers.py
git commit -m "feat(a2a_client): resolve a peer credential by origin"
```

---

### Task 3: The outbound client and its tool

**Files:**
- Create: `raven/a2a_client/client.py`
- Create: `raven/a2a_client/tool.py`
- Test: `tests/test_a2a_client_tool.py`

**Interfaces:**
- Consumes: `resolve_peer`, `auth_headers` (Task 2); `A2aConfig` (Task 1); `Tool` from `raven/contracts/tool.py:205`.
- Produces: `A2aTool(config: A2aConfig)` whose `name` is `"a2a_send"`; `send_message(config, card_url, message, *, timeout_s) -> str`.

The `Tool` ABC requires four members: a `name` property, a `description` property, a `parameters()` method returning a JSON Schema dict, and `async execute(**kwargs) -> str | ToolResult`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_a2a_client_tool.py`:

```python
"""The outbound tool: its schema, and that it never surfaces a credential."""

import pytest

from raven.a2a_client.tool import A2aTool
from raven.config.schema import A2aConfig

CONFIG = A2aConfig.model_validate(
    {"peers": [{"origin": "https://peer.example.com", "authScheme": "bearer", "credential": "sekrit"}]}
)


def test_tool_name_is_stable():
    assert A2aTool(CONFIG).name == "a2a_send"


def test_parameters_ask_for_a_card_url_and_a_message():
    schema = A2aTool(CONFIG).parameters()
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"card_url", "message"}
    assert set(schema["properties"]) == {"card_url", "message"}


def test_no_credential_field_is_exposed_to_the_model():
    blob = repr(A2aTool(CONFIG).parameters()) + A2aTool(CONFIG).description
    assert "credential" not in blob
    assert "sekrit" not in blob


async def test_execute_returns_the_peer_reply(monkeypatch):
    seen = {}

    async def fake_send(config, card_url, message, *, timeout_s):
        seen["card_url"] = card_url
        seen["message"] = message
        return "peer answered"

    monkeypatch.setattr("raven.a2a_client.tool.send_message", fake_send)
    out = await A2aTool(CONFIG).execute(
        card_url="https://peer.example.com/.well-known/agent-card.json", message="hello"
    )
    assert out == "peer answered"
    assert seen["message"] == "hello"


async def test_a_transport_failure_becomes_a_model_readable_error(monkeypatch):
    async def boom(config, card_url, message, *, timeout_s):
        raise ConnectionError("refused")

    monkeypatch.setattr("raven.a2a_client.tool.send_message", boom)
    out = await A2aTool(CONFIG).execute(card_url="https://peer.example.com/c", message="hi")
    assert out.startswith("Error:")
    assert "refused" in out
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_client_tool.py -v
```

Expected: FAIL, `ModuleNotFoundError: No module named 'raven.a2a_client.tool'`.

- [ ] **Step 3: Implement the client**

Create `raven/a2a_client/client.py`:

```python
"""One A2A call: fetch the peer's card, send a message, return its text.

The SDK owns the wire format; this module owns which credential goes out and
how a reply becomes a string the model can read.
"""

from __future__ import annotations

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, SendMessageRequest

from raven.a2a_client.peers import auth_headers, resolve_peer
from raven.config.schema import A2aConfig

A2A_VERSION_HEADER = {"A2A-Version": "1.0"}


def _text_of(event: object) -> str:
    """Any text carried by one StreamResponse event.

    The oneof is task-or-message, so both arms are read: a peer may answer with a
    message directly, or with a task whose artifacts hold the answer.
    """
    chunks: list[str] = []
    message = getattr(event, "message", None)
    for part in getattr(message, "parts", None) or []:
        if getattr(part, "text", ""):
            chunks.append(part.text)
    task = getattr(event, "task", None)
    for artifact in getattr(task, "artifacts", None) or []:
        for part in getattr(artifact, "parts", None) or []:
            if getattr(part, "text", ""):
                chunks.append(part.text)
    return "\n".join(chunks)


async def send_message(config: A2aConfig, card_url: str, message: str, *, timeout_s: float = 300.0) -> str:
    """Send `message` to the A2A agent whose card is at `card_url`."""
    headers = {**A2A_VERSION_HEADER, **auth_headers(resolve_peer(config, card_url))}
    async with httpx.AsyncClient(headers=headers, timeout=timeout_s) as http:
        factory = ClientFactory(ClientConfig(httpx_client=http))
        client = factory.create_from_url(card_url)
        request = SendMessageRequest(
            message=Message(role=Role.ROLE_USER, parts=[Part(text=message)])
        )
        chunks = [text async for event in client.send_message(request) if (text := _text_of(event))]
    return "\n".join(chunks) or "(the peer returned no text)"
```

Four things here are not what the names suggest, all measured -- see **Measured API facts**: the factory takes a `ClientConfig` (not an `httpx_client`), `create` takes a card object so a URL needs `create_from_url`, `Message` carries `parts` (not `content`), and `send_message` takes a `SendMessageRequest` and returns an **async iterator**, so it is iterated rather than awaited for a single value.

- [ ] **Step 4: Implement the tool**

Create `raven/a2a_client/tool.py`:

```python
"""The single host-agent tool for calling a remote A2A agent.

Withheld from a sub-agent process by name -- see
``raven.agent.subagent.role.WITHHELD_FROM_SUBAGENT``. A credential never
appears in this tool's schema or description: the model passes a URL and
``peers`` decides what authenticates it.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from raven.a2a_client.client import send_message
from raven.config.schema import A2aConfig
from raven.contracts.tool import Tool


class A2aTool(Tool):
    timeout_seconds = 600.0

    def __init__(self, config: A2aConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "a2a_send"

    @property
    def description(self) -> str:
        return (
            "Send a task to an external agent that speaks the A2A protocol, and return its reply. "
            "Takes the URL of the agent's card (usually <origin>/.well-known/agent-card.json). "
            "Use it only for an agent the user has named or that is already configured as a trusted peer."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "card_url": {
                    "type": "string",
                    "description": "URL of the remote agent's A2A agent card.",
                },
                "message": {
                    "type": "string",
                    "description": "The task or question to send to that agent.",
                },
            },
            "required": ["card_url", "message"],
        }

    async def execute(self, **kwargs: Any) -> str:
        card_url = str(kwargs.get("card_url", "")).strip()
        message = str(kwargs.get("message", "")).strip()
        if not card_url or not message:
            return "Error: both card_url and message are required."
        try:
            return await send_message(self._config, card_url, message)
        except Exception as exc:
            logger.opt(exception=True).warning("a2a_send failed for {}", card_url)
            return f"Error: the A2A call failed: {exc}"
```

- [ ] **Step 5: Run the tests**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_client_tool.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add raven/a2a_client/client.py raven/a2a_client/tool.py tests/test_a2a_client_tool.py
git commit -m "feat(a2a_client): add the a2a_send tool over the sdk client"
```

---

### Task 4: The outbound gate

**Files:**
- Modify: `raven/agent/subagent/role.py` (the `WITHHELD_FROM_SUBAGENT` frozenset)
- Modify: `raven/agent/loop/wiring.py:867` (register inside the existing `if not is_subagent_process():` branch)
- Test: `tests/test_agent_loop_subagent_role.py` (modify -- do **not** create a new file)

**Interfaces:**
- Consumes: `A2aTool` (Task 3), `Config.a2a` (Task 1).
- Produces: nothing new; the tool is now registered on a host and absent in a sub-agent.

`WITHHELD_FROM_SUBAGENT` is read by the tests, not by the gates -- the gates work by not building the tool at all. So adding the name here without also registering the tool makes the existing test fail, which is the intended order.

- [ ] **Step 1: Add the name to the withheld set**

In `raven/agent/subagent/role.py`, inside `WITHHELD_FROM_SUBAGENT`:

```python
        "resolve_dag_node",
        "a2a_send",
```

Extend the frozenset's docstring paragraph with the reason:

```
#: ``a2a_send`` is here because it reaches another agent without passing through
#: ``spawn`` -- the same shape as ``load_playbook``. A sub-agent that hands its
#: task to an external A2A peer returns a receipt, and the caller cannot tell
#: that from an answer.
```

- [ ] **Step 2: Run the existing role test and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_agent_loop_subagent_role.py -v
```

Expected: FAIL. The test asserting every withheld name is registered on an ordinary host reports `a2a_send` missing -- exactly the direction that catches a name nothing registers.

- [ ] **Step 3: Register the tool on the host path**

There is **no** `self.config` on this class -- `WiringMixin` and `AgentLoop` hold no whole
`Config`. Per-feature config arrives as its own constructor argument and attribute, the way
`ask_user_config` does. Follow that pattern exactly.

In `raven/agent/loop/main.py`, beside the existing `self.ask_user_config = ask_user_config or AskUserToolConfig()` (line 320), add:

```python
        self.a2a_config = a2a_config or A2aConfig()
```

with `a2a_config: "A2aConfig | None" = None` added to `AgentLoop.__init__`'s keyword arguments, and `from raven.config.schema import A2aConfig` imported. Then find where `ask_user_config` is read off the incoming config near line 223 and pass `a2a_config` down the same way from whichever caller assembles the loop.

In `raven/agent/loop/wiring.py`, inside `_register_orchestration_tools` (line 983, the method called at line 867 under `if not is_subagent_process():`), add:

```python
        self.tools.register(A2aTool(self.a2a_config))
```

and the import at the top of the file:

```python
from raven.a2a_client.tool import A2aTool
```

- [ ] **Step 4: Run the role test again**

```bash
uv run --frozen --all-extras pytest tests/test_agent_loop_subagent_role.py -v
```

Expected: PASS. Both directions now cover `a2a_send` -- registered on a host, absent in a sub-agent process -- with no new assertions needed.

- [ ] **Step 5: Prove the sub-agent direction explicitly**

Append to `tests/test_agent_loop_subagent_role.py`:

```python
def test_a2a_send_is_withheld_from_a_subagent(monkeypatch):
    from raven.agent.subagent.role import WITHHELD_FROM_SUBAGENT, is_subagent_process

    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    assert is_subagent_process() is True
    assert "a2a_send" in WITHHELD_FROM_SUBAGENT
```

- [ ] **Step 6: Run it**

```bash
uv run --frozen --all-extras pytest tests/test_agent_loop_subagent_role.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add raven/agent/subagent/role.py raven/agent/loop/wiring.py tests/test_agent_loop_subagent_role.py
git commit -m "feat(agent): register a2a_send on the host and withhold it from sub-agents"
```

---

### Task 5: The Agent Card, and the import-linter contract

**Files:**
- Create: `raven/a2a/__init__.py`
- Create: `raven/a2a/card.py`
- Modify: `pyproject.toml` (import-linter contract at line 610)
- Test: `tests/test_a2a_card.py`

**Interfaces:**
- Consumes: `A2aConfig` (Task 1).
- Produces: `build_agent_card(config: A2aConfig, *, base_url: str) -> AgentCard` returning the SDK's protobuf `AgentCard`; the module constants `CARD_PATH`, `JSONRPC_BINDING` and `PROTOCOL_VERSION`, which Task 9 imports.

- [ ] **Step 1: Write the failing test**

Create `tests/test_a2a_card.py`:

```python
"""The agent card declares exactly what this build does, never optimistically."""

from raven.a2a.card import CARD_PATH, build_agent_card
from raven.config.schema import A2aConfig


def test_card_path_is_the_1_0_well_known():
    assert CARD_PATH == "/.well-known/agent-card.json"


def test_card_declares_one_jsonrpc_interface_at_1_0():
    card = build_agent_card(A2aConfig(), base_url="https://host.example.com/a2a")
    assert len(card.supported_interfaces) == 1
    iface = card.supported_interfaces[0]
    assert iface.url == "https://host.example.com/a2a"
    assert iface.protocol_version == "1.0"


def test_capabilities_match_what_is_implemented():
    caps = build_agent_card(A2aConfig(), base_url="https://h/a2a").capabilities
    assert caps.streaming is True
    assert caps.push_notifications is False


def test_card_advertises_at_least_one_skill():
    assert len(build_agent_card(A2aConfig(), base_url="https://h/a2a").skills) >= 1
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_card.py -v
```

Expected: FAIL, `ModuleNotFoundError: No module named 'raven.a2a'`.

- [ ] **Step 3: Implement**

Create `raven/a2a/__init__.py`:

```python
"""Inbound A2A: this host answering as the agent its card describes.

Named for symmetry with ``raven.acp``. A sub-agent process never serves this --
see ``gate.py``.
"""
```

Create `raven/a2a/card.py`:

```python
"""The A2A agent card this host publishes.

Every capability is answered by what this build actually does. An optimistic
card is worse than a narrow one: a caller that believes an advertised
capability fails at the call instead of choosing another path.
"""

from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from raven.config.schema import A2aConfig

CARD_PATH = "/.well-known/agent-card.json"
JSONRPC_BINDING = "JSONRPC"
PROTOCOL_VERSION = "1.0"


def build_agent_card(config: A2aConfig, *, base_url: str) -> AgentCard:
    """The card served at :data:`CARD_PATH` for a server mounted at `base_url`."""
    return AgentCard(
        name="Raven",
        description=(
            "A general-purpose assistant that can research, write, and run tasks on its host, "
            "and orchestrate its own sub-agents to do so."
        ),
        version="1.0",
        supported_interfaces=[
            AgentInterface(
                url=base_url,
                protocol_binding=JSONRPC_BINDING,
                protocol_version=PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extended_agent_card=False,
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="general",
                name="General assistance",
                description=(
                    "Answer a question, research a topic, write or edit a document, or carry out a "
                    "multi-step task and report what was done."
                ),
                tags=["general", "research", "writing"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ],
    )
```

Note for the implementer: these are protobuf messages, so unknown keyword arguments raise at construction. Confirm each field name against the descriptor before writing -- `uv run --frozen --all-extras python -c "from a2a.types import AgentCard; print([f.name for f in AgentCard().DESCRIPTOR.fields])"`.

- [ ] **Step 4: Add the import-linter contract**

In `pyproject.toml`, in the contract named `the served surfaces do not import the launcher` (line 610), add `raven.a2a` to `source_modules`:

```toml
source_modules = ["raven.rpc", "raven.acp", "raven.a2a"]
```

- [ ] **Step 5: Run the tests and the contract check**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_card.py -v
make lint-imports
```

Expected: 4 passed; import-linter reports the contract kept.

- [ ] **Step 6: Commit**

```bash
git add raven/a2a/ pyproject.toml tests/test_a2a_card.py
git commit -m "feat(a2a): publish the agent card and fence the module from the launcher"
```

---

### Task 6: Task lifecycle mapping

**Files:**
- Create: `raven/a2a/lifecycle.py`
- Test: `tests/test_a2a_lifecycle.py`

**Interfaces:**
- Consumes: `TaskState` from `a2a.types`.
- Produces: `TURN_TO_TASK_STATE: dict[str, int]` and `task_state_for(outcome: str) -> int`, where `outcome` is one of `"running"`, `"done"`, `"failed"`, `"cancelled"`, `"question"`.

`TaskState` is a protobuf enum, so its members are ints, not a Python `Enum`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_a2a_lifecycle.py`:

```python
"""A raven turn's outcome, in A2A task-state terms."""

import pytest
from a2a.types import TaskState

from raven.a2a.lifecycle import task_state_for


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("running", TaskState.TASK_STATE_WORKING),
        ("done", TaskState.TASK_STATE_COMPLETED),
        ("failed", TaskState.TASK_STATE_FAILED),
        ("cancelled", TaskState.TASK_STATE_CANCELED),
        ("question", TaskState.TASK_STATE_INPUT_REQUIRED),
    ],
)
def test_each_outcome_maps_to_its_state(outcome, expected):
    assert task_state_for(outcome) == expected


def test_a_question_is_not_a_terminal_state():
    terminal = {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
    assert task_state_for("question") not in terminal


def test_an_unknown_outcome_raises_rather_than_guessing():
    with pytest.raises(ValueError, match="unknown turn outcome"):
        task_state_for("sideways")
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_lifecycle.py -v
```

Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `raven/a2a/lifecycle.py`:

```python
"""One raven turn, in A2A task-state terms.

The load-bearing row is ``question``. A turn can stop and ask (``AskUserTool``),
and over ACP that goes out as ``session/request_permission`` on the caller's own
wire. A2A models it natively: the task parks in ``INPUT_REQUIRED`` and the caller
resumes it with another message against the same task id. So an inbound question
parks the task instead of holding a request thread open against a caller that was
never asked to answer one.
"""

from __future__ import annotations

from a2a.types import TaskState

TURN_TO_TASK_STATE: dict[str, int] = {
    "running": TaskState.TASK_STATE_WORKING,
    "done": TaskState.TASK_STATE_COMPLETED,
    "failed": TaskState.TASK_STATE_FAILED,
    "cancelled": TaskState.TASK_STATE_CANCELED,
    "question": TaskState.TASK_STATE_INPUT_REQUIRED,
}


def task_state_for(outcome: str) -> int:
    """The A2A task state for a raven turn `outcome`."""
    try:
        return TURN_TO_TASK_STATE[outcome]
    except KeyError:
        raise ValueError(f"unknown turn outcome: {outcome!r}") from None
```

- [ ] **Step 4: Run the tests**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_lifecycle.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add raven/a2a/lifecycle.py tests/test_a2a_lifecycle.py
git commit -m "feat(a2a): map turn outcomes onto a2a task states"
```

---

### Task 7: The AgentExecutor

**Files:**
- Create: `raven/a2a/executor.py`
- Test: `tests/test_a2a_executor.py`

**Interfaces:**
- Consumes: `task_state_for` (Task 6); `AgentExecutor`, `RequestContext`, `EventQueue` from `a2a.server.agent_execution` and `a2a.server.events`.
- Produces: `RavenAgentExecutor(run_turn: Callable[[str], Awaitable[str]])` implementing `execute` and `cancel`.

`run_turn` is injected rather than imported so this file has no dependency on how a turn is built, and so the tests need no runtime.

- [ ] **Step 1: Write the failing test**

Create `tests/test_a2a_executor.py`:

```python
"""The executor turns an A2A task into one raven turn, and leaks nothing when it raises."""

import pytest

from raven.a2a.executor import RavenAgentExecutor


class FakeQueue:
    """Stands in for EventQueue. `enqueue_event` is a coroutine on the real one."""

    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class FakeContext:
    """Stands in for RequestContext, which exposes get_user_input(), not an attribute."""

    def __init__(self, text="do the thing"):
        self._text = text
        self.task_id = "task-1"
        self.context_id = "ctx-1"

    def get_user_input(self, delimiter="\n"):
        return self._text


async def test_a_completed_turn_enqueues_its_answer():
    from a2a.types import TaskState

    async def run_turn(prompt):
        assert prompt == "do the thing"
        return "the answer"

    queue = FakeQueue()
    await RavenAgentExecutor(run_turn).execute(FakeContext(), queue)
    states = [e.status.state for e in queue.events]
    assert states == [TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_COMPLETED]
    assert queue.events[-1].status.message.parts[0].text == "the answer"


async def test_a_failed_turn_reports_the_failed_state():
    from a2a.types import TaskState

    async def run_turn(prompt):
        raise RuntimeError("boom")

    queue = FakeQueue()
    await RavenAgentExecutor(run_turn).execute(FakeContext(), queue)
    assert queue.events[-1].status.state == TaskState.TASK_STATE_FAILED


async def test_a_raising_turn_does_not_put_the_traceback_on_the_wire():
    async def run_turn(prompt):
        raise RuntimeError("/srv/secret/path.py exploded with API_KEY=abc123")

    queue = FakeQueue()
    await RavenAgentExecutor(run_turn).execute(FakeContext(), queue)
    wire = " ".join(str(e) for e in queue.events)
    assert "/srv/secret/path.py" not in wire
    assert "abc123" not in wire
    assert "Traceback" not in wire
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_executor.py -v
```

Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `raven/a2a/executor.py`:

```python
"""An A2A task becomes one raven turn.

The two methods the SDK asks for. Everything else about the protocol --
the task store, the event queue, the eleven request-handler methods -- is
``DefaultRequestHandler``'s.

The failure path is a trust boundary: a turn's exception can carry file paths,
prompt fragments and tool output, and the caller is in another trust domain, so
the wire gets a fixed sentence and this host's log gets the cause.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from a2a.server.agent_execution import AgentExecutor
from loguru import logger

from raven.a2a.lifecycle import task_state_for

TURN_FAILED_MESSAGE = "The agent turn failed. Ask the operator of this agent to check its logs."


class RavenAgentExecutor(AgentExecutor):
    def __init__(self, run_turn: Callable[[str], Awaitable[str]]) -> None:
        self._run_turn = run_turn

    async def execute(self, context, event_queue) -> None:
        prompt = context.get_user_input()
        await event_queue.enqueue_event(self._status(context, "running"))
        try:
            answer = await self._run_turn(prompt)
        except Exception:
            logger.opt(exception=True).error("a2a turn failed for task {}", getattr(context, "task_id", "?"))
            await event_queue.enqueue_event(self._status(context, "failed", TURN_FAILED_MESSAGE))
            return
        await event_queue.enqueue_event(self._status(context, "done", answer))

    async def cancel(self, context, event_queue) -> None:
        logger.info("a2a task {} cancelled by the caller", getattr(context, "task_id", "?"))
        await event_queue.enqueue_event(self._status(context, "cancelled"))

    def _status(self, context, outcome: str, text: str = "") -> TaskStatusUpdateEvent:
        """One task-status event carrying `outcome`'s A2A state and optional text."""
        status = TaskStatus(state=task_state_for(outcome))
        if text:
            status.message.CopyFrom(Message(role=Role.ROLE_AGENT, parts=[Part(text=text)]))
        return TaskStatusUpdateEvent(
            task_id=context.task_id,
            context_id=context.context_id,
            status=status,
        )
```

with these imports:

```python
from a2a.types import Message, Part, Role, TaskStatus, TaskStatusUpdateEvent
```

A real `TaskStatusUpdateEvent`, not a dict: `EventQueue.enqueue_event` accepts only
`Message | Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent`, so a dict would pass a
unit test and fail the moment a real queue saw it. Field names are measured --
`TaskStatus` is `state, message, timestamp` and `TaskStatusUpdateEvent` is
`task_id, context_id, status, metadata`.


- [ ] **Step 4: Run the tests**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_executor.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add raven/a2a/executor.py tests/test_a2a_executor.py
git commit -m "feat(a2a): run an inbound task as one raven turn"
```

---

### Task 8: Authenticating the caller

**Files:**
- Create: `raven/a2a/auth.py`
- Test: `tests/test_a2a_auth.py`

**Interfaces:**
- Consumes: `A2aServerConfig` (Task 1).
- Produces: `is_authorized(config: A2aServerConfig, header_value: str | None) -> bool`.

Constant-time comparison, because a bearer token compared with `==` leaks its prefix to a caller who can time the request.

- [ ] **Step 1: Write the failing test**

Create `tests/test_a2a_auth.py`:

```python
"""Only a caller holding the configured token reaches a turn."""

import pytest

from raven.a2a.auth import is_authorized
from raven.config.schema import A2aServerConfig

CONFIG = A2aServerConfig(enabled=True, token="t0ken")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer t0ken", True),
        ("bearer t0ken", True),
        ("Bearer wrong", False),
        ("t0ken", False),
        ("", False),
        (None, False),
    ],
)
def test_only_the_configured_bearer_token_is_accepted(header, expected):
    assert is_authorized(CONFIG, header) is expected


def test_an_empty_configured_token_refuses_everyone():
    open_cfg = A2aServerConfig(enabled=True, token="")
    assert is_authorized(open_cfg, "Bearer ") is False
    assert is_authorized(open_cfg, None) is False
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_auth.py -v
```

Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `raven/a2a/auth.py`:

```python
"""Who may call this host's A2A face.

One configured bearer token, declared on the card as ``http_auth``. Deliberately
not the ``/rpc`` cookie: that authenticates this user's browser to their own
gateway and is minted by a human clicking a nonce, while an A2A caller is a
program in another trust domain. One credential across both faces would make
either one's compromise the other's.

An empty configured token refuses everyone rather than admitting everyone --
a server switched on before its token is set must not be open.
"""

from __future__ import annotations

import hmac

from raven.config.schema import A2aServerConfig

_PREFIX = "bearer "


def is_authorized(config: A2aServerConfig, header_value: str | None) -> bool:
    """Whether an ``Authorization`` header value carries the configured token."""
    if not config.token:
        return False
    if not header_value or not header_value.lower().startswith(_PREFIX):
        return False
    presented = header_value[len(_PREFIX) :].strip()
    return hmac.compare_digest(presented, config.token)
```

- [ ] **Step 4: Run the tests**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_auth.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add raven/a2a/auth.py tests/test_a2a_auth.py
git commit -m "feat(a2a): authenticate inbound callers with a configured bearer token"
```

---

### Task 9: The aiohttp route layer and the error mapping

**Files:**
- Create: `raven/a2a/routes_aiohttp.py`
- Test: `tests/test_a2a_routes.py`

**Interfaces:**
- Consumes: `build_agent_card`, `CARD_PATH`, `PROTOCOL_VERSION` (Task 5); `is_authorized` (Task 8); `A2aConfig` (Task 1). Not the executor: these routes take an opaque `handler` and never construct one -- Task 11 supplies the real `DefaultRequestHandler`.
- Produces: `add_a2a_routes(app: web.Application, config: A2aConfig, handler) -> None`, plus `error_response(code: str, request_id) -> dict`.

This is the file the spec says is written to be deleted: if the gateway ever moves to ASGI, it is replaced by the SDK's `add_a2a_routes_to_fastapi()`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_a2a_routes.py`:

```python
"""The JSON-RPC binding: version gating, auth ordering, and error shape."""

from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from raven.a2a.card import CARD_PATH
from raven.a2a.routes_aiohttp import add_a2a_routes
from raven.config.schema import A2aConfig

CONFIG = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken", "path": "/a2a"}})
AUTH = {"Authorization": "Bearer t0ken", "A2A-Version": "1.0"}


class RecordingHandler:
    def __init__(self):
        self.calls = []

    async def on_message_send(self, params, context):
        self.calls.append(params)
        return {"ok": True}


@pytest.fixture
async def client_and_handler() -> AsyncIterator[tuple[TestClient, RecordingHandler]]:
    """`pytest-aiohttp` is not installed here; aiohttp ships these test utils itself.
    Same shape as the fixture in tests/test_rpc_files.py."""
    handler = RecordingHandler()
    app = web.Application()
    add_a2a_routes(app, CONFIG, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, handler
    finally:
        await client.close()


async def test_the_card_is_served_unauthenticated(client_and_handler):
    client, _ = client_and_handler
    resp = await client.get(CARD_PATH)
    assert resp.status == 200
    assert "supportedInterfaces" in await resp.text()


async def test_a_missing_version_header_is_refused(client_and_handler):
    client, handler = client_and_handler
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}},
        headers={"Authorization": "Bearer t0ken"},
    )
    body = await resp.json()
    assert body["error"]["message"] == "VersionNotSupportedError"
    assert handler.calls == []


async def test_an_unauthenticated_call_never_reaches_the_handler(client_and_handler):
    client, handler = client_and_handler
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}},
        headers={"A2A-Version": "1.0"},
    )
    assert resp.status == 401
    assert handler.calls == []


async def test_an_authenticated_1_0_call_reaches_the_handler(client_and_handler):
    client, handler = client_and_handler
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": "SendMessage", "params": {"x": 1}},
        headers=AUTH,
    )
    assert resp.status == 200
    assert (await resp.json())["id"] == 7
    assert handler.calls == [{"x": 1}]


async def test_an_unknown_method_is_a_json_rpc_error(client_and_handler):
    client, _ = client_and_handler
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 2, "method": "Nope", "params": {}},
        headers=AUTH,
    )
    assert (await resp.json())["error"]["message"] == "MethodNotFoundError"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_routes.py -v
```

Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `raven/a2a/routes_aiohttp.py`:

```python
"""The A2A JSON-RPC binding, on aiohttp.

Written to be deleted. The SDK ships ``add_a2a_routes_to_fastapi()``,
``create_jsonrpc_routes()`` and ``create_agent_card_routes()`` for an ASGI host;
this file exists only because the gateway is aiohttp. If that ever changes, drop
this module and call those -- no A2A logic moves with it.

Two orderings are load-bearing: the version header is checked before the method
is dispatched, and authentication is checked before anything reaches the
request handler, so an unknown caller never starts a turn.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web
from google.protobuf.json_format import MessageToDict

from raven.a2a.auth import is_authorized
from raven.a2a.card import CARD_PATH, PROTOCOL_VERSION, build_agent_card
from raven.config.schema import A2aConfig

VERSION_HEADER = "A2A-Version"

#: The JSON-RPC codes this binding emits, by A2A error name. Fixed here so two
#: call sites cannot disagree about what a caller sees.
ERROR_CODES: dict[str, int] = {
    "VersionNotSupportedError": -32001,
    "MethodNotFoundError": -32601,
    "InvalidRequestError": -32600,
    "InvalidParamsError": -32602,
    "TaskNotFoundError": -32002,
    "TaskNotCancelableError": -32005,
    "PushNotificationNotSupportedError": -32003,
    "ContentTypeNotSupportedError": -32004,
    "InternalError": -32603,
}

#: JSON-RPC method -> the ``RequestHandler`` coroutine that serves it.
METHODS: dict[str, str] = {
    "SendMessage": "on_message_send",
    "SendStreamingMessage": "on_message_send_stream",
    "GetTask": "on_get_task",
    "ListTasks": "on_list_tasks",
    "CancelTask": "on_cancel_task",
    "SubscribeToTask": "on_subscribe_to_task",
}


def error_response(name: str, request_id: Any) -> dict[str, Any]:
    """A JSON-RPC error body naming an A2A error type."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": ERROR_CODES.get(name, ERROR_CODES["InternalError"]), "message": name},
    }


def add_a2a_routes(app: web.Application, config: A2aConfig, handler: Any) -> None:
    """Mount the card and the JSON-RPC endpoint onto `app`."""

    async def serve_card(request: web.Request) -> web.Response:
        base = str(request.url.origin().join(web.URL(config.server.path)))
        return web.json_response(MessageToDict(build_agent_card(config, base_url=base)))

    async def serve_rpc(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(error_response("InvalidRequestError", None), status=400)
        request_id = body.get("id")

        if request.headers.get(VERSION_HEADER, "").strip() != PROTOCOL_VERSION:
            return web.json_response(error_response("VersionNotSupportedError", request_id), status=400)

        if not is_authorized(config.server, request.headers.get("Authorization")):
            return web.json_response(error_response("InvalidRequestError", request_id), status=401)

        method_name = METHODS.get(body.get("method", ""))
        if method_name is None:
            return web.json_response(error_response("MethodNotFoundError", request_id), status=200)

        try:
            result = await getattr(handler, method_name)(body.get("params") or {}, None)
        except Exception:
            return web.json_response(error_response("InternalError", request_id), status=200)
        return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": result})

    app.router.add_get(CARD_PATH, serve_card)
    app.router.add_post(config.server.path, serve_rpc)
```

Note for the implementer: `MessageToDict` renders protobuf `snake_case` as `camelCase` by default, which is what the JSON binding wants -- that is why the card test greps for `supportedInterfaces`. `SendStreamingMessage` and `SubscribeToTask` return async generators and need an SSE response (`text/event-stream`) rather than `json_response`; wire the two non-streaming paths first, get this test green, then add the SSE branch with a test that reads two events off one response.

- [ ] **Step 4: Run the tests**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_routes.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add raven/a2a/routes_aiohttp.py tests/test_a2a_routes.py
git commit -m "feat(a2a): serve the json-rpc binding and the card over aiohttp"
```

---

### Task 10: The inbound gate and the two hostings

**Files:**
- Create: `raven/a2a/gate.py`
- Create: `raven/cli/a2a_commands.py`
- Modify: `raven/rpc/transports/ws.py:424` (`build_app`)
- Modify: `raven/cli/commands.py:157` (register the typer app)
- Test: `tests/test_cli_a2a_commands.py`

**Interfaces:**
- Consumes: `add_a2a_routes` (Task 9); `is_subagent_process` from `raven/agent/subagent/role.py`.
- Produces: `refuse_if_subagent() -> str | None` returning a refusal reason or None; `mount_if_allowed(app, config, *, handler) -> bool`, which Task 11 calls; `a2a_app` typer application.

Naming follows AGENTS.md 5.1: a CLI module's tests live in `tests/test_cli_<module>_commands.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_a2a_commands.py`:

```python
"""The inbound face refuses to start in a sub-agent process, on both hostings."""

import pytest
from aiohttp import web
from typer.testing import CliRunner

from raven.a2a.gate import refuse_if_subagent
from raven.cli.a2a_commands import a2a_app
from raven.config.schema import A2aConfig

runner = CliRunner()


def test_the_gate_is_open_on_a_host(monkeypatch):
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    assert refuse_if_subagent() is None


def test_the_gate_closes_in_a_subagent_process(monkeypatch):
    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    reason = refuse_if_subagent()
    assert reason is not None
    assert "sub-agent" in reason


def test_serve_exits_nonzero_in_a_subagent_process(monkeypatch):
    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    result = runner.invoke(a2a_app, ["serve"])
    assert result.exit_code != 0
    assert "sub-agent" in result.output


def test_the_gateway_mount_is_skipped_in_a_subagent_process(monkeypatch):
    from raven.a2a.gate import mount_if_allowed

    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    app = web.Application()
    cfg = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken"}})
    assert mount_if_allowed(app, cfg, handler=object()) is False
    assert [r.resource.canonical for r in app.router.routes()] == []


def test_the_mount_is_skipped_when_disabled(monkeypatch):
    from raven.a2a.gate import mount_if_allowed

    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    app = web.Application()
    assert mount_if_allowed(app, A2aConfig(), handler=object()) is False
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_cli_a2a_commands.py -v
```

Expected: FAIL, `ModuleNotFoundError: No module named 'raven.a2a.gate'`.

- [ ] **Step 3: Implement the gate**

Create `raven/a2a/gate.py`:

```python
"""Whether this process may serve A2A at all.

The second of the two gates the boundary needs. The outbound half rides the tool
registry -- ``a2a_send`` is simply not built in a sub-agent -- but serving a port
does not pass through the registry, so this is its own check.

The signal is ``RAVEN_SUBAGENT``, which the host merges into every ``kind: acp``
child it launches. All five products under ``agents/`` are ``kind: acp`` and none
overrides it, so the seam covers them and covers a future product for free.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web
from loguru import logger

from raven.agent.subagent.role import is_subagent_process
from raven.config.schema import A2aConfig

REFUSAL = (
    "this raven was launched as a sub-agent, and a sub-agent does not serve A2A: "
    "it is reached over ACP by the host that started it"
)


def refuse_if_subagent() -> str | None:
    """The reason this process may not serve A2A, or None if it may."""
    return REFUSAL if is_subagent_process() else None


def mount_if_allowed(app: web.Application, config: A2aConfig, *, handler: Any) -> bool:
    """Mount the A2A routes onto `app` when enabled and permitted. Returns whether it did."""
    if not config.server.enabled:
        return False
    reason = refuse_if_subagent()
    if reason is not None:
        logger.info("not mounting the A2A face: {}", reason)
        return False
    from raven.a2a.routes_aiohttp import add_a2a_routes

    add_a2a_routes(app, config, handler)
    return True
```

- [ ] **Step 4: Implement the CLI hosting**

Create `raven/cli/a2a_commands.py`:

```python
"""`raven a2a serve`: the A2A face without a gateway.

The headless hosting. The gateway-mounted one is ``gate.mount_if_allowed``,
called from the app builder; both refuse in a sub-agent process through the same
check, so neither can be the one that forgot.
"""

from __future__ import annotations

import typer

from raven.a2a.gate import refuse_if_subagent

a2a_app = typer.Typer(name="a2a", help="Serve the A2A protocol face.", subcommand_metavar="")


@a2a_app.command("serve")
def serve(
    port: int = typer.Option(8710, help="Port to bind."),
    host: str = typer.Option("127.0.0.1", help="Address to bind."),
) -> None:
    """Run the A2A server standalone."""
    reason = refuse_if_subagent()
    if reason is not None:
        typer.echo(f"Refusing to start: {reason}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Serving A2A on http://{host}:{port}")
```

The body that actually builds the runtime, the `DefaultRequestHandler` and the aiohttp site is the next increment; the gate and its refusal are what this task delivers and what the test pins.

- [ ] **Step 5: Wire both hostings in**

In `raven/cli/commands.py`, beside the other imports at line 144:

```python
from raven.cli.a2a_commands import a2a_app
```

and beside the other registrations at line 157:

```python
app.add_typer(a2a_app, name="a2a")
```

In `raven/rpc/transports/ws.py`, inside `build_app` after the existing `app.router.add_get("/oauth/callback", handle_oauth_callback)`:

```python
    from raven.a2a.gate import mount_if_allowed
    from raven.config import load_config

    mount_if_allowed(app, load_config().a2a, handler=gateway.a2a_handler)
```

`WsGateway.__init__(self)` takes no arguments and holds no `Config`, which is why the config is loaded here rather than read off the gateway. Add an `a2a_handler` attribute defaulting to `None` beside the existing `self.agent_loop_factory: Any = None` (line 112), set the same way. A `None` handler with `server.enabled` false never mounts, which is the default.

- [ ] **Step 6: Run the tests**

```bash
uv run --frozen --all-extras pytest tests/test_cli_a2a_commands.py -v
```

Expected: 5 passed.

- [ ] **Step 7: Run the whole A2A set plus the role test**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_*.py tests/test_cli_a2a_commands.py tests/test_agent_loop_subagent_role.py -v
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add raven/a2a/gate.py raven/cli/a2a_commands.py raven/cli/commands.py raven/rpc/transports/ws.py tests/test_cli_a2a_commands.py
git commit -m "feat(a2a): refuse to serve in a sub-agent and mount both hostings"
```

---

### Task 11: Assemble the inbound runtime, end to end

**Files:**
- Create: `raven/a2a/runtime.py`
- Modify: `raven/cli/a2a_commands.py` (fill the `serve` body)
- Modify: `raven/rpc/transports/ws.py` (pass a real handler instead of `None`)
- Test: `tests/integration/test_a2a_protocol_e2e.py`

**Interfaces:**
- Consumes: `RavenAgentExecutor` (Task 7), `add_a2a_routes` (Task 9), `mount_if_allowed` (Task 10), `build_agent_card` (Task 5).
- Produces: `build_request_handler(config, run_turn, *, base_url='') -> DefaultRequestHandler` and `serve_standalone(config, *, host, port, run_turn) -> None`.

Tasks 5 through 10 each deliver a piece; until this one runs, nothing constructs a `DefaultRequestHandler` and no external client can complete a call. This is the task that makes the inbound face real, and it carries the spec's conformance test.

Integration-test naming follows AGENTS.md 5.2: `tests/integration/test_<scope>_<kind>.py`, kind `e2e`.

- [ ] **Step 1: Write the failing end-to-end test**

Create `tests/integration/test_a2a_protocol_e2e.py`:

```python
"""A real A2A client against a real raven A2A server, over a real socket.

The SDK's own client is the closest thing to a second implementation available,
so conformance is asserted against it rather than against our own encoder.
"""

from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from raven.a2a.card import CARD_PATH
from raven.a2a.routes_aiohttp import add_a2a_routes
from raven.a2a.runtime import build_request_handler
from raven.config.schema import A2aConfig

CONFIG = A2aConfig.model_validate({"server": {"enabled": True, "token": "t0ken", "path": "/a2a"}})


@pytest.fixture
async def server() -> AsyncIterator[TestServer]:
    """`pytest-aiohttp` is absent; aiohttp's own TestServer is what this repo uses."""

    async def run_turn(prompt):
        return f"echo: {prompt}"

    app = web.Application()
    add_a2a_routes(app, CONFIG, build_request_handler(CONFIG, run_turn))
    srv = TestServer(app)
    await srv.start_server()
    try:
        yield srv
    finally:
        await srv.close()


async def test_the_card_round_trips_through_a_plain_fetch(server):
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.get(str(server.make_url(CARD_PATH)))
    assert resp.status_code == 200
    card = resp.json()
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["capabilities"]["streaming"] is True


async def test_send_message_returns_the_turn_answer(server):
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            str(server.make_url("/a2a")),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {"message": {"role": "ROLE_USER", "content": [{"text": "hello"}]}},
            },
            headers={"Authorization": "Bearer t0ken", "A2A-Version": "1.0"},
        )
    body = resp.json()
    assert "error" not in body
    assert "echo: hello" in str(body["result"])


async def test_a_header_less_request_is_refused_over_the_wire(server):
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.post(
            str(server.make_url("/a2a")),
            json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}},
            headers={"Authorization": "Bearer t0ken"},
        )
    assert resp.json()["error"]["message"] == "VersionNotSupportedError"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/integration/test_a2a_protocol_e2e.py -v
```

Expected: FAIL, `ModuleNotFoundError: No module named 'raven.a2a.runtime'`.

- [ ] **Step 3: Implement the runtime assembly**

Create `raven/a2a/runtime.py`:

```python
"""Assembling the inbound face: executor, request handler, and a standalone site.

The SDK's ``DefaultRequestHandler`` implements all eleven protocol methods over a
task store and an event queue; the only thing it lacks is what a turn is, which
is ``RavenAgentExecutor``. This module is where those two meet, so no other file
has to know how the SDK is put together.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from aiohttp import web
from loguru import logger

from raven.a2a.card import build_agent_card
from raven.a2a.executor import RavenAgentExecutor
from raven.a2a.routes_aiohttp import add_a2a_routes
from raven.config.schema import A2aConfig


def build_request_handler(
    config: A2aConfig,
    run_turn: Callable[[str], Awaitable[str]],
    *,
    base_url: str = "",
) -> DefaultRequestHandler:
    """A request handler serving `run_turn` as this agent's behaviour.

    `agent_card` is required by the SDK, not optional -- a two-argument call
    raises TypeError -- so the config has to reach here to build one.
    """
    return DefaultRequestHandler(
        agent_executor=RavenAgentExecutor(run_turn),
        task_store=InMemoryTaskStore(),
        agent_card=build_agent_card(config, base_url=base_url or config.server.path),
    )


async def serve_standalone(
    config: A2aConfig,
    *,
    host: str,
    port: int,
    run_turn: Callable[[str], Awaitable[str]],
) -> None:
    """Run the A2A face on its own aiohttp site until cancelled."""
    app = web.Application()
    add_a2a_routes(app, config, build_request_handler(config, run_turn))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("A2A serving on http://{}:{}{}", host, port, config.server.path)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
```

Add `import asyncio` at the top. Confirm `DefaultRequestHandler`'s real constructor keywords and `InMemoryTaskStore`'s import path before writing -- run `uv run --frozen --all-extras python -c "import inspect; from a2a.server.request_handlers import DefaultRequestHandler; print(inspect.signature(DefaultRequestHandler.__init__))"`.

- [ ] **Step 4: Fill the CLI body**

In `raven/cli/a2a_commands.py`, replace the placeholder echo in `serve` with the real run:

```python
    import asyncio

    from raven.a2a.runtime import serve_standalone
    from raven.config.loader import load_config

    cfg = load_config()

    async def run_turn(prompt: str) -> str:
        raise NotImplementedError("wire the host runtime's turn entry point here")

    typer.echo(f"Serving A2A on http://{host}:{port}")
    asyncio.run(serve_standalone(cfg.a2a, host=host, port=port, run_turn=run_turn))
```

Replace `run_turn` with the host's real turn entry point: find how `raven agent -m` runs one turn (`raven/cli/agent_commands.py`) and reuse that assembly, so the A2A face and the CLI face run the same code. Do not leave the `NotImplementedError` in the committed version -- the gate test from Task 10 still passes with it, which is exactly why it would survive review unnoticed.

- [ ] **Step 5: Pass a real handler from the gateway**

In `raven/rpc/transports/ws.py`, the `mount_if_allowed` call from Task 10 currently passes `gateway.a2a_handler`. Set that attribute in `build_app` from the same turn entry point the gateway already uses for `/rpc`:

```python
    from raven.config import load_config

    gateway.a2a_handler = build_request_handler(load_config().a2a, gateway.run_turn)
```

Use whatever coroutine the gateway already exposes for running one turn; if there is none with that exact name, read `WsGateway.handle_ws` to find how it drives a turn and reuse that path rather than building a second one.

- [ ] **Step 6: Run the end-to-end test**

```bash
uv run --frozen --all-extras pytest tests/integration/test_a2a_protocol_e2e.py -v
```

Expected: 3 passed.

- [ ] **Step 7: Add the streaming case**

Append to the same file:

```python
async def test_streaming_delivers_more_than_one_event(server):
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as http:
        async with http.stream(
            "POST",
            str(server.make_url("/a2a")),
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "SendStreamingMessage",
                "params": {"message": {"role": "ROLE_USER", "content": [{"text": "hello"}]}},
            },
            headers={"Authorization": "Bearer t0ken", "A2A-Version": "1.0"},
        ) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            lines = [line async for line in resp.aiter_lines() if line.startswith("data:")]
    assert len(lines) >= 2
```

- [ ] **Step 8: Implement the SSE branch**

In `raven/a2a/routes_aiohttp.py`, split the dispatch: the two streaming methods return async generators, so they need a `web.StreamResponse` with `content_type="text/event-stream"`, each yielded event written as `data: <json>\n\n`. The non-streaming path stays as it is.

```python
STREAMING_METHODS = frozenset({"SendStreamingMessage", "SubscribeToTask"})
```

Branch on that set before calling the handler, and write each event with `await response.write(f"data: {json.dumps(payload)}\n\n".encode())`.

- [ ] **Step 9: Run the whole file**

```bash
uv run --frozen --all-extras pytest tests/integration/test_a2a_protocol_e2e.py -v
```

Expected: 4 passed.

- [ ] **Step 10: Commit**

```bash
git add raven/a2a/runtime.py raven/a2a/routes_aiohttp.py raven/cli/a2a_commands.py raven/rpc/transports/ws.py tests/integration/test_a2a_protocol_e2e.py
git commit -m "feat(a2a): assemble the inbound runtime and stream over sse"
```

---

### Task 12: A question parks the task in INPUT_REQUIRED

**Files:**
- Create: `raven/a2a/asking.py`
- Modify: `raven/a2a/executor.py` (run the turn as a background task, park on a question)
- Test: `tests/test_a2a_asking.py`

**Interfaces:**
- Consumes: `QuestionResponder` protocol from `raven/contracts/asking.py:58`; `task_state_for` (Task 6).
- Produces: `A2aQuestionBroker(on_park: Callable[[str], None])` implementing `await_question(...)`, plus `answer(task_id: str, text: str) -> bool`.

**Read this before starting.** A raven turn does **not** suspend when it asks. `AskUserTool` sets `blocking_interaction = True` and awaits `broker.await_question(...)`, so the turn's coroutine stays alive holding a future. A2A's `INPUT_REQUIRED` is therefore served by keeping that coroutine running in the background while the HTTP request returns, and resolving its future when a later `SendMessage` arrives against the same task id. Nothing is suspended or replayed -- a waiting turn is answered.

`QuestionResponder` is a structural Protocol that each transport implements with its own broker (the TUI has one, the gateway has one). A2A gets a third. Nothing needs to change in `AskUserTool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_a2a_asking.py`:

```python
"""A turn that asks parks its task; a later message answers the waiting turn."""

import asyncio

import pytest
from a2a.types import TaskState

from raven.a2a.asking import A2aQuestionBroker


async def test_await_question_parks_and_then_returns_the_answer():
    parked = []
    broker = A2aQuestionBroker(on_park=parked.append)

    waiting = asyncio.create_task(
        broker.await_question("task-1", prompt="which one?", timeout_s=5.0)
    )
    await asyncio.sleep(0)
    assert parked == ["task-1"]
    assert not waiting.done()

    assert broker.answer("task-1", "the second one") is True
    assert await waiting == "the second one"


async def test_answering_an_unknown_task_reports_that_it_did_nothing():
    assert A2aQuestionBroker(on_park=lambda _: None).answer("nope", "hi") is False


async def test_a_timed_out_question_returns_the_default_and_unparks():
    broker = A2aQuestionBroker(on_park=lambda _: None)
    out = await broker.await_question("task-2", prompt="?", default="fallback", timeout_s=0.01)
    assert out == "fallback"
    assert broker.answer("task-2", "too late") is False


def test_parked_is_the_input_required_state():
    from raven.a2a.lifecycle import task_state_for

    assert task_state_for("question") == TaskState.TASK_STATE_INPUT_REQUIRED
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_asking.py -v
```

Expected: FAIL, `ModuleNotFoundError: No module named 'raven.a2a.asking'`.

- [ ] **Step 3: Implement the broker**

Create `raven/a2a/asking.py`:

```python
"""The A2A transport's question broker.

``QuestionResponder`` is structural, and each transport brings its own: the TUI
has one, the gateway has one, this is A2A's. Nothing in ``AskUserTool`` changes.

The turn is never suspended. It awaits a future here while its task is reported
as ``INPUT_REQUIRED``, and a later ``SendMessage`` against the same task id
resolves that future -- so what looks like resuming a task is answering a turn
that never stopped running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from loguru import logger


class A2aQuestionBroker:
    """Puts a turn's question to an A2A caller by parking the task."""

    def __init__(self, on_park: Callable[[str], None]) -> None:
        self._on_park = on_park
        self._waiting: dict[str, asyncio.Future[str]] = {}

    async def await_question(
        self,
        conversation_id: str,
        *,
        prompt: str = "",
        default: str = "",
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> str:
        """Park `conversation_id`'s task and wait for the caller's next message."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._waiting[conversation_id] = future
        self._on_park(conversation_id)
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except (TimeoutError, asyncio.TimeoutError):
            logger.info("a2a question on task {} timed out; using the default", conversation_id)
            return default
        finally:
            self._waiting.pop(conversation_id, None)

    def answer(self, task_id: str, text: str) -> bool:
        """Resolve the question a turn is waiting on. False if nothing was waiting."""
        future = self._waiting.get(task_id)
        if future is None or future.done():
            return False
        future.set_result(text)
        return True
```

The `**kwargs` absorbs the rest of `QuestionResponder.await_question`'s keywords (`choices`, `header`, `recommended`, `index`, `total`, `batch`) -- read `raven/contracts/asking.py:68` and name the ones this transport can actually render rather than leaving them all anonymous.

- [ ] **Step 4: Park the task from the executor**

In `raven/a2a/executor.py`, run the turn as a background task so the request can return while it waits, and emit the parked state when the broker parks:

```python
    async def execute(self, context, event_queue) -> None:
        task_id = getattr(context, "task_id", "")
        await event_queue.enqueue_event(self._status(context, "running"))

        def on_park(_task_id: str) -> None:
            asyncio.create_task(event_queue.enqueue_event(self._status(context, "question")))

        self._broker = A2aQuestionBroker(on_park=on_park)
        ...
```

Two things to settle against the SDK before writing this: whether `EventQueue` has a non-async enqueue (if not, schedule the coroutine with `asyncio.create_task` from `on_park`), and where the executor should hold the broker so a later `SendMessage` for the same task id can reach it -- a module-level `dict[str, A2aQuestionBroker]` keyed by task id, cleared when the turn ends, is the smallest thing that works.

- [ ] **Step 5: Route a second message to a waiting turn**

In `raven/a2a/routes_aiohttp.py`, before dispatching `SendMessage` normally, check whether its params name a task id with a waiting broker; if so, call `answer(task_id, text)` and return the task's current status instead of starting a second turn.

- [ ] **Step 6: Run the tests**

```bash
uv run --frozen --all-extras pytest tests/test_a2a_asking.py tests/test_a2a_executor.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add raven/a2a/asking.py raven/a2a/executor.py raven/a2a/routes_aiohttp.py tests/test_a2a_asking.py
git commit -m "feat(a2a): park a task on a question and answer the waiting turn"
```

---

## Final verification

- [ ] **Step 1: Full suite**

```bash
make test-python
```

Two failures pre-exist on `main` (a `doctor` test that is width-plus-xdist sensitive, and one other). Confirm any failure is pre-existing by running the same command at the merge base before blaming it on this branch.

- [ ] **Step 2: All gates**

```bash
make lint check-commits check-large-files check-source-language
```

Expected: all pass. `make check-commits` rebuilds the venv, so take any timing baseline before it, not after.

- [ ] **Step 3: Confirm the dependency landed as measured**

```bash
uv run --frozen --all-extras python -c "
import a2a, importlib.metadata as md
print('a2a-sdk', md.version('a2a-sdk'))
for p in ('protobuf','google-api-core','googleapis-common-protos','json-rpc','culsans'):
    print(' ', p, md.version(p))
"
```

- [ ] **Step 4: Pre-submit sweep**

Run the `pr-review-patterns` pre-submit sweep over `git diff origin/main...HEAD` before pushing. It is a standing requirement in this repo, not an optional check.
