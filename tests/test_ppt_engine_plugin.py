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
from raven.config.schema import MediaToolConfig  # noqa: E402
from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402
from raven.plugins.context import PluginContext, ServiceLocator  # noqa: E402
from raven_ppt import plugin as plugin_module  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ENGINE_HOME = REPO / "plugins-dist" / "ppt-engine"

ENABLED = {"enabled": True, "profile": "script_author"}


def _ctx(slice_: dict, workspace: Path, *, image: MediaToolConfig | None = None) -> PluginContext:
    """A host that configured an image tool, unless the test says otherwise."""
    image = MediaToolConfig(api_key="k", model="openai/gpt-image-2") if image is None else image
    return PluginContext(
        config=slice_,
        services=ServiceLocator(
            workspace=workspace, user_id="u", agent_id="a", media_config=lambda kind: image, media_proxy=None
        ),
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


def test_image_generation_is_contributed_and_offered_only_while_the_host_section_asks(tmp_path: Path) -> None:
    """The same withholding image_generate applies to itself, on the same live lane:
    the tool is contributed once and declares `configured()`, which follows the
    host's section both ways -- a section added after the prototypes were built
    offers it on the next assembly, an emptied one withdraws it -- instead of a
    decision frozen at the first prototype. A host that grants no media_config at
    all (an older ServiceLocator) is a tool that is never offered."""
    section: dict[str, MediaToolConfig | None] = {"now": MediaToolConfig()}
    ctx = PluginContext(
        config=dict(ENABLED),
        services=ServiceLocator(
            workspace=tmp_path, user_id="u", agent_id="a", media_config=lambda kind: section["now"], media_proxy=None
        ),
    )
    tool = plugin_module.make_ppt_generate_image(ctx)
    assert tool is not None and tool.configured() is False
    section["now"] = MediaToolConfig(api_key="k", model="openai/gpt-image-2")
    assert tool.configured() is True
    section["now"] = MediaToolConfig()
    assert tool.configured() is False

    bare = PluginContext(config=dict(ENABLED), services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a"))
    plugin_module._SHARED.clear()
    bare_tool = plugin_module.make_ppt_generate_image(bare)
    assert bare_tool is not None and bare_tool.configured() is False


def test_image_generation_follows_the_host_image_config(tmp_path: Path) -> None:
    ctx = _ctx(dict(ENABLED), tmp_path, image=MediaToolConfig(api_key="k", model="google/gemini-3.1-flash-image"))
    tool = plugin_module.make_ppt_generate_image(ctx)
    assert tool is not None
    assert "references" in tool.parameters["properties"]
    assert "16:9" in tool.parameters["properties"]["aspect_ratio"]["enum"]


def test_the_usage_recorder_bound_at_runtime_reaches_the_image_generator(tmp_path: Path) -> None:
    """The host mints RuntimeHandles after the factories ran; a recorder that arrives
    then must reach the generator built for the next turn, so the deck's image spend
    is recorded where image_generate records its own."""
    from raven.contracts.plugin_surface import RuntimeHandles

    async def recorder(snapshot) -> None:
        return None

    ctx = _ctx(dict(ENABLED), tmp_path)
    tool = plugin_module.make_ppt_generate_image(ctx)
    tool.bind_runtime(RuntimeHandles(usage_recorder=recorder))
    generator = tool._engine_for(tmp_path)["ppt_generate_image"]
    assert generator.media._usage_recorder is recorder


def test_image_search_reads_the_hosts_serper_key_through_the_web_grant(tmp_path: Path, monkeypatch) -> None:
    """A deployment that configured web_search once has configured the deck's image
    search too: the host's tools.web reaches the plugin through the locator, vendor
    slot first, legacy leaf after, and the slice key stays an override."""
    from raven.config.schema import WebToolsConfig

    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    web = WebToolsConfig.model_validate({"providers": {"serper": {"apiKey": "vendor-key"}}, "proxy": "http://p:1"})
    ctx = PluginContext(
        config=dict(ENABLED),
        services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a", web_config=lambda: web),
    )
    shared = plugin_module._Shared(ctx)
    assert shared.image_search_key() == "vendor-key"
    assert shared.web_proxy() == "http://p:1"
    assert plugin_module.make_ppt_image_search(ctx) is not None

    legacy = WebToolsConfig.model_validate({"search": {"apiKey": "legacy-key"}})
    shared = plugin_module._Shared(
        PluginContext(
            config=dict(ENABLED),
            services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a", web_config=lambda: legacy),
        )
    )
    assert shared.image_search_key() == "legacy-key"

    shared = plugin_module._Shared(
        PluginContext(
            config={**ENABLED, "imageSearch": {"apiKey": "slice-key"}, "webProxy": "http://own:2"},
            services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a", web_config=lambda: web),
        )
    )
    assert shared.image_search_key() == "slice-key" and shared.web_proxy() == "http://own:2"


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
        {"enabled": True, "image": "nope"},
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


def test_a_slice_image_section_overrides_the_hosts_grant(tmp_path: Path, monkeypatch) -> None:
    """The grant is the default; a slice naming its own ``image`` section is the
    override -- the seat for a host that grants nothing, or a deck kept on another
    account than the host's pictures. Read off the built tool: both values name
    what neither the grant nor any environment fallback would produce."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    slice_ = {
        **ENABLED,
        "image": {"apiKey": "sk-pictures", "model": "google/gemini-3.1-flash-image"},
    }
    tools = plugin_module._Shared(_ctx(slice_, tmp_path))._assemble(tmp_path)

    generator = tools["ppt_generate_image"]
    assert generator.api_key == "sk-pictures"
    assert generator.model == "google/gemini-3.1-flash-image"


def test_without_slice_or_grant_the_generator_has_no_key_and_the_shipped_default(tmp_path: Path, monkeypatch) -> None:
    """A host that grants nothing and a slice that says nothing: the generator
    stands on the shipped default id with no key, which is the shape configured()
    answers False on."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    tools = plugin_module._Shared(_ctx(dict(ENABLED), tmp_path, image=MediaToolConfig()))._assemble(tmp_path)

    generator = tools["ppt_generate_image"]
    assert generator.api_key == ""
    assert generator.model == "openai/gpt-image-2.5-sunburst"
    assert generator.configured() is False


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


def test_the_turn_after_a_delivery_is_not_pushed_into_another_build(tmp_path: Path) -> None:
    """A session does not end at the delivery. The turn where the user says the deck
    is fine used to arrive carrying "compile the deck under out/ and end your final
    reply with the MEDIA line", which is an instruction to publish again."""
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()

    async def run() -> tuple:
        with workdir.bind(wd):
            first = await hook.before_user_inbound(AgentHookContext(session_key="s1", inbound_content="make me a deck"))
            own = Path(workdir.current())
            deck = _pptx(own / "out" / "deck.pptx", slides=3)
            _published(own, deck)
            after = await hook.before_user_inbound(
                AgentHookContext(session_key="s1", inbound_content="looks good, thanks")
            )
        return first, after, own, deck

    first, after, own, deck = asyncio.run(run())
    assert f"Compile the deck under {own / 'out'}/" in first.modified_content
    assert f"Compile the deck under {own / 'out'}/" not in after.modified_content
    assert str(deck) in after.modified_content
    assert "stands as published" in after.modified_content


def test_a_source_staged_on_the_first_turn_does_not_hold_the_next_one_to_a_build(tmp_path: Path) -> None:
    """The ordinary deck: a source on turn one, "looks good, thanks" on turn two. The
    staging book is kept for the whole session, so any rule reading it to decide what
    this turn is answers the second turn with the first turn's material -- which is
    why nothing here decides that. The compile instruction goes when a deck stands,
    and the book's listing stays."""
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    source = tmp_path / "notes.md"
    source.write_text("the numbers\n", encoding="utf-8")

    async def run() -> tuple:
        with workdir.bind(wd):
            first = await hook.before_user_inbound(
                AgentHookContext(session_key="s1", inbound_content=f"make me a deck from {source}")
            )
            own = Path(workdir.current())
            deck = _pptx(own / "out" / "deck.pptx", slides=3)
            _published(own, deck)
            after = await hook.before_user_inbound(
                AgentHookContext(session_key="s1", inbound_content="looks good, thanks")
            )
        return first, after, own, deck

    first, after, own, deck = asyncio.run(run())
    assert "# Material staged for this run" in first.modified_content, "turn one staged the source"
    assert f"Compile the deck under {own / 'out'}/" in first.modified_content
    assert f"Compile the deck under {own / 'out'}/" not in after.modified_content
    assert "# Material staged for this run" in after.modified_content, "the book is the session's, not the turn's"
    assert str(deck) in after.modified_content and "stands as published" in after.modified_content


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


def test_the_sessions_tier_is_written_where_the_deck_tools_read_it(tmp_path: Path) -> None:
    """The mode overlay reaches hooks only; ppt_build reads the caps off the deck
    folder per call, so a tier switched mid-session takes effect on the next build."""
    from raven_ppt.services import tier

    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()

    async def run():
        with workdir.bind(wd):
            ctx = AgentHookContext(
                session_key="s1",
                iteration=1,
                metadata={"mode": "high", "mode_overlay": {"buildCap": 10, "readingCap": 3}},
            )
            await hook.before_iteration(ctx)
            capped = tier.read_caps(Path(workdir.current()))
            await hook.before_iteration(
                AgentHookContext(session_key="s1", iteration=1, metadata={"mode": "max", "mode_overlay": {}})
            )
            return capped, tier.read_caps(Path(workdir.current()))

    capped, uncapped = asyncio.run(run())
    assert capped == tier.Caps(mode="high", build_cap=10, reading_cap=3)
    assert uncapped == tier.Caps(mode="max")


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


def test_what_the_model_sees_is_jpeg_and_what_a_check_measures_stays_png(tmp_path: Path) -> None:
    """One page render, two readers, two formats.

    `data_uri` is the model's side and it is JPEG at the pinned quality: 23 template
    page renders were 4.32MB of base64 on the request that a gateway answered with an
    empty 200. The renders `pages_of` writes are the measurement's side and stay PNG,
    because `measure.contrast` opens them to decide ink against ground and JPEG's ring
    around a stroke is exactly the kind of pixel that reading answers wrongly.
    """
    import base64
    import io

    from PIL import Image

    from raven.utils.images import detect_image_mime
    from raven_ppt.stages._views import MODEL_IMAGE_QUALITY, DeckViews

    page = tmp_path / "page-001.png"
    noise = Image.new("RGB", (640, 360))
    noise.putdata([((x * 7) % 256, (y * 11) % 256, (x * y) % 256) for y in range(360) for x in range(640)])
    noise.save(page, format="PNG")

    class _Renderer:
        def to_pngs(self, pdf, out_dir, dpi, pages):
            return [page]

    views = DeckViews(renderer=_Renderer())
    rendered = asyncio.run(views.pages_of(tmp_path / "deck.pdf", tmp_path, [1]))
    assert detect_image_mime(rendered[1].read_bytes()) == "image/png", "a measurement reads PNG"

    uri = views.data_uri(page)
    head, _, payload = uri.partition(",")
    assert head == "data:image/jpeg;base64"
    sent = base64.b64decode(payload)
    assert detect_image_mime(sent) == "image/jpeg", "the label and the bytes agree"

    at_quality = io.BytesIO()
    Image.open(page).convert("RGB").save(
        at_quality, format="JPEG", quality=MODEL_IMAGE_QUALITY, subsampling=0, optimize=True, progressive=True
    )
    assert sent == at_quality.getvalue(), "the quality and the chroma sampling are the pinned ones"
    assert MODEL_IMAGE_QUALITY == 85


def test_a_picture_the_reader_sees_through_stays_png(tmp_path: Path) -> None:
    """A keyed illustration is judged on its transparency and JPEG has nowhere to put
    it, so alpha is the one thing that keeps a picture out of the JPEG path."""
    import base64

    from PIL import Image

    from raven.utils.images import detect_image_mime
    from raven_ppt.stages._views import DeckViews

    keyed = tmp_path / "figure.png"
    Image.new("RGBA", (120, 90), (255, 0, 0, 0)).save(keyed, format="PNG")
    opaque = tmp_path / "photo.png"
    Image.new("RGBA", (120, 90), (255, 0, 0, 255)).save(opaque, format="PNG")

    views = DeckViews()
    assert views.data_uri(keyed).startswith("data:image/png;base64,")
    assert detect_image_mime(base64.b64decode(views.data_uri(keyed).partition(",")[2])) == "image/png"
    assert views.data_uri(opaque).startswith("data:image/jpeg;base64,"), "an alpha channel that hides nothing is not it"


def test_a_page_too_heavy_for_its_budget_is_a_jpeg_before_it_is_downscaled(tmp_path: Path) -> None:
    """The budget is answered by the format first and the pixels only after.

    As a PNG a dark photographic page missed the 900KB ceiling by enough for two
    halvings, and the model was handed a 1920x1080 slide at 784x453 to read 10pt type
    off. Encoded first, the same page fits at full size.
    """
    import base64
    import io
    import random

    from PIL import Image, ImageFilter

    from raven.utils.images import image_pixel_size
    from raven_ppt.stages._views import MODEL_IMAGE_QUALITY, DeckViews

    rng = random.Random(7)
    heavy = Image.new("RGB", (1400, 800))
    heavy.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(1400 * 800)])
    heavy = heavy.filter(ImageFilter.GaussianBlur(2))
    page = tmp_path / "page-002.png"
    heavy.save(page, format="PNG")

    as_png = io.BytesIO()
    heavy.save(as_png, format="PNG", optimize=True)
    as_jpeg = io.BytesIO()
    heavy.save(as_jpeg, format="JPEG", quality=MODEL_IMAGE_QUALITY, subsampling=0, optimize=True, progressive=True)
    png_wire = len(base64.b64encode(as_png.getvalue()))
    jpeg_wire = len(base64.b64encode(as_jpeg.getvalue()))
    assert jpeg_wire < png_wire
    budget = (png_wire + jpeg_wire) // 2

    views = DeckViews()
    sent = base64.b64decode(views.data_uri(page, budget=budget).partition(",")[2])
    assert len(base64.b64encode(sent)) <= budget
    assert image_pixel_size(sent) == (1400, 800), "the format paid for the budget, not the pixels"


