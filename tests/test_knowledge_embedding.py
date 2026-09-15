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
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
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


def test_the_configured_endpoint_is_read(everos_root) -> None:
    _write(everos_root, _FULL)
    config = load_embedding_config()

    assert config.model == "text-embedding-3-small"
    assert config.api_key == "sk-test"
    assert config.dimensions is None  # unpinned: ask the model, never guess


def test_the_trailing_slash_is_dropped(everos_root) -> None:
    """The request appends /embeddings, so a kept slash sends //embeddings and
    some gateways answer 404 to that."""
    _write(everos_root, _FULL)
    assert load_embedding_config().base_url == "https://embed.test/v1"


def test_a_width_in_the_config_wins_over_the_default(everos_root) -> None:
    _write(everos_root, _FULL + "dimensions = 3072\n")
    assert load_embedding_config().dimensions == 3072


def test_a_nonsense_width_is_treated_as_unpinned(everos_root) -> None:
    _write(everos_root, _FULL + "dimensions = 0\n")
    assert load_embedding_config().dimensions is None


@pytest.mark.parametrize(
    "body",
    [
        "",
        "[embedding]\nmodel = 'm'\n",
        "[embedding]\nmodel = 'm'\nbase_url = 'https://x/v1'\n",
        "[embedding]\nbase_url = 'https://x/v1'\napi_key = 'k'\n",
    ],
    ids=["empty", "model-only", "no-key", "no-model"],
)
def test_an_incomplete_section_is_no_configuration_at_all(everos_root, body) -> None:
    """Half a config cannot embed. Returning it would defer the failure to the
    first upload, which is where it is hardest to read."""
    _write(everos_root, body)
    assert load_embedding_config() is None


def test_a_missing_file_is_not_an_error(everos_root) -> None:
    """A deployment with no embedding configured is one with no knowledge
    bases, which is an ordinary state -- not a failed start."""
    assert load_embedding_config() is None


def test_unparseable_toml_is_not_an_error(everos_root) -> None:
    _write(everos_root, "[embedding\nmodel =")
    assert load_embedding_config() is None


# ── the client ────────────────────────────────────────────────────


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


def test_the_recorded_root_wins_over_the_environment(tmp_path, monkeypatch, raven_config) -> None:
    """The bug this replaced: reading EVEROS_ROOT made one installation answer
    two different things -- the recorded root once the memory backend had
    exported the variable, a hardcoded ~/.everos/raven before that. On the
    deployment it was found on those were two files with two endpoints, one of
    them keyless."""
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    _write(recorded, _FULL)
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    _write(ambient, _FULL.replace("text-embedding-3-small", "wrong-model"))

    _write_config(raven_config, plugins={"config": {"everos-memory": {"root": str(recorded)}}})
    monkeypatch.setenv("EVEROS_ROOT", str(ambient))

    config = load_embedding_config()

    assert config is not None
    assert config.model == "text-embedding-3-small"


# --------------------------------------------------------------------------- resolution order


def test_ravens_own_section_is_the_answer(raven_config) -> None:
    """The endpoint a knowledge base uses is raven's to hold: it indexes and
    answers inside the gateway process and never speaks to the memory service."""
    _write_config(
        raven_config,
        embedding={"model": "host-model", "baseUrl": "https://host.test/v1/", "apiKey": "sk-host"},
    )

    config = load_embedding_config()

    assert config is not None
    assert (config.model, config.base_url, config.api_key) == ("host-model", "https://host.test/v1", "sk-host")


def test_ravens_own_section_wins_over_the_legacy_file(everos_root, raven_config) -> None:
    """An operator who filled raven's section is done; the old file is history."""
    _write(everos_root, _FULL)
    _write_config(
        raven_config,
        embedding={"model": "host-model", "baseUrl": "https://host.test/v1", "apiKey": "sk-host"},
        plugins={"config": {"everos-memory": {"root": str(everos_root)}}},
    )

    config = load_embedding_config()

    assert config is not None
    assert config.model == "host-model"


def test_a_half_filled_section_falls_through_to_the_legacy_file(everos_root, raven_config) -> None:
    """All three strings or nothing. A model with no key cannot embed, and
    treating it as an answer would hide a usable legacy file behind it."""
    _write(everos_root, _FULL)
    _write_config(
        raven_config,
        embedding={"model": "host-model"},
        plugins={"config": {"everos-memory": {"root": str(everos_root)}}},
    )

    config = load_embedding_config()

    assert config is not None
    assert config.model == "text-embedding-3-small"


def test_no_section_and_no_recorded_root_answers_none(raven_config) -> None:
    """An install with no embedding configured has no knowledge bases, which is
    an ordinary state -- and with no memory plugin there is no file to inherit
    from either. The caller turns this into "configure this first"."""
    _write_config(raven_config, memory={"backend": None})

    assert load_embedding_config() is None
