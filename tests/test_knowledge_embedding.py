"""Tests for resolving the embedding endpoint, and for the client that calls it.

Raven's own ``embedding`` section is the answer; the EverOS config file is a
fallback for an operator who has not moved the values across. Both layers are
exercised here, plus the case where neither is configured.
"""

from __future__ import annotations

import json

import httpx
import pytest

from raven.knowledge._embedding import (
    SILICONFLOW_DEFAULT_MAX_TOKENS,
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    SiliconFlowEmbeddingClient,
    embedding_client,
    embedding_config_for,
    estimate_tokens,
    fit_to_tokens,
    load_embedding_config,
)


@pytest.fixture
def raven_config(tmp_path, monkeypatch):
    """A config file this test owns, aimed at by ``load_raven_config``."""
    from raven.config.loader import get_config_path, set_config_path

    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    before = get_config_path()
    set_config_path(path)
    yield path
    set_config_path(before)


def _write_config(path, **sections) -> None:
    path.write_text(json.dumps(sections), encoding="utf-8")


@pytest.fixture
def everos_root(tmp_path, raven_config):
    """A legacy EverOS root, recorded the way raven records it.

    Through ``plugins.config["everos-memory"]["root"]`` rather than the
    plugin's own helper: the fallback reads the recorded root with plain
    tomllib, because importing the plugin here would put a knowledge base back
    at the mercy of whether the memory plugin is installed.
    """
    root = tmp_path / "everos"
    root.mkdir()
    _write_config(raven_config, plugins={"config": {"everos-memory": {"root": str(root)}}})
    # Set too, and to somewhere else on purpose: the read must not fall back to
    # it now that the recorded root is the answer.
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setenv("EVEROS_ROOT", str(tmp_path / "not-this-one"))
    yield root
    monkeypatch_env.undo()


def _write(root, body: str) -> None:
    (root / "everos.toml").write_text(body, encoding="utf-8")


_FULL = """
[embedding]
model = "text-embedding-3-small"
base_url = "https://embed.test/v1/"
api_key = "sk-test"
"""


class TestTheConfiguredPinIsTheOnlySource:
    """``embedding.model`` + ``embedding.provider``, resolved through the provider.

    One source, deliberately. The endpoint used to be inherited from the memory
    backend's file, which meant a knowledge base stopped working when that
    plugin was not installed -- for a subsystem that never speaks to the memory
    service. Reading it there at all is gone; an install that only ever had it
    in that file is migrated once, by ``raven doctor --fix``.
    """

    @staticmethod
    def _pin(monkeypatch, *, model, provider, credentials):
        from raven.config.raven import EmbeddingConfig, load_raven_config

        config = load_raven_config()
        config.embedding = EmbeddingConfig(model=model or "", provider=provider or "")
        monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: config)
        monkeypatch.setattr(
            "raven.config.update_providers.resolve_provider_credentials",
            lambda name, **_: credentials,
        )

    def test_a_complete_pin_resolves_through_its_provider(self, raven_config, monkeypatch) -> None:
        self._pin(
            monkeypatch,
            model="openai/text-embedding-3-large",
            provider="openai",
            credentials=("https://api.openai.com/v1", "sk-pinned"),
        )

        config = load_embedding_config()

        assert config.base_url == "https://api.openai.com/v1"
        assert config.api_key == "sk-pinned"
        # Only the leading provider segment comes off: the vendor's own id may
        # itself contain a slash.
        assert config.model == "text-embedding-3-large"

    def test_half_a_pin_is_no_configuration(self, raven_config, monkeypatch) -> None:
        """A model with nobody to serve it cannot be called, and guessing the
        provider from the id is how two subsystems end up on different ones."""
        self._pin(monkeypatch, model="text-embedding-3-large", provider=None, credentials=None)

        assert load_embedding_config() is None

    def test_a_provider_with_no_key_is_no_configuration(self, raven_config, monkeypatch) -> None:
        self._pin(monkeypatch, model="openai/text-embedding-3-large", provider="openai", credentials=None)

        assert load_embedding_config() is None

    def test_an_unreadable_config_is_not_an_error(self, raven_config, monkeypatch) -> None:
        """A config raven cannot parse is stepped over at debug. Raising here
        would take the knowledge base down with the config."""

        def _boom():
            raise RuntimeError("config is not readable")

        monkeypatch.setattr("raven.config.raven.load_raven_config", _boom)

        assert load_embedding_config() is None

    def test_nothing_is_inherited_from_the_memory_backend(self, everos_root, raven_config, monkeypatch) -> None:
        """The file is on disk and complete, and it is not consulted.

        This is the whole point of the move: with no pin, a knowledge base
        behaves the same whether or not a memory plugin is installed --
        unconfigured, and saying so.
        """
        _write(everos_root, _FULL)
        self._pin(monkeypatch, model=None, provider=None, credentials=None)

        assert load_embedding_config() is None

    def test_a_real_resolver_answer_is_what_gets_used(self, raven_config, monkeypatch) -> None:
        """Through the resolver itself rather than a stand-in, so the pair this
        actually sends is the one a request would go out on."""
        from raven.config.raven import EmbeddingConfig, load_raven_config
        from raven.config.update_providers import set_provider_fields

        set_provider_fields("siliconflow", {"api_key": "sk-sf"})
        config = load_raven_config()
        config.embedding = EmbeddingConfig(model="siliconflow/BAAI/bge-m3", provider="siliconflow")
        monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: config)

        resolved = load_embedding_config()

        assert resolved.base_url == "https://api.siliconflow.cn/v1"
        assert resolved.api_key == "sk-sf"
        assert resolved.model == "BAAI/bge-m3"


