"""Tests for ``commands.catalog`` RPC handler.

Reflects ``raven.cli.commands.app`` and returns a
``CommandsCatalogResponse``-shaped dict.

Coverage:
- shape contract
- canon contains known commands
- excludes blacklist + REPL
- dispatch-compat lives in test_tui_rpc_cli_dispatch.py
"""

from __future__ import annotations

import pytest

from raven.tui_rpc.methods.commands import commands_catalog


@pytest.mark.asyncio
async def test_catalog_returns_non_null_shape() -> None:
    """REQ-1 — handler returns dict with all 5 required keys at correct types.

    Does not assert content; that's TDD-3 / TDD-5. Just shape.
    """
    result = await commands_catalog({})
    assert isinstance(result, dict)
    # 5 required keys per CommandsCatalogResponse schema
    assert set(result.keys()) >= {"canon", "pairs", "sub", "categories", "skill_count"}
    assert isinstance(result["canon"], dict)
    assert isinstance(result["pairs"], list)
    assert isinstance(result["sub"], dict)
    assert isinstance(result["categories"], list)
    assert isinstance(result["skill_count"], int)
    assert result["skill_count"] >= 0
    # `warning` is optional; if present must be str
    if "warning" in result and result["warning"] is not None:
        assert isinstance(result["warning"], str)


# ---------------------------------------------------------------------------
# reflection + mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_canon_contains_known_commands() -> None:
    """REQ-2 — canon includes one entry per visible top-level command and
    per visible group head.

    Per hermes catalog contract (TS ``parseSlashCommand`` extracts a
    single-token slash name; see ``ui-tui/src/domain/slash.ts:6-10`` +
    ``createSlashHandler.ts:53-79``) canon keys MUST be single-token —
    multi-token keys ``/skill list`` etc. never exact-match against
    ``parsed.name`` and false-trigger prefix-1-match as ambiguous,
    breaking previously-working slashes. The full subcommand surface
    lives in ``sub`` (asserted in ``test_catalog_sub_grouping``).
    """
    result = await commands_catalog({})
    canon = result["canon"]
    # Single-token entries: one top-level (status; onboard/gateway/agent
    # are filtered) + 6 group heads.
    expected = [
        "/status",      # top-level
        "/skill",       # group head — TS exact match falls through; slash.exec("skill <sub>") routes via cli.dispatch
        "/sentinel",
        "/channels",
        "/provider",
        "/cron",
        "/sandbox",
    ]
    missing = [e for e in expected if e not in canon]
    assert not missing, f"expected canon entries missing: {missing}; canon keys: {sorted(canon)}"
    # v0.1: alias = canonical 1:1
    for e in expected:
        assert canon[e] == e, f"canon[{e!r}] expected {e!r}, got {canon[e]!r}"
    # NO multi-token canon entries — that was the smoke-discovered regression.
    multi_token = [k for k in canon if " " in k]
    assert not multi_token, (
        f"canon must be single-token-only (hermes contract); leaked: {multi_token}"
    )


@pytest.mark.asyncio
async def test_catalog_pairs_shape_and_consistency() -> None:
    """pairs must be a list of 2-tuples + 1:1 with canon."""
    result = await commands_catalog({})
    pairs = result["pairs"]
    canon = result["canon"]
    assert isinstance(pairs, list) and pairs, "pairs must be non-empty"
    for entry in pairs:
        # JSON deserialises tuples as lists; handler must produce length-2.
        assert len(entry) == 2 and all(isinstance(x, str) for x in entry), (
            f"pairs entry malformed: {entry!r}"
        )
    # pairs (alias, canonical) ≡ canon items (order may differ — set-compare).
    pairs_set = {(a, c) for a, c in pairs}
    canon_set = set(canon.items())
    assert pairs_set == canon_set, (
        f"pairs / canon drift: only-in-pairs={pairs_set - canon_set}; "
        f"only-in-canon={canon_set - pairs_set}"
    )


