"""Unit tests for the single storage every Codex credential path goes through."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.providers.codex_token import CREDENTIAL_FILENAME, codex_storage


def test_codex_storage_lives_under_ravens_oauth_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("OAUTH_CLI_KIT_TOKEN_PATH", raising=False)

    path = codex_storage().get_token_path()

    assert path.name == CREDENTIAL_FILENAME
    assert tmp_path / ".raven" / "oauth" in path.parents


def test_codex_storage_does_not_adopt_the_codex_cli_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raven signs in for itself, so what the picker reports, the request path
    uses, and a disconnect removes are all the same file."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("OAUTH_CLI_KIT_TOKEN_PATH", raising=False)
    codex_cli = tmp_path / ".codex"
    codex_cli.mkdir()
    (codex_cli / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "A", "refresh_token": "R", "account_id": "acct"}}),
        encoding="utf-8",
    )

    assert codex_storage().load() is None


def test_codex_storage_is_named_for_ravens_slug() -> None:
    """The kit defaults to ``codex.json``; reading that name under our own
    directory would keep one credential answering to two spellings."""
    assert CREDENTIAL_FILENAME == "openai_codex.json"
