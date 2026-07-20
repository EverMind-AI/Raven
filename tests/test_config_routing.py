"""RoutingConfig / ModelEndpoint schema: defaults, knn fields, aliases, back-compat."""

from __future__ import annotations

from raven.config.schema import ModelEndpoint, RoutingConfig


def test_model_endpoint_defaults():
    ep = ModelEndpoint()
    assert ep.model == ""
    assert ep.api_base == ""
    assert ep.api_key == "EMPTY"


def test_routing_defaults():
    cfg = RoutingConfig()
    # Existing fields keep their original defaults (back-compat).
    assert cfg.enabled is False
    assert cfg.profile == "balanced"
    assert cfg.api_key == ""
    # New backend switch defaults to the original ecoclaw router.
    assert cfg.backend == "ecoclaw"
    # New knn fields have inert defaults.
    assert cfg.models == []
    assert cfg.memory_path == ""
    assert cfg.k == 30
    assert cfg.lambda_cost == 0.0
    assert cfg.embedding_endpoint == ""
    # Safety-gate defaults: leave the default model only with enough evidence.
    assert cfg.min_similarity == 0.6
    assert cfg.min_similar_neighbors == 4
    assert cfg.min_memory_size == 10
    assert cfg.min_margin == 0.0


def test_knn_config_construction():
    cfg = RoutingConfig(
        enabled=True,
        backend="knn",
        k=5,
        lambda_cost=0.5,
        memory_path="/tmp/knn_memory.json",
        embedding_endpoint="http://localhost:9100/embed",
        models=[
            {"model": "small", "api_base": "http://a/v1"},
            {"model": "large", "api_base": "http://b/v1", "api_key": "K"},
        ],
    )
    assert cfg.backend == "knn"
    assert cfg.k == 5
    assert cfg.lambda_cost == 0.5
    assert len(cfg.models) == 2
    assert all(isinstance(m, ModelEndpoint) for m in cfg.models)
    assert cfg.models[0].model == "small"
    assert cfg.models[0].api_key == "EMPTY"  # default applied
    assert cfg.models[1].api_key == "K"


def test_camel_case_aliases():
    # Base uses a camelCase alias generator with populate_by_name.
    cfg = RoutingConfig(memoryPath="/x", lambdaCost=0.3, embeddingEndpoint="http://e")
    assert cfg.memory_path == "/x"
    assert cfg.lambda_cost == 0.3
    assert cfg.embedding_endpoint == "http://e"


def test_backward_compat_ecoclaw_fields():
    cfg = RoutingConfig(enabled=True, profile="eco", api_key="sk-or-x")
    assert cfg.enabled is True
    assert cfg.profile == "eco"
    assert cfg.api_key == "sk-or-x"
    assert cfg.backend == "ecoclaw"
