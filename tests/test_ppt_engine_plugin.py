"""The engine's plugin face: admission, per-turn seating, and the turn hook.

The fork registered its tools from inside its own loop
(``_register_ppt_tools``, pinned by the fork's ``tests/ppt/test_loop_registration.py``);
here the same guarantees are said in plugin vocabulary: the manifest's factory
rows decline or contribute (admission), every contributed tool resolves the
turn's working directory per call instead of taking one at construction (the
workdir cargo seat, verdict feature 13), and the material/deck frame the fork's
ACP layer ran around each prompt is one contributed hook (verdict feature 5).
"""

from __future__ import annotations

import asyncio
import json
import tomllib
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("pptx")

from raven.agent import workdir  # noqa: E402
from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402
from raven.plugins.context import PluginContext, ServiceLocator  # noqa: E402
from raven_ppt import plugin as plugin_module  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ENGINE_HOME = REPO / "plugins-dist" / "ppt-engine"
FORK = REPO / "subagents" / "raven-ppt" / "Raven-PPT"

ENABLED = {"enabled": True, "profile": "script_author"}


def _ctx(slice_: dict, workspace: Path) -> PluginContext:
    return PluginContext(
        config=slice_,
        services=ServiceLocator(workspace=workspace, user_id="u", agent_id="a"),
    )


def _manifest() -> dict:
    return tomllib.loads((ENGINE_HOME / "raven_ppt" / "raven-plugin.toml").read_text(encoding="utf-8"))


def _factories() -> dict[str, object]:
    rows = _manifest()["plugin"]["contributes"]
    found = {}
    for row in rows.get("tools", []) + rows.get("hooks", []):
        module, _, symbol = row["factory"].partition(":")
        assert module == "raven_ppt.plugin", row
        found[row["name"]] = getattr(plugin_module, symbol)
    return found


def test_the_manifest_contributes_the_fork_face_plus_the_self_named_search() -> None:
    """Ten fork tools, the D2 image search, and one hook -- no built-in shadowed."""
    rows = _manifest()["plugin"]["contributes"]
    names = [row["name"] for row in rows["tools"]]
    assert names == [
        "ppt_prepare",
        "ppt_brief",
        "ppt_fetch",
        "ppt_generate_image",
        "ppt_ingest",
        "ppt_figure_inspect",
        "ppt_outline",
        "ppt_template",
        "ppt_build",
        "ppt_review",
        "ppt_image_search",
    ]
    assert [row["name"] for row in rows["hooks"]] == ["ppt_engine"]
    assert "web_search" not in names, "D2: the image surface is self-named, never a shadow"


def test_an_absent_or_disabled_slice_casts_no_surface(tmp_path: Path) -> None:
    """The D6 admission shape: this wheel rides every install of the dev env,
    and an instance whose config never asked for deck tools gets none."""
    for slice_ in ({}, {"enabled": False}):
        ctx = _ctx(slice_, tmp_path / repr(sorted(slice_))[:9])
        for name, factory in _factories().items():
            assert factory(ctx) is None, name


def test_an_enabled_slice_contributes_every_row_with_the_forks_schemas(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "k")
    ctx = _ctx(dict(ENABLED), tmp_path)
    for name, factory in _factories().items():
        built = factory(ctx)
        assert built is not None, name
        assert built.name == name
        if name != "ppt_engine":
            assert built.parameters["type"] == "object"


def test_image_search_declines_without_a_key(tmp_path: Path, monkeypatch) -> None:
    """The fork's loop registered no keyless search tool; the same refusal,
    said as a factory decline."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    assert plugin_module.make_ppt_image_search(_ctx(dict(ENABLED), tmp_path)) is None


def test_a_malformed_slice_casts_the_fail_closed_sentinel(tmp_path: Path) -> None:
    """The host's stack builder logs-and-skips a raising factory, so a parse
    error that escaped would boot this deck product with no deck face at all
    under a config that says enabled: true. Instead the tools decline and the
    hook seat is taken by a sentinel that answers every turn with the config
    fix named -- the code-flow MisconfiguredGate doctrine (w101), on this
    plugin's one always-cast seat."""
    bad_slices = (
        {"enabled": True, "viewsPerCall": 0},
        {"enabled": True, "renderDpi": "300"},
        {"enabled": "false"},
        # G2: the fork refused a route typo at config load, naming the
        # alternatives; a fallback to script_author with only a process log
        # would be the silent inverse of that contract.
        {"enabled": True, "profile": "script_writer"},
    )
    for index, slice_ in enumerate(bad_slices):
        ctx = _ctx(dict(slice_), tmp_path / str(index))
        for name, factory in _factories().items():
            if name == "ppt_engine":
                continue
            assert factory(ctx) is None, (slice_, name)
        sentinel = plugin_module.make_hook(ctx)
        assert sentinel is not None, slice_
        decision = asyncio.run(sentinel.before_user_inbound(AgentHookContext(session_key="s", inbound_content="hi")))
        reply, media = decision.short_circuit_result
        assert 'plugins.config["ppt-engine"]' in reply
        assert media == []
    assert (
        "viewsPerCall"
        in asyncio.run(
            plugin_module.make_hook(_ctx({"enabled": True, "viewsPerCall": 0}, tmp_path / "named")).before_user_inbound(
                AgentHookContext(session_key="s", inbound_content="hi")
            )
        ).short_circuit_result[0]
    )
    profile_reply = asyncio.run(
        plugin_module.make_hook(
            _ctx({"enabled": True, "profile": "script_writer"}, tmp_path / "routes")
        ).before_user_inbound(AgentHookContext(session_key="s", inbound_content="hi"))
    ).short_circuit_result[0]
    assert "script_writer" in profile_reply and "script_author" in profile_reply


