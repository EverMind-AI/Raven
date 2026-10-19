"""The password kept for a registered machine is one more credential Raven holds, and is scrubbed like the rest."""

from __future__ import annotations


def test_a_machine_password_kept_here_is_scrubbed_from_what_a_model_reads_until_it_is_forgotten(tmp_path, monkeypatch):
    from raven.config import held_secrets
    from raven.ops.connection_add import forget_password, store_password

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(held_secrets, "get_config_path", lambda: config)
    monkeypatch.setattr(held_secrets, "_cache", None)

    assert held_secrets.scrub_held_secrets("pw: correct-horse") == "pw: correct-horse"
    store_password("box", "correct-horse")
    assert held_secrets.scrub_held_secrets("pw: correct-horse") == "pw: [redacted: the password kept for machine box]"
    store_password("box", "battery-staple")
    assert held_secrets.scrub_held_secrets("correct-horse battery-staple") == (
        "correct-horse [redacted: the password kept for machine box]"
    ), "a rewritten password is read again, the old one is not held"
    forget_password("box")
    assert held_secrets.scrub_held_secrets("pw: battery-staple") == "pw: battery-staple"


def test_the_scrubber_looks_where_the_password_is_kept():
    """``held_secrets`` spells the password file's place itself (no ``raven.ops`` import from ``raven.config``); it must stay the place ``store_password`` writes to."""
    from raven.config.held_secrets import MACHINE_PASSWORD_GLOB
    from raven.home import raven_home
    from raven.ops.connections import password_path

    kept = password_path("box")
    assert kept.parent == raven_home() / MACHINE_PASSWORD_GLOB[0]
    assert kept.match(MACHINE_PASSWORD_GLOB[1])