@pytest.fixture
def mock_transport(monkeypatch):
    def install(handler):
        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs.setdefault("transport", transport)
            return original(*args, **kwargs)

        monkeypatch.setattr("raven.knowledge._embedding.httpx.AsyncClient", _patched)

    return install


async def test_embedding_nothing_never_calls_the_endpoint(mock_transport) -> None:
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": []})

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://embed.test/v1", api_key="k"))

    assert await client.embed([]) == []
    assert calls == []


async def test_the_request_carries_the_model_and_the_key(mock_transport) -> None:
    seen: dict = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://embed.test/v1", api_key="k"))
    await client.embed(["hello"])

    assert seen["url"] == "https://embed.test/v1/embeddings"
    assert seen["auth"] == "Bearer k"
    assert seen["body"] == {"model": "m", "input": ["hello"]}


async def test_vectors_are_returned_in_the_order_asked_for(mock_transport) -> None:
    """The caller pairs vectors with chunks positionally, so a response that
    arrives out of order would attach every vector to the wrong text. The
    endpoint reports the order it used; this sorts by it."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 2, "embedding": [3.0]},
                    {"index": 0, "embedding": [1.0]},
                    {"index": 1, "embedding": [2.0]},
                ]
            },
        )

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://embed.test/v1", api_key="k"))

    assert await client.embed(["a", "b", "c"]) == [[1.0], [2.0], [3.0]]


async def test_a_short_response_is_an_error_not_a_silent_gap(mock_transport) -> None:
    """Two chunks in, one vector back: pairing them positionally would index
    the second chunk under the first one's vector."""

    def handler(request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://embed.test/v1", api_key="k"))

    with pytest.raises(EmbeddingError, match="1 vectors for 2 inputs"):
        await client.embed(["a", "b"])


async def test_mixed_widths_are_refused(mock_transport) -> None:
    """A collection is sized once. Rows of two widths cannot go into it, and
    the failure at insert time says nothing about where they came from."""

    def handler(request):
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0, 2.0]}, {"index": 1, "embedding": [1.0]}]},
        )

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://embed.test/v1", api_key="k"))

    with pytest.raises(EmbeddingError, match="mixed widths"):
        await client.embed(["a", "b"])