@pytest.mark.asyncio
async def test_catalog_sub_grouping() -> None:
    """sub dict groups subcommands per group.

    Asserts that every reflected group appears with its subcommands; the
    blacklist-exclusion assertions live in the filter test below.
    """
    result = await commands_catalog({})
    sub = result["sub"]
    # Each major group present with at least the canonical non-blacklisted
    # subcommands. We do NOT assert the entire subcommand list here — the
    # filter test handles exclusions.
    assert "channels" in sub
    assert "skill" in sub
    assert "sentinel" in sub
    assert "cron" in sub
    assert "provider" in sub
    assert "sandbox" in sub
    # Spot-check representative subcommands
    assert "list" in sub["channels"]
    assert "status" in sub["channels"]
    # channels login is no longer blanket-blacklisted, so it surfaces
    # in the catalog (per-channel gating happens at dispatch time, not here).
    assert "login" in sub["channels"]
    assert "list" in sub["skill"]
    assert "nudges" in sub["sentinel"]
    assert "routines" in sub["sentinel"]


@pytest.mark.asyncio
async def test_catalog_categories_order() -> None:
    """categories: '(top-level)' first then alphabetical groups (post-blacklist).

    Asserts the leading element + presence of the live groups; the full
    filter audit lives in the filter test below.
    """
    result = await commands_catalog({})
    categories = result["categories"]
    assert categories[0] == "(top-level)", (
        f"first category must be '(top-level)', got {categories[0]!r}"
    )
    # Live groups always present
    for g in ("channels", "cron", "provider", "sandbox", "sentinel", "skill"):
        assert g in categories, f"group {g!r} missing from categories: {categories}"
    # Alphabetical ordering for the post-(top-level) tail
    tail = [c for c in categories[1:] if c not in ("(top-level)",)]
    assert tail == sorted(tail), f"categories tail not alphabetical: {tail}"


# ---------------------------------------------------------------------------
# blacklist + REPL filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_excludes_blacklist_and_repl() -> None:
    """REQ-2 / REQ-4 — blacklisted entries + agent-REPL absent from catalog.

    Asserts ``canon`` / ``pairs`` / ``sub`` / ``categories`` are all consistent
    in their exclusions (no half-filter — one canonical source).
    """
    result = await commands_catalog({})
    canon = result["canon"]
    sub = result["sub"]
    categories = result["categories"]

    # Blacklist entries — none must appear as canon keys.
    must_not_be_in_canon = [
        "/gateway",
        "/provider login",
        "/sandbox shell",
        "/tui",
        "/onboard",
        # agent (REPL no-arg form) — special-cased separately.
        "/agent",
    ]
    leaked = [c for c in must_not_be_in_canon if c in canon]
    assert not leaked, f"catalog leaked blacklist entries: {leaked}"

    # Blacklisted subgroup entries also absent from `sub`.
    # (channels login is NOT blacklisted anymore — see test_catalog_sub_grouping.)
    assert "login" not in sub.get("provider", []), "provider login leaked into sub"
    assert "shell" not in sub.get("sandbox", []), "sandbox shell leaked into sub"

    # `tui` group fully filtered (only entry is the group-callback; once
    # filtered, the group has no members and disappears from categories).
    assert "tui" not in categories, f"tui group should be filtered out: {categories}"

    # `agent` top-level (REPL) absent — kept simple.
    # If a future change wires `/agent <message>` we'd amend this test.
    assert any(c.startswith("/") for c in canon), "canon must still have entries"


# ---------------------------------------------------------------------------
# skill_count source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_count_propagated_to_response(monkeypatch) -> None:
    """REQ-5 — handler reads from _compute_skill_count and passes value through.

    Decoupled from SqliteStore so the test asserts the contract (catalog
    response surfaces the count) without binding to the store schema.
    """
    from raven.tui_rpc.methods import commands

    monkeypatch.setattr(commands, "_compute_skill_count", lambda: (42, None))
    result = await commands.commands_catalog({})
    assert result["skill_count"] == 42
    assert "warning" not in result or result["warning"] is None