def test_a_sheet_goes_out_whole_where_a_page_is_clipped_to_the_token_cap(tmp_path: Path) -> None:
    """Two pictures, two caps, and the reason they cannot be one number.

    `_IMAGE_TOKEN_CAP` is 1568 because past it the patch count is clamped, which is
    right for a picture whose subject fills the frame. A sheet's subject is one cell, a
    twentieth of the frame, so the same cap takes a 768px cell to 392px: measured on the
    model this engine runs, a sheet clipped that way names the right page 82% of the
    time against 90% un-clipped and 90% for twenty separate renders.

    The page door is the one `ppt_review` reads a page through, and it keeps the cap.
    """
    import base64

    from PIL import Image

    from raven.utils.images import _IMAGE_TOKEN_CAP, image_pixel_size
    from raven_ppt.stages._views import CONTACT_SHEET_BYTES, CONTACT_SHEET_PIXELS, DeckViews

    wide = tmp_path / "sheet.png"
    Image.new("RGB", (3112, 1764), (245, 245, 247)).save(wide)
    views = DeckViews()

    def decode(uri: str) -> bytes:
        return base64.b64decode(uri.partition(",")[2])

    as_sheet = views.sheet_uri(wide)
    assert as_sheet.startswith("data:image/jpeg;base64,"), "a sheet rides the same JPEG door a page does"
    assert image_pixel_size(decode(as_sheet)) == (3112, 1764), "the sheet keeps the pixels its cells need"
    assert max(image_pixel_size(decode(views.data_uri(wide)))) == _IMAGE_TOKEN_CAP, "a page is still clipped"
    assert CONTACT_SHEET_PIXELS >= 3112 and CONTACT_SHEET_BYTES == 6_000_000