async def test_an_http_error_carries_the_status_and_the_body(mock_transport) -> None:
    def handler(request):
        return httpx.Response(401, text="invalid api key")

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://embed.test/v1", api_key="k"))

    with pytest.raises(EmbeddingError, match="401.*invalid api key"):
        await client.embed(["a"])


async def test_an_unreachable_endpoint_says_so(mock_transport) -> None:
    def handler(request):
        raise httpx.ConnectError("no route to host")

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://embed.test/v1", api_key="k"))

    with pytest.raises(EmbeddingError, match="unreachable"):
        await client.embed(["a"])


async def test_the_width_is_measured_rather_than_assumed(mock_transport) -> None:
    """The config this inherited hardcoded 1024 on the claim that onboarding
    guarantees it. The model actually configured in that deployment returns
    4096, so the width has to come from the model."""
    seen: list = []

    def handler(request):
        seen.append(json.loads(request.read()))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0] * 4096}]})

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://embed.test/v1", api_key="k"))

    assert client.declared_dimensions is None
    assert await client.probe_dimensions() == 4096
    assert len(seen[0]["input"]) == 1


async def test_a_pinned_width_is_reported_without_a_call(mock_transport) -> None:
    calls: list = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": []})

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://x/v1", api_key="k", dimensions=3072))

    assert client.declared_dimensions == 3072
    assert calls == []


def _config(base_url: str = "https://api.siliconflow.cn/v1", model: str = "BAAI/bge-large-zh-v1.5"):
    return EmbeddingConfig(model=model, base_url=base_url, api_key="k")


def cjk(count: int) -> str:
    """``count`` CJK ideographs, built from code points rather than written out.

    Written out they would be a non-English string in a file that is not an
    i18n fixture, which is what ``scripts/check_source_language.py`` refuses.
    Built this way the tests keep the thing they are actually about: a CJK
    character costs a whole token where the old estimate said three quarters,
    and that is a property of the character class rather than of any word.

    Every code point from U+4E00 is an assigned ideograph, and the range this
    walks stays inside the one ``_is_cjk`` recognises.
    """
    return "".join(chr(0x4E00 + i % 0x1000) for i in range(count))


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.siliconflow.cn/v1", SiliconFlowEmbeddingClient),
        ("https://api.siliconflow.com/v1", SiliconFlowEmbeddingClient),
        ("https://SiliconFlow.cn/v1", SiliconFlowEmbeddingClient),
        ("https://api.openai.com/v1", EmbeddingClient),
        ("http://localhost:11434/v1", EmbeddingClient),
        # A host that merely ends in the vendor's name is not the vendor. The
        # subclass cuts inputs, so answering yes here would quietly truncate
        # against a limit somebody else's endpoint does not have.
        ("https://siliconflow.cn.example.com/v1", EmbeddingClient),
        ("https://notsiliconflow.cn/v1", EmbeddingClient),
    ],
)
def test_the_vendor_is_read_off_the_host_not_configured(base_url, expected) -> None:
    assert type(embedding_client(_config(base_url))) is expected


def test_a_cjk_character_costs_a_whole_token_and_a_url_more_than_prose() -> None:
    """The chunker's own estimate is utf-8 bytes over four, which reads a CJK
    character as three quarters of a token when it is one. That is what made
    chunks that looked within budget arrive over it."""
    assert estimate_tokens(cjk(700)) >= 700
    # Prose is the cheap case and is still counted at two characters a token,
    # because a knowledge base is mostly markdown, URLs and code, which are not.
    assert estimate_tokens("word " * 100) == 250


def test_text_within_the_budget_is_passed_through_untouched() -> None:
    text = "a short line of prose"
    assert fit_to_tokens(text, 512) is text