@pytest.mark.asyncio
async def test_skill_count_fallback_warning_propagated(monkeypatch) -> None:
    """REQ-5 fallback — when _compute_skill_count returns (0, warning) the
    response surfaces warning string + skill_count 0.
    """
    from raven.tui_rpc.methods import commands

    monkeypatch.setattr(
        commands,
        "_compute_skill_count",
        lambda: (0, "skill store not initialized"),
    )
    result = await commands.commands_catalog({})
    assert result["skill_count"] == 0
    assert result.get("warning") == "skill store not initialized"


def test_compute_skill_count_fallback_zero_on_db_missing(monkeypatch, tmp_path) -> None:
    """_compute_skill_count returns (0, warning) when load_config raises.

    Validates the wrapper's defensive try/except — any failure (config
    not loaded, store init exception, schema mismatch) yields (0, warning_str)
    rather than propagating so the hermes UI never sees a broken catalog
    over skill counting.
    """
    from raven.tui_rpc.methods import commands

    def _explode() -> None:
        raise RuntimeError("config_missing")

    # Patch load_config (lazy-imported inside _compute_skill_count) to raise.
    monkeypatch.setattr(
        "raven.config.loader.load_config", _explode, raising=False
    )
    n, warning = commands._compute_skill_count()
    assert n == 0
    assert warning is not None and "config_missing" in warning


@pytest.mark.asyncio
async def test_catalog_filters_hidden_typer_commands(monkeypatch) -> None:
    """Reflection must honor ``CommandInfo.hidden=True`` — hidden commands
    are absent from ``--help`` and must be absent from the slash catalog.

    No real EC CLI command currently sets ``hidden=True``, so this guards
    against a future contributor adding a hidden command and being
    surprised that it shows up as a slash. Uses a fake Typer app to avoid
    coupling to the real CLI surface.
    """
    import typer as _typer

    import raven.cli.commands as ec_commands
    from raven.tui_rpc.methods import commands

    fake = _typer.Typer(no_args_is_help=False)

    @fake.command(name="visible-cmd")
    def _visible() -> None:  # pragma: no cover — never invoked
        pass

    @fake.command(name="hidden-cmd", hidden=True)
    def _hidden() -> None:  # pragma: no cover — never invoked
        pass

    monkeypatch.setattr(ec_commands, "app", fake)
    result = await commands.commands_catalog({})
    canon = result["canon"]
    assert "/visible-cmd" in canon, f"visible command missing from canon: {sorted(canon)}"
    assert "/hidden-cmd" not in canon, (
        f"hidden=True command leaked into catalog: {sorted(canon)}"
    )


def test_catalog_filter_uses_dispatch_blacklist_constant() -> None:
    """REQ-4 — single source of truth: catalog filter reads ``_DISPATCH_BLACKLIST``.

    Imports the constant from cli_dispatch and asserts the filter function
    refers to it directly (no parallel hardcoded list in ``commands.py``).
    """
    from raven.tui_rpc.methods import cli_dispatch, commands

    # commands.py must NOT define its own _BLACKLIST / similar.
    has_local = any(
        name.endswith("BLACKLIST") and not name.startswith("__")
        for name in dir(commands)
        if name != "_DISPATCH_BLACKLIST"  # allow re-export but not a new copy
    )
    # If the filter exports something blacklist-like, it MUST be the
    # cli_dispatch constant by identity, not a clone.
    if hasattr(commands, "_DISPATCH_BLACKLIST"):
        assert commands._DISPATCH_BLACKLIST is cli_dispatch._DISPATCH_BLACKLIST, (
            "commands module shadowed the blacklist instead of importing it"
        )
    assert not has_local, "commands.py defined its own *BLACKLIST — break single source of truth"