def test_the_second_reader_still_gets_one_page_at_a_time(tmp_path: Path) -> None:
    """The path the sheet must not touch.

    `ppt_review` asks a fresh reader what is wrong with one page, and a void is a fact
    about that page: it sends one render per call, labelled, at the page cap. Choosing
    a page off a sheet and reading a page closely are different jobs, and only the first
    one got cheaper.
    """
    import inspect

    from raven_ppt.tools import review

    source = inspect.getsource(review.PptReviewTool)

    assert source.count('self.views.data_uri(renders[number], label=f"page {number}")') == 2
    assert "sheet_uri" not in source and "contact_sheet" not in source


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


def test_a_turn_that_began_with_a_standing_deck_is_not_sent_back_to_publish_it_again(tmp_path: Path) -> None:
    """The other side of D38. The inbound phase tells this turn its deck stands and
    gives it no compile instruction, but the guard knew only whether this turn had
    published: the plain answer to "looks good, thanks" was rolled back twice with
    "continue: build ... and publish", and only the two-nudge cap let the third reply
    end. A turn that began with a deck on the record and built none of its own has
    nothing it failed to publish. A build this turn wrote that the record does not
    hold is still unfinished, standing deck or not."""
    from raven_ppt.plugin.hook import UNFINISHED_NUDGE

    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(AgentHookContext(session_key="s1", inbound_content="make me a deck"))
            own = Path(workdir.current())
            deck = _pptx(own / "out" / "deck.pptx", slides=3)
            _published(own, deck)

            settled = AgentHookContext(session_key="s1", inbound_content="looks good, thanks", metadata={})
            inbound = await hook.before_user_inbound(settled)
            settled.response = _reply("Glad it works -- that is the deck you already have.")
            replies = [await hook.after_iteration(settled) for _ in range(3)]

            revising = AgentHookContext(session_key="s1", inbound_content="add a page on pricing", metadata={})
            await hook.before_user_inbound(revising)
            _pptx(own / "out" / "deck-v2.pptx", slides=4)
            revising.response = _reply("I have put the new page together; the numbers still need a source.")
            unfinished = await hook.after_iteration(revising)
        return inbound, replies, unfinished

    inbound, replies, unfinished = asyncio.run(run())
    assert "stands as published" in inbound.modified_content
    assert "Compile the deck under" not in inbound.modified_content
    assert not any(reply.rollback for reply in replies), "the settled turn is not sent back at all"
    assert not any(reply.rollback_inject for reply in replies)
    assert unfinished.rollback and unfinished.rollback_inject == [{"role": "user", "content": UNFINISHED_NUDGE}], (
        "a build this turn wrote that the publish record does not hold is still unfinished"
    )