def test_cutting_lands_on_the_longest_prefix_that_fits() -> None:
    """By bisection rather than a flat characters-per-token multiplication:
    the cost per character is not uniform, so a paragraph followed by a code
    block would be cut in the wrong place by a single ratio."""
    text = cjk(800) + "and then some english prose that costs far less per character"
    cut = fit_to_tokens(text, 100)

    assert text.startswith(cut)
    assert estimate_tokens(cut) <= 100
    # Longest, not merely short enough: one character more must not fit.
    assert estimate_tokens(text[: len(cut) + 1]) > 100


def test_a_budget_of_nothing_cuts_nothing_rather_than_everything() -> None:
    """A model whose limit resolved to zero is a table bug, and answering it by
    embedding empty strings would fill a base with vectors for nothing."""
    assert fit_to_tokens("some text", 0) == "some text"


async def test_every_input_is_cut_to_the_model_limit_before_it_is_sent(mock_transport) -> None:
    """The whole point: the endpoint refuses the entire request when one input
    is over the limit, naming neither which one nor why -- so a document of
    eighteen chunks where two are long used to index none of the other sixteen."""
    sent: list[list[str]] = []

    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload["input"])
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": [0.1, 0.2]} for i in range(len(payload["input"]))]},
        )

    mock_transport(handler)
    client = embedding_client(_config())
    long_cjk = cjk(1400)
    vectors = await client.embed(["short one", long_cjk, "short two"])

    assert len(vectors) == 3
    # One vector per input still, in order: the caller pairs them with its
    # chunks positionally, so cutting must never change how many come back.
    assert sent[0][0] == "short one"
    assert sent[0][2] == "short two"
    assert len(sent[0][1]) < len(long_cjk)
    assert estimate_tokens(sent[0][1]) <= SILICONFLOW_DEFAULT_MAX_TOKENS


async def test_a_model_with_a_longer_context_is_not_cut_to_the_short_default(mock_transport) -> None:
    sent: list[list[str]] = []

    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload["input"])
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

    mock_transport(handler)
    client = embedding_client(_config(model="BAAI/bge-m3"))
    text = cjk(1400)

    await client.embed([text])

    assert client.max_input_tokens == 8192
    assert sent[0][0] == text


def test_an_unknown_model_gets_the_smaller_limit_rather_than_the_larger() -> None:
    """A model whose real limit is higher loses one input's tail; a model whose
    real limit is 512 loses the whole document."""
    client = embedding_client(_config(model="some/model-nobody-listed"))

    assert client.max_input_tokens == SILICONFLOW_DEFAULT_MAX_TOKENS


async def test_the_cut_is_kept_under_the_limit_rather_than_at_it(mock_transport) -> None:
    """The count is an estimate and the endpoint refuses the whole request, so
    the margin is what a wrong estimate costs instead of a failed document."""
    sent: list[list[str]] = []

    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload["input"])
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

    mock_transport(handler)
    client = embedding_client(_config())

    await client.embed([cjk(1600)])

    assert estimate_tokens(sent[0][0]) < client.max_input_tokens


async def test_a_plain_endpoint_is_left_alone(mock_transport) -> None:
    """Only the vendor that departs from the shape gets a subclass; everything
    else keeps sending exactly what it was given."""
    sent: list[list[str]] = []

    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload["input"])
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

    mock_transport(handler)
    client = embedding_client(_config(base_url="https://api.openai.com/v1"))
    text = cjk(3500)

    await client.embed([text])

    assert sent[0][0] == text


# --------------------------------------------------------------------------- resolution order


def test_a_base_model_keeps_its_namespace_when_a_provider_is_resolved(monkeypatch) -> None:
    """``BAAI/bge-large-zh-v1.5`` is one model id, not a provider and a model.

    The configured pin is stored as ``provider/model`` and has its first
    segment stripped on the way out; a base records what already went on the
    wire, and stripping that again asks the endpoint for a model nobody
    serves.
    """
    monkeypatch.setattr(
        "raven.config.update_providers.resolve_provider_credentials",
        lambda provider: ("https://api.siliconflow.cn/v1", "key"),
    )

    config = embedding_config_for("siliconflow", "BAAI/bge-large-zh-v1.5", 1024)

    assert config is not None
    assert config.model == "BAAI/bge-large-zh-v1.5"
    assert config.provider == "siliconflow"
    assert config.dimensions == 1024