def test_a_well_typed_slice_never_meets_the_sentinel(tmp_path: Path) -> None:
    """The strict parser must not widen into refusing what the fork accepted:
    the shipped five-key slice and every defaulted key parse clean."""
    ctx = _ctx(
        {"enabled": True, "profile": "script_author", "composerModel": "", "renderDpi": 144, "renderConcurrency": 2},
        tmp_path,
    )
    hook = plugin_module.make_hook(ctx)
    assert type(hook).__name__ == "PptEngineHook"


def test_a_tool_call_outside_a_turn_is_refused_with_the_reason(tmp_path: Path) -> None:
    build = plugin_module.make_ppt_build(_ctx(dict(ENABLED), tmp_path))
    body = json.loads(asyncio.run(build.execute(project="deck")))
    assert body["ok"] is False
    assert "working directory" in body["error"]


def test_each_bound_workdir_gets_its_own_fenced_engine(tmp_path: Path) -> None:
    """The fork built one engine per session because ``Project.root`` fences one
    deck per workspace; the wrapper preserves that per bound directory."""
    brief = plugin_module.make_ppt_brief(_ctx(dict(ENABLED), tmp_path / "ws"))

    async def record(where: Path) -> dict:
        with workdir.bind(where):
            return json.loads(
                await brief.execute(
                    project="deck",
                    language="English",
                    audience="team",
                    occasion="demo",
                    minutes=10,
                    pages_low=3,
                    pages_high=5,
                )
            )

    one, two = tmp_path / "one", tmp_path / "two"
    for where in (one, two):
        where.mkdir()
        assert asyncio.run(record(where))["ok"] is True
    assert (one / "deck").is_dir() and (two / "deck").is_dir()


def test_the_wrappers_schema_and_budget_are_the_fork_tools_own(tmp_path: Path) -> None:
    review = plugin_module.make_ppt_review(_ctx(dict(ENABLED), tmp_path))
    from raven_ppt.tools.review import PptReviewTool

    assert review.timeout_seconds == PptReviewTool.timeout_seconds
    assert review.description == PptReviewTool.description
    assert set(review.parameters["properties"]) == {"project", "pages"}


def _pptx(path: Path, slides: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1, slides + 1):
            archive.writestr(f"ppt/slides/slide{index}.xml", "<sld/>")
    return path


def test_the_hook_stages_rewrites_and_announces(tmp_path: Path) -> None:
    """The fork's per-prompt frame (stage -> describe -> verify -> announce),
    end to end on the hook's two phases inside one workdir bind."""
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    source = tmp_path / "notes.md"
    source.write_text("facts", encoding="utf-8")
    ctx = AgentHookContext(session_key="s1", inbound_content=f"build a deck from {source}")

    async def run() -> tuple:
        with workdir.bind(wd):
            inbound = await hook.before_user_inbound(ctx)
            deck = _pptx(wd / "out" / "deck.pptx", slides=3)
            ctx.outbound_content = f"done\nMEDIA: {deck}"
            outbound = await hook.after_send(ctx)
        return inbound, outbound, deck

    inbound, outbound, deck = asyncio.run(run())
    assert "# Material staged for this run" in inbound.modified_content
    assert f"Compile the deck under {wd / 'out'}/" in inbound.modified_content
    assert (wd / "materials" / "notes.md").read_text(encoding="utf-8") == "facts"
    assert f"Published a 3-slide deck.\nDeck: {deck}\nMEDIA: {deck}" in outbound.modified_content


def test_a_staging_failure_short_circuits_the_turn_with_the_forks_sentence(tmp_path: Path) -> None:
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(
        session_key="s1",
        inbound_content='```raven-ppt\n{"materials": ["/nowhere/gone.md"]}\n```',
    )

    async def run():
        with workdir.bind(wd):
            return await hook.before_user_inbound(ctx)

    decision = asyncio.run(run())
    reply, media = decision.short_circuit_result
    assert reply.startswith("The material could not be staged.")
    assert media == []


def test_a_claimed_deck_that_verifies_as_nothing_is_called_out(tmp_path: Path) -> None:
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(session_key="s1", inbound_content="make it from nothing then")

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(ctx)
            (wd / "out").mkdir(exist_ok=True)
            (wd / "out" / "deck.pptx").write_bytes(b"not a deck")
            ctx.outbound_content = f"MEDIA: {wd / 'out' / 'deck.pptx'}"
            return await hook.after_send(ctx)

    decision = asyncio.run(run())
    assert "No verifiable deck was published" in decision.modified_content


