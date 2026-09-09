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
    assert set(review.parameters["properties"]) == {"project", "pages", "dismiss"}


def _pptx(path: Path, slides: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1, slides + 1):
            archive.writestr(f"ppt/slides/slide{index}.xml", "<sld/>")
    return path


def _published(own: Path, deck: Path) -> None:
    """Record `deck` the way the publish step does, so the hook can tell it from a copy."""
    import hashlib
    import json

    state = own / "deck" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "published.json").write_text(
        json.dumps({"published": [{"path": str(deck), "sha256": hashlib.sha256(deck.read_bytes()).hexdigest()}]}),
        encoding="utf-8",
    )


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
            # The turn now runs in this session's own folder under the bound directory.
            own = Path(workdir.current())
            deck = _pptx(own / "out" / "deck.pptx", slides=3)
            _published(own, deck)
            ctx.outbound_content = f"done\nMEDIA: {deck}"
            outbound = await hook.after_send(ctx)
        return inbound, outbound, deck, own

    inbound, outbound, deck, own = asyncio.run(run())
    assert own == wd / "decks" / "s1"
    assert "# Material staged for this run" in inbound.modified_content
    assert f"Compile the deck under {own / 'out'}/" in inbound.modified_content
    assert (own / "materials" / "notes.md").read_text(encoding="utf-8") == "facts"
    assert f"Published a 3-slide deck.\nDeck: {deck}\nMEDIA: {deck}" in outbound.modified_content


def test_a_renamed_copy_of_the_published_deck_gets_the_preview_under_its_own_name(tmp_path: Path) -> None:
    """The PDF follows the deck the reply names, not the stem the publish wrote.

    A live run copied `out/deck.pptx` to a title of its own with `exec` and named the
    copy: the digest check let it through, but the preview sat beside the original as
    `deck.pdf`, so the copy went out with none and the web surface had nothing to show.
    """
    import shutil
    import time

    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(session_key="s1", inbound_content="build a deck")

    async def run() -> tuple:
        with workdir.bind(wd):
            await hook.before_user_inbound(ctx)
            own = Path(workdir.current())
            deck = _pptx(own / "out" / "deck.pptx", slides=3)
            _published(own, deck)
            (own / "out" / "deck.pdf").write_bytes(b"%PDF-1.4 rendered")
            time.sleep(0.01)
            copy = own / "out" / "The Title.pptx"
            shutil.copyfile(deck, copy)
            ctx.outbound_content = f"done\nMEDIA: {copy}"
            outbound = await hook.after_send(ctx)
        return outbound, copy

    outbound, copy = asyncio.run(run())
    preview = copy.with_suffix(".pdf")
    assert preview.read_bytes() == b"%PDF-1.4 rendered"
    assert f"Deck: {copy}\nMEDIA: {copy}" in outbound.modified_content
    assert f"Preview (the same deck as a PDF, for viewing): {preview}\nMEDIA: {preview}" in outbound.modified_content


def test_a_turn_that_skipped_the_inbound_phase_is_pointed_at_the_deck_on_its_first_iteration(tmp_path: Path) -> None:
    """A sub-agent's late result starts a turn the host runs no inbound hook for.

    On a live run that turn worked one level above the deck: three edit_file calls on
    a path that was not there, a ppt_build that found no brief, and a rebuild from
    nothing. The first iteration is where every turn passes, so that is where the
    repoint (and the bookkeeping after_send needs to announce a deck) happens now.
    """
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(session_key="s1", metadata={})
    ctx.iteration = 1

    async def run() -> tuple:
        with workdir.bind(wd):
            await hook.before_iteration(ctx)
            own = Path(workdir.current())
            deck = _pptx(own / "out" / "deck.pptx", slides=2)
            _published(own, deck)
            ctx.outbound_content = f"folded the research in\nMEDIA: {deck}"
            outbound = await hook.after_send(ctx)
        return own, deck, outbound

    own, deck, outbound = asyncio.run(run())
    assert own == wd / "decks" / "s1"
    assert f"Published a 2-slide deck.\nDeck: {deck}\nMEDIA: {deck}" in outbound.modified_content


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