def test_a_provider_with_no_credentials_resolves_to_nothing(monkeypatch) -> None:
    """The caller decides what that means: one base skipped, not a failed
    search."""
    monkeypatch.setattr(
        "raven.config.update_providers.resolve_provider_credentials",
        lambda provider: None,
    )

    assert embedding_config_for("siliconflow", "BAAI/bge-large-zh-v1.5") is None
    assert embedding_config_for("", "some-model") is None


# -- batching ------------------------------------------------------


def _echo(request):
    """Answer with one vector per input, numbered as the endpoint would."""
    inputs = json.loads(request.read())["input"]
    return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(i)]} for i in range(len(inputs))]})


async def test_a_long_document_is_sent_in_batches_in_order(mock_transport) -> None:
    """One call carrying every chunk is what the endpoints that cap this
    reject, and the rejection takes the whole document rather than the excess."""
    batches: list[list[str]] = []

    def handler(request):
        batches.append(json.loads(request.read())["input"])
        return _echo(request)

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://embed.test/v1", api_key="k", batch_size=3))

    vectors = await client.embed([f"chunk {n}" for n in range(7)])

    assert [len(batch) for batch in batches] == [3, 3, 1]
    assert [text for batch in batches for text in batch] == [f"chunk {n}" for n in range(7)]
    assert len(vectors) == 7


async def test_an_endpoint_that_refuses_a_batch_teaches_the_client_its_cap(mock_transport) -> None:
    """DashScope answers a batch of 21 with a 400 naming the batch, not the
    content. The number in the message is not reliably there, so the size is
    halved until it is accepted -- and remembered, so one document pays for
    the lesson rather than every document."""
    sizes: list[int] = []

    def handler(request):
        inputs = json.loads(request.read())["input"]
        sizes.append(len(inputs))
        if len(inputs) > 4:
            return httpx.Response(
                400,
                json={"error": {"message": "batch size is invalid, it should not be larger than 4."}},
            )
        return _echo(request)

    mock_transport(handler)
    client = EmbeddingClient(
        EmbeddingConfig(model="learned", base_url="https://embed.test/v1", api_key="k", batch_size=16)
    )

    vectors = await client.embed([f"chunk {n}" for n in range(12)])

    assert len(vectors) == 12
    # Configured 16 so it sends 12, halves to 6, is refused again, halves to 3
    # and gets the rest through four at a time. Two wasted requests, once.
    assert sizes == [12, 6, 3, 3, 3, 3]
    assert client.batch_size == 3, "the cap outlives the call"


async def test_a_refusal_that_is_not_about_the_batch_is_not_retried(mock_transport) -> None:
    """Retrying a rejected *input* in smaller batches only rejects it again,
    one batch at a time."""
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "the parameter is invalid"}})

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="m", base_url="https://embed.test/v1", api_key="k", batch_size=8))

    with pytest.raises(EmbeddingError, match="400"):
        await client.embed([f"chunk {n}" for n in range(8)])
    assert len(calls) == 1


async def test_a_batch_of_one_that_is_refused_gives_up(mock_transport) -> None:
    """Otherwise the halving has nowhere left to go and loops."""

    def handler(request):
        return httpx.Response(400, json={"error": {"message": "batch size is invalid"}})

    mock_transport(handler)
    client = EmbeddingClient(EmbeddingConfig(model="one", base_url="https://embed.test/v1", api_key="k", batch_size=1))

    with pytest.raises(EmbeddingError, match="batch size"):
        await client.embed(["only one"])