def test_an_earlier_turns_deck_is_not_announced_as_this_turns(tmp_path: Path) -> None:
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    _pptx(wd / "out" / "earlier.pptx")

    async def run():
        with workdir.bind(wd):
            ctx = AgentHookContext(session_key="s1", inbound_content="just answer a question")
            await hook.before_user_inbound(ctx)
            ctx.outbound_content = "no deck this turn"
            return await hook.after_send(ctx)

    decision = asyncio.run(run())
    assert decision.modified_content is None


def test_the_d3_page_stamp_rides_the_encode_path(tmp_path: Path) -> None:
    """A labelled page render comes back taller than the page by a strip: the
    page's own pixels are untouched, and identity survives a host that demotes
    tool images away from the text beside them (D3)."""
    from PIL import Image

    from raven.utils.images import image_pixel_size
    from raven_ppt.stages._views import DeckViews

    png = tmp_path / "page.png"
    Image.new("RGB", (320, 180), (250, 250, 250)).save(png)
    views = DeckViews()
    import base64

    def decode(uri: str) -> bytes:
        return base64.b64decode(uri.split(",", 1)[1])

    bare = image_pixel_size(decode(views.data_uri(png)))
    stamped = image_pixel_size(decode(views.data_uri(png, label="page 3")))
    assert bare == (320, 180)
    assert stamped[0] == 320 and stamped[1] > 180


def test_the_identity_prompts_ride_the_wheel_for_the_seeding_wave() -> None:
    """pw2b's write-if-missing seeding needs the three drifted prompts as
    package data; byte-equality against the fork is the launcher family's pin,
    presence under the import home is this one."""
    import raven_ppt

    home = Path(raven_ppt.__file__).parent / "prompts"
    assert {p.name for p in home.glob("*.md")} == {"SOUL.md", "AGENTS.md", "TOOLS.md"}


def test_discovery_sees_the_distribution_through_the_entry_point() -> None:
    """The everos shape: the group names the package, the manifest is package
    data, and activation admits the plugin beside everos-memory."""
    from raven.plugins.discover import PluginDiscovery

    found = {record.manifest.id for record in PluginDiscovery(entry_points_group="raven.plugins").discover()}
    assert "ppt-engine" in found


def test_the_first_turn_seeds_the_deck_identity_into_the_granted_home(tmp_path: Path) -> None:
    """The fork seeded its three drifted prompts into every session workspace
    and its per-session context builder read them back; the trunk host reads
    identity from ONE agent home, so the hook's first turn seeds that home:
    the wheel's carried bytes into the exact seats the context builder names,
    then the host's own template sync for the rest -- write-if-missing all
    the way down (bu5: the identity pieces ride the plugin that teaches the
    behaviour, and the tree delivers what the charter says it carries)."""
    import raven_ppt
    from raven_ppt.plugin.hook import IDENTITY_SEATS

    home = tmp_path / "home"
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), home))
    wd = tmp_path / "session"
    wd.mkdir()

    async def run():
        with workdir.bind(wd):
            return await hook.before_user_inbound(AgentHookContext(session_key="s", inbound_content="a deck please"))

    asyncio.run(run())
    prompts = Path(raven_ppt.__file__).parent / "prompts"
    for name, seat in IDENTITY_SEATS:
        assert (home / seat).read_bytes() == (prompts / name).read_bytes(), seat
    # The host's own template sync covered the rest of the set.
    assert (home / "HEARTBEAT.md").is_file()
    assert (home / "user_memory" / "profile" / "user.md").is_file()
    # And the identity landed in the home, never in the turn's directory.
    assert not (wd / "agent_memory").exists()


def test_an_operators_identity_edit_outlives_every_later_first_turn(tmp_path: Path) -> None:
    """Write-if-missing is the fork's own contract: a soul tuned in place is
    never overwritten, by this process or the next."""
    home = tmp_path / "home"
    seat = home / "agent_memory" / "profile" / "soul.md"
    seat.parent.mkdir(parents=True)
    seat.write_text("my own deck voice\n", encoding="utf-8")
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), home))
    wd = tmp_path / "session"
    wd.mkdir()

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(AgentHookContext(session_key="s", inbound_content="hi there"))

    asyncio.run(run())
    assert seat.read_text(encoding="utf-8") == "my own deck voice\n"
    assert (home / "TOOLS.md").is_file(), "the missing seats are still filled"


def test_the_sentinel_never_seeds(tmp_path: Path) -> None:
    """A misconfigured slice proves nothing about intent; the fail-closed
    seat refuses turns and writes nothing anywhere."""
    home = tmp_path / "home"
    sentinel = plugin_module.make_hook(_ctx({"enabled": "false"}, home))
    asyncio.run(sentinel.before_user_inbound(AgentHookContext(session_key="s", inbound_content="hi")))
    assert not home.exists()
