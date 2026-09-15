"""The a2a config section: defaults, peer lookup keys, and camelCase wire keys."""

from raven.config.schema import A2aConfig, A2aPeerConfig, Config


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
            # Deliberately not "bearer": that is also this field's default, so a
            # broken camelCase alias would fall back to it and the assertion below
            # would pass without the wire key ever having been read.
            "peers": [{"origin": "https://peer.example.com", "authScheme": "X-Api-Key", "credential": "sekrit"}],
        }
    )
    assert cfg.server.enabled is True
    assert cfg.peers[0].origin == "https://peer.example.com"
    assert cfg.peers[0].auth_scheme == "X-Api-Key"
    assert cfg.peers[0].credential == "sekrit"
    assert A2aPeerConfig(origin="https://peer.example.com").auth_scheme == "bearer"