def _refused(own: Path, note: str = "not published: 1 blocking finding(s) on page 3") -> None:
    """Record a refusal the way `record_refused` does."""
    import json

    state = own / "deck" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "refused.json").write_text(
        json.dumps(
            {"note": note, "blocking": [{"page": 3, "kind": "overflow", "message": "the body runs past the frame"}]}
        ),
        encoding="utf-8",
    )


def test_a_refused_build_is_not_a_turn_that_built_none(tmp_path: Path) -> None:
    """The shape a real refused build leaves, which reading out/ could not see.

    `BuildStage.run` returns on blocking findings before `stage()` and `publish()`,
    so the candidate stays under `deck/build/` and out/ is untouched. With a deck
    already on the record, the standing-deck exemption then read "this turn built
    none" off an empty out/ and let the reply end -- the premature ending the
    unfinished nudge exists to stop, on an ordinary revision turn. The earlier
    control hand-wrote a deck under out/, which is not this filesystem shape.
    """
    from raven_ppt.plugin.hook import UNFINISHED_NUDGE

    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(AgentHookContext(session_key="s1", inbound_content="make me a deck"))
            own = Path(workdir.current())
            _published(own, _pptx(own / "out" / "deck.pptx", slides=3))

            revising = AgentHookContext(session_key="s1", inbound_content="add a page on pricing", metadata={})
            await hook.before_user_inbound(revising)
            # What a refused build leaves: a candidate under deck/build and a refusal
            # on the record. Nothing under out/, because publish was never reached.
            _pptx(own / "deck" / "build" / "deck.pptx", slides=4)
            _refused(own)
            revising.response = _reply("The pricing page is updated.")
            return await hook.after_iteration(revising)

    decision = asyncio.run(run())
    assert decision.rollback, "a turn whose build was refused has not finished"
    assert decision.rollback_inject == [{"role": "user", "content": UNFINISHED_NUDGE}]


