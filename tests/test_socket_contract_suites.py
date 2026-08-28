"""Socket conformance suites — the S2 pattern, prototyped on the two hardest
sockets (provider, executor) with built-in bite-tests.

Ruling basis (2026-08-26): replacement clears three gates = the paper
(shape) + the socket conformance test (behavior) + the baseline diff.
This file is gate two's exemplar: a reusable checker per socket that ANY answer
(factory, shelf, third-party) must pass before it may occupy the socket. The
member lists are the MEASURED surfaces (campaign A plus the three
fields every seven-swap run died on), not
designed ones. Written against today's de-facto shapes; S2 re-points the
imports at contracts/ without changing the assertions.
"""

import inspect

import pytest

PROVIDER_METHODS = ("chat", "chat_with_retry", "chat_stream")
RESPONSE_MEMBERS = ("content", "tool_calls", "reasoning_content", "thinking_blocks",
                    "usage", "finish_reason", "error_classification")


def check_provider(provider) -> list[str]:
    """Shape half of the provider socket's entry ticket."""
    problems = []
    for m in PROVIDER_METHODS:
        fn = getattr(provider, m, None)
        if fn is None:
            problems.append(f"missing method {m}")
        elif not inspect.iscoroutinefunction(fn):
            problems.append(f"{m} must be async")
    return problems


def check_provider_response(resp) -> list[str]:
    problems = [f"response missing {m}" for m in RESPONSE_MEMBERS if not hasattr(resp, m)]
    _MISSING = object()
    val = getattr(resp, "has_tool_calls", _MISSING)
    if val is _MISSING:
        problems.append("response missing has_tool_calls")
    elif callable(val):
        # The loop reads this as an attribute (main.py:3214). A method-shaped
        # stand-in yields a bound method, which is always truthy -- a latent
        # bug, so it is refused here.
        problems.append("has_tool_calls must be a property/attribute, not a method")
    return problems


def check_executor(executor) -> list[str]:
    """Entry ticket for the sandbox socket (the conformance-test half of the
    hardened admission)."""
    problems = []
    if not isinstance(getattr(type(executor), "is_sandboxed", None), property) \
            and not hasattr(executor, "is_sandboxed"):
        problems.append("missing is_sandboxed")
    if not hasattr(executor, "supports_process_spawning"):
        problems.append("missing supports_process_spawning")
    for dunder in ("__aenter__", "__aexit__"):
        if not hasattr(type(executor), dunder):
            problems.append(f"{dunder} must live on the type (async CM protocol)")
    return problems


# -- compliant doubles: same shapes the seven-swap runs used; live samples --


class _Resp:
    content = "ok"
    tool_calls: list = []
    reasoning_content = None
    thinking_blocks = None
    usage: dict = {}
    finish_reason = "stop"
    error_classification = None

    has_tool_calls = False


class _GoodProvider:
    async def chat(self, messages, **kw):
        return _Resp()

    async def chat_with_retry(self, messages, **kw):
        return await self.chat(messages, **kw)

    async def chat_stream(self, messages, **kw):
        return _Resp()


class _GoodExecutor:
    is_sandboxed = False
    supports_process_spawning = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_compliant_provider_passes():
    assert check_provider(_GoodProvider()) == []


def test_compliant_response_passes():
    assert check_provider_response(_Resp()) == []


def test_compliant_executor_passes():
    assert check_executor(_GoodExecutor()) == []


def test_factory_response_shape_is_compliant():
    from raven.providers.base import LLMResponse

    resp = LLMResponse(content="x")
    assert check_provider_response(resp) == []


# -- bite tests: built-in mutation audit; the three lethal fields and the --
# -- async context manager must each draw blood                          --


def test_sync_chat_is_rejected():
    class Bad(_GoodProvider):
        def chat(self, messages, **kw):  # not async
            return _Resp()

    assert any("must be async" in p for p in check_provider(Bad()))


def test_method_form_has_tool_calls_is_rejected():
    class Sneaky(_Resp):
        def has_tool_calls(self):  # method-shaped: bound method is always truthy
            return False

    assert any("not a method" in p for p in check_provider_response(Sneaky()))


def test_missing_retry_method_is_rejected():
    class Bad:
        async def chat(self, messages, **kw):
            return _Resp()

        async def chat_stream(self, messages, **kw):
            return _Resp()

    assert any("chat_with_retry" in p for p in check_provider(Bad()))


@pytest.mark.parametrize("missing", ["usage", "thinking_blocks", "error_classification"])
def test_response_missing_measured_member_is_rejected(missing):
    resp = _Resp()

    class Stripped:
        pass

    for m in RESPONSE_MEMBERS:
        if m != missing:
            setattr(Stripped, m, getattr(resp, m))
    Stripped.has_tool_calls = False
    assert any(missing in p for p in check_provider_response(Stripped()))


def test_executor_without_type_level_aenter_is_rejected():
    class Bad:
        is_sandboxed = False
        supports_process_spawning = True

        def __getattr__(self, name):  # instance-level fake, must NOT count
            async def _f(*a):
                return self
            return _f

    assert any("__aenter__" in p for p in check_executor(Bad()))