@pytest.mark.asyncio
async def test_a_cancelled_turn_keeps_the_users_words_not_the_staging_block(tmp_path: Path) -> None:
    """The H1 broken-turn repair, pinned for this product too: the materials
    hook rewrites the model's view of the inbound, and a turn the user
    cancels must not persist that rewrite as the user's own words."""
    import asyncio

    from raven.agent import workdir
    from raven.agent.loop import AgentLoop
    from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
    from raven.spine import ChatType, Origin, Source, TurnRequest
    from raven_ppt.plugin.hook import PptEngineHook

    class _CancellingProvider:
        async def chat_with_retry(self, **kwargs):
            raise asyncio.CancelledError()

        def get_default_model(self):
            return "fake/model"

    loop = AgentLoop(
        provider=_CancellingProvider(),
        workspace=tmp_path,
        model="fake/model",
        policy=TurnPolicy(max_iterations=2),
        host=HostWiring(hooks=[PptEngineHook(home=None)]),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    async def _noop(**_kw) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop

    request = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
        text="build a deck about penguins",
    )
    with workdir.bind(tmp_path), pytest.raises(asyncio.CancelledError):
        await loop._process_message(request)

    session = loop.sessions.get_or_create("cli:c")
    users = [m for m in session.messages if m.get("role") == "user"]
    assert users and users[-1]["content"] == "build a deck about penguins"


def test_two_sessions_on_one_channel_directory_get_decks_of_their_own(tmp_path: Path) -> None:
    """The web gateway gives every session on a channel one directory, and the engine
    fences one deck per directory: measured, the third deck request in a channel built
    on the first's template with the second's sources. The hook repoints each turn to
    <workdir>/decks/<session>/ before staging, and the same session comes back to it."""
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    channel_dir = tmp_path / "tui"
    channel_dir.mkdir()

    async def turn(session_key: str) -> Path:
        with workdir.bind(channel_dir):
            await hook.before_user_inbound(AgentHookContext(session_key=session_key, inbound_content="make a deck"))
            return Path(workdir.current())

    first = asyncio.run(turn("acp:20260903_084606_58248d"))
    second = asyncio.run(turn("acp:20260903_084931_67fd0d"))
    again = asyncio.run(turn("acp:20260903_084606_58248d"))

    assert first == channel_dir / "decks" / "20260903_084606_58248d" and first.is_dir()
    assert second == channel_dir / "decks" / "20260903_084931_67fd0d" and second != first
    assert again == first, "a resumed session lands in the folder it started in"
    assert workdir.current() is None, "the bind's reset still clears the turn's repoint"


def test_the_per_session_deck_can_be_switched_off(tmp_path: Path) -> None:
    hook = plugin_module.make_hook(_ctx({**ENABLED, "deckPerSession": False}, tmp_path / "ws"))
    channel_dir = tmp_path / "tui"
    channel_dir.mkdir()

    async def turn() -> Path:
        with workdir.bind(channel_dir):
            await hook.before_user_inbound(AgentHookContext(session_key="acp:x", inbound_content="make a deck"))
            return Path(workdir.current())

    assert asyncio.run(turn()) == channel_dir


def test_a_deck_the_model_copied_into_out_is_not_announced_as_published(tmp_path: Path) -> None:
    """Two live runs answered a refused build with `cp deck/build/deck.pptx out/...` and told
    the user the deck was delivered; the hook confirmed it, knowing only "a valid deck under
    out/, newer than the turn". The publish step's record is what a deck has to be on."""
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(session_key="s1", inbound_content="make the deck")

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(ctx)
            own = Path(workdir.current())
            copied = _pptx(own / "out" / "成都夜间市集项目提案.pptx", slides=20)
            ctx.outbound_content = f"发布完成。\nMEDIA: {copied}"
            return await hook.after_send(ctx)

    decision = asyncio.run(run())
    assert "No deck was published this turn" in decision.modified_content
    assert (
        "成都夜间市集项目提案.pptx" in decision.modified_content
        and "not written by ppt_build" in decision.modified_content
    )
    assert "Published a" not in decision.modified_content