def test_a_draft_build_is_not_a_turn_that_built_none(tmp_path: Path) -> None:
    """The second path the premise was wrong on. A draft returns `ok=True` with
    "draft: not published" and never publishes, so it too leaves out/ as it found
    it -- and it leaves no refusal behind either, so a remedy that only looked for
    a refused build would let this one through."""
    from raven_ppt.plugin.hook import UNFINISHED_NUDGE

    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(AgentHookContext(session_key="s1", inbound_content="make me a deck"))
            own = Path(workdir.current())
            _published(own, _pptx(own / "out" / "deck.pptx", slides=3))

            revising = AgentHookContext(session_key="s1", inbound_content="redo the cover", metadata={})
            await hook.before_user_inbound(revising)
            _pptx(own / "deck" / "build" / "deck.pptx", slides=3)
            revising.response = _reply("Here is how the cover reads now.")
            return await hook.after_iteration(revising)

    decision = asyncio.run(run())
    assert decision.rollback, "a draft is work in progress, not a finished turn"
    assert decision.rollback_inject == [{"role": "user", "content": UNFINISHED_NUDGE}]


def test_a_build_whose_script_failed_is_not_a_turn_that_built_none(tmp_path: Path) -> None:
    """The third path: the script crashed, so there is no candidate and no refusal
    -- the runner keeps the failed script under review/build_failures instead. A
    turn that ran a build and got nothing is the clearest case of unfinished."""
    from raven_ppt.plugin.hook import UNFINISHED_NUDGE

    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(AgentHookContext(session_key="s1", inbound_content="make me a deck"))
            own = Path(workdir.current())
            _published(own, _pptx(own / "out" / "deck.pptx", slides=3))

            revising = AgentHookContext(session_key="s1", inbound_content="fix the chart", metadata={})
            await hook.before_user_inbound(revising)
            kept = own / "deck" / "review" / "build_failures" / "failure-001"
            kept.mkdir(parents=True)
            (kept / "stderr.txt").write_text("Traceback: the chart helper raised", encoding="utf-8")
            revising.response = _reply("The chart should be right now.")
            return await hook.after_iteration(revising)

    decision = asyncio.run(run())
    assert decision.rollback, "a build that produced nothing has not finished the turn"
    assert decision.rollback_inject == [{"role": "user", "content": UNFINISHED_NUDGE}]


