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


def test_a_password_rewritten_in_place_with_the_same_mtime_is_still_the_one_scrubbed(tmp_path, monkeypatch):
    """A rewrite can land inside the filesystem's timestamp resolution; the files are read, not stamped."""
    import os

    from raven.config import held_secrets as hs
    from raven.ops.connection_add import store_password

    home = tmp_path / "home"
    monkeypatch.setenv("RAVEN_HOME", str(home))
    config = home / "config.json"
    home.mkdir()
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(hs, "get_config_path", lambda: config)
    hs._cache = None
    path = store_password("box", "correct-horse")
    assert hs.scrub_held_secrets("say correct-horse") == "say [redacted: the password kept for machine box]"
    stamp = path.stat()
    store_password("box", "battery-staple")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_mtime_ns == stamp.st_mtime_ns, "the rewrite kept the old mtime, as a fast one can"
    assert hs.scrub_held_secrets("say battery-staple") == "say [redacted: the password kept for machine box]"
    assert hs.scrub_held_secrets("say correct-horse") == "say correct-horse"


def test_a_machine_password_is_scrubbed_even_with_no_config_file(tmp_path, monkeypatch):
    """The config and the kept passwords live independently under the Raven home: a configless Raven can register a
    password machine, and its password must still be scrubbed (reviewed 2026-10-19 -- the missing config used to end
    the whole call before the passwords were read)."""
    from raven.config import held_secrets
    from raven.ops.connection_add import store_password

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    monkeypatch.setattr(held_secrets, "get_config_path", lambda: tmp_path / "no-such-config.json")
    monkeypatch.setattr(held_secrets, "_cache", None)

    assert held_secrets.scrub_held_secrets("pw: correct-horse") == "pw: correct-horse"
    store_password("box", "correct-horse")
    assert held_secrets.scrub_held_secrets("pw: correct-horse") == "pw: [redacted: the password kept for machine box]"
    assert held_secrets.scrub_held_secrets("sk-anything-at-all") == "sk-anything-at-all", (
        "no config holds no config credentials"
    )