def test_a_reply_naming_a_copy_the_publish_step_never_wrote_is_sent_back_with_the_refusal(tmp_path: Path) -> None:
    """A live run answered a refused build with `cp` to a Chinese-titled copy under out/
    and a reply that the deck was delivered; the turn ended on it, the delegating agent
    had to adjudicate, and the run was started again to continue. The refusal is on
    disk, so the reply is sent back once with the reason in it -- to the author, who can
    act on it -- and only then to the unfinished nudges."""
    from types import SimpleNamespace

    from raven_ppt.contracts import Finding, Severity
    from raven_ppt.plugin.hook import UNFINISHED_NUDGE
    from raven_ppt.services.publish.deliver import record_refused

    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(session_key="s1", inbound_content="make the deck", metadata={})

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(ctx)
            own = Path(workdir.current())
            record_refused(
                SimpleNamespace(state_dir=own / "deck" / "state"),
                "not published: 1 blocking finding(s) on page(s) 3; fix them and build again",
                [Finding(kind="overflow", severity=Severity.BLOCKING, message="the body runs past the page", page=3)],
            )
            copied = _pptx(own / "out" / "成都夜间市集项目提案.pptx", slides=8)
            ctx.response = _reply(f"已发布：{copied}，还需要调整吗？")
            first = await hook.after_iteration(ctx)
            ctx.response = _reply(f"已发布：{copied}。")
            second = await hook.after_iteration(ctx)
            ctx.outbound_content = f"已发布：{copied}"
            sent = await hook.after_send(ctx)
        return copied, first, second, sent

    copied, first, second, sent = asyncio.run(run())
    assert first.rollback, "a copy named as the deliverable is sent back, question or not"
    told = first.rollback_inject[0]["content"]
    assert str(copied) in told and "did not publish that file" in told
    assert "The last build was refused: not published: 1 blocking finding(s) on page(s) 3" in told
    assert "page 3: overflow -- the body runs past the page" in told
    assert second.rollback and second.rollback_inject == [{"role": "user", "content": UNFINISHED_NUDGE}], (
        "once with the reason; after that the unfinished nudges take over"
    )
    assert "not written by ppt_build" in sent.modified_content
    assert "The last build was refused: not published: 1 blocking" in sent.modified_content


def test_the_announcement_carries_the_pdf_beside_the_deck(tmp_path: Path) -> None:
    """A .pptx is a download and nothing more on the web surface; the PDF the build wrote
    beside it is the same deck as pages, and the announcement hands both over."""
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(session_key="s1", inbound_content="make the deck")

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(ctx)
            own = Path(workdir.current())
            deck = _pptx(own / "out" / "deck.pptx", slides=3)
            _published(own, deck)
            deck.with_suffix(".pdf").write_bytes(b"%PDF-1.4")
            ctx.outbound_content = "done"
            return await hook.after_send(ctx), deck

    decision, deck = asyncio.run(run())
    assert f"Deck: {deck}\nMEDIA: {deck}" in decision.modified_content
    assert f"{deck.with_suffix('.pdf')}\nMEDIA: {deck.with_suffix('.pdf')}" in decision.modified_content


def _reply(text: str, tool_calls=()):
    from types import SimpleNamespace

    return SimpleNamespace(content=text, tool_calls=list(tool_calls))