def test_the_author_with_nothing_left_to_do_still_ends_the_turn(tmp_path: Path) -> None:
    """The exemption's own case, which the fix must not take away: a turn that
    began with a deck on the record and ran no build at all ends where it stops.
    Held here beside the three unfinished shapes so the pair is read together --
    the guard is about not ending a turn that still has work in it, and a turn
    with no work in it is still allowed to end.
    """
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(AgentHookContext(session_key="s1", inbound_content="make me a deck"))
            own = Path(workdir.current())
            _published(own, _pptx(own / "out" / "deck.pptx", slides=3))
            # A build directory that exists and does not move: an earlier turn built
            # here, so the marks are non-empty and still equal at both ends.
            _pptx(own / "deck" / "build" / "deck.pptx", slides=3)

            settled = AgentHookContext(session_key="s1", inbound_content="looks good, thanks", metadata={})
            await hook.before_user_inbound(settled)
            settled.response = _reply("Glad it works -- that is the deck you already have.")
            return [await hook.after_iteration(settled) for _ in range(3)]

    replies = asyncio.run(run())
    assert not any(reply.rollback for reply in replies), "a settled turn is not sent back at all"
    assert not any(reply.rollback_inject for reply in replies)


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


# -- the destination the user named ------------------------------------------


def _delivered_record(own: Path, deck: Path, delivered: Path) -> None:
    """Record `deck` under out/ and `delivered` as the copy the publish step wrote there."""
    import hashlib
    import json

    state = own / "deck" / "state"
    state.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(deck.read_bytes()).hexdigest()
    (state / "published.json").write_text(
        json.dumps(
            {
                "published": [
                    {"path": str(deck), "sha256": digest, "pages": 3},
                    {"path": str(delivered), "sha256": digest, "pages": 3, "role": "delivery"},
                ]
            }
        ),
        encoding="utf-8",
    )