def test_a_reply_that_ends_the_turn_without_a_deck_is_rolled_back_with_a_nudge(tmp_path: Path) -> None:
    """A live run answered its own ingest with "append needs real content -- re-ingest
    after..." and the loop, seeing no tool call, ended the turn with the deck unbuilt;
    the delegating agent had to spawn it again. The directory says the turn is not done,
    so the iteration is sent back -- twice at most, then the turn may end."""
    from raven_ppt.plugin.hook import UNFINISHED_NUDGE

    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(session_key="s1", inbound_content="make the deck", metadata={})

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(ctx)
            own = Path(workdir.current())
            (own / "deck" / "state").mkdir(parents=True)
            ctx.response = _reply("append 需要实际内容——补上 PDF 后半部分核实到的运营痛点与标准，再重新 ingest。")
            first = await hook.after_iteration(ctx)
            second = await hook.after_iteration(ctx)
            third = await hook.after_iteration(ctx)
            ctx.response = _reply("材料里没有上海案例的入住率数据，请提供来源或允许我标注为估计值？")
            question = await hook.after_iteration(ctx)
            ctx.response = _reply("正在构建", tool_calls=[{"name": "ppt_build"}])
            working = await hook.after_iteration(ctx)
        return first, second, third, question, working

    first, second, third, question, working = asyncio.run(run())
    assert first.rollback and first.rollback_inject == [{"role": "user", "content": UNFINISHED_NUDGE}]
    assert second.rollback
    assert not third.rollback and third.rollback_inject is None, "two nudges, then the turn may end"
    assert not question.rollback, "a question to the user is a legitimate end"
    assert not working.rollback, "an iteration with tool calls is not an ending"


def test_a_turn_that_published_its_deck_ends_once_its_reply_names_the_deck(tmp_path: Path) -> None:
    """On a live run the model's last act after publishing was a `cp` of the deck that the
    exec policy refused, and its reply was the refusal -- "would you like me to continue?"
    -- so the delegating agent was asked a question about a deck it was never told
    existed. The path is in the directory; the reply is sent back once to carry it."""
    from raven_ppt.plugin.hook import DELIVERED_NUDGE

    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(session_key="s1", inbound_content="make the deck", metadata={})

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(ctx)
            own = Path(workdir.current())
            deck = _pptx(own / "out" / "deck.pptx", slides=3)
            _published(own, deck)
            ctx.response = _reply("The operation was not completed. Would you like me to continue with the task?")
            unnamed = await hook.after_iteration(ctx)
            ctx.response = _reply(f"Delivered: {deck} -- 3 pages on the plan.")
            named = await hook.after_iteration(ctx)
            ctx.response = _reply("done.")
            again = await hook.after_iteration(ctx)
        return deck, unnamed, named, again

    deck, unnamed, named, again = asyncio.run(run())
    assert unnamed.rollback
    assert unnamed.rollback_inject == [{"role": "user", "content": DELIVERED_NUDGE.format(paths=str(deck))}]
    assert not named.rollback, "a reply that names the delivered deck is the end of the turn"
    assert not again.rollback, "one nudge per turn; after it the turn may end however it likes"


def test_a_turn_with_no_deck_in_progress_is_left_alone(tmp_path: Path) -> None:
    """A question answered in prose -- which templates exist, what the outline says --
    starts no deck, and ending it is not an unfinished deck."""
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(session_key="s1", inbound_content="which templates do you have?", metadata={})

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(ctx)
            ctx.response = _reply("Eight bundled templates: amber, beige, ...")
            return await hook.after_iteration(ctx)

    assert not asyncio.run(run()).rollback


def test_a_preview_from_an_earlier_deck_is_not_announced_as_this_one(tmp_path: Path) -> None:
    """`_pdf_beside` returns None when the build could not render a PDF, and the older
    file it leaves in place is a picture of a deck that no longer exists. Announced as
    "the same deck" it is worse than no preview, so the announcement takes only a
    preview at least as new as the deck it stands for."""
    import os
    import time

    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    ctx = AgentHookContext(session_key="s1", inbound_content="make the deck")

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(ctx)
            own = Path(workdir.current())
            stale = own / "out" / "deck.pdf"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_bytes(b"%PDF-1.4 an earlier deck")
            older = time.time() - 60
            os.utime(stale, (older, older))
            deck = _pptx(own / "out" / "deck.pptx", slides=3)
            _published(own, deck)
            ctx.outbound_content = "done"
            return await hook.after_send(ctx), deck, stale

    decision, deck, stale = asyncio.run(run())
    assert f"Deck: {deck}" in decision.modified_content
    assert str(stale) not in decision.modified_content
    assert "Preview" not in decision.modified_content