def test_the_delivered_path_is_announced_with_its_preview(tmp_path: Path) -> None:
    """The user asked for the file at a path of their own; the reply that names it is
    confirmed rather than contradicted, and its PDF rides along."""
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    ctx = AgentHookContext(session_key="s1", inbound_content="make the deck, put it in handoff/", metadata={})

    async def run():
        with workdir.bind(wd):
            await hook.before_user_inbound(ctx)
            own = Path(workdir.current())
            deck = _pptx(own / "out" / "deck.pptx", slides=3)
            delivered = handoff / "ravenx-intro.pptx"
            delivered.write_bytes(deck.read_bytes())
            delivered.with_suffix(".pdf").write_bytes(b"%PDF-1.4")
            _delivered_record(own, deck, delivered)
            ctx.response = _reply(f"The deck is at {delivered} and has 3 slides.")
            iteration = await hook.after_iteration(ctx)
            ctx.outbound_content = f"The deck is at {delivered} and has 3 slides.\nMEDIA: {delivered}"
            return await hook.after_send(ctx), delivered, iteration

    decision, delivered, iteration = asyncio.run(run())
    assert not iteration.rollback, "a reply naming the delivered path is a finished turn"
    assert f"Published a 3-slide deck.\nDeck: {delivered}\nMEDIA: {delivered}" in decision.modified_content
    assert f"MEDIA: {delivered.with_suffix('.pdf')}" in decision.modified_content
    assert "No deck was published" not in decision.modified_content


def test_an_earlier_turns_delivery_is_not_announced_again(tmp_path: Path) -> None:
    hook = plugin_module.make_hook(_ctx(dict(ENABLED), tmp_path / "ws"))
    wd = tmp_path / "session"
    wd.mkdir()
    handoff = tmp_path / "handoff"
    handoff.mkdir()

    async def run():
        with workdir.bind(wd):
            first = AgentHookContext(session_key="s1", inbound_content="make the deck", metadata={})
            await hook.before_user_inbound(first)
            own = Path(workdir.current())
            deck = _pptx(own / "out" / "deck.pptx", slides=3)
            delivered = handoff / "ravenx-intro.pptx"
            delivered.write_bytes(deck.read_bytes())
            _delivered_record(own, deck, delivered)
        with workdir.bind(wd):
            later = AgentHookContext(session_key="s1", inbound_content="what did you deliver?", metadata={})
            await hook.before_user_inbound(later)
            later.outbound_content = f"Last time: {delivered}\nMEDIA: {delivered}"
            return await hook.after_send(later)

    decision = asyncio.run(run())
    assert "Published a" not in (decision.modified_content or "")
