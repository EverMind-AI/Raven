"""Whose credentials the routing gate's readiness probe reads.

``_routed_target_ready`` answers for the lane a route points at, not for the
host loop that asks. The two are separately configured: a product is rendered
from its own folder first (``agents/<product>/.env``) and inherits the host's
sections only where the folder is silent, so a probe wired to the host's own
readers is wrong in both directions -- it refuses a lane equipped by its own
file on a host that holds nothing, and it opens a lane whose pipeline has no
key for the surface it actually calls.

The declaration side of the same gate -- which routes are probed at all -- is
in ``test_subagent_routing_backend.py``; this file is only about what the probe
measures once a route has asked for it.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.subagent import vendored_agents as va
from raven.config.schema import MediaGenConfig
from raven.providers.base import LLMProvider, LLMResponse
from tests._wiring import wire

LANE = "Raven-Deck"
"""The routed product under test. Spelled unlike its folder on purpose: the
probe looks the folder up by row name, and a prefix derived from the row would
read settings no ``.env`` carries."""

FOLDER = "raven-deck"

NEEDS = ("image_generation", "image_search")
"""What the shipped deck route declares."""


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return LLMResponse(content="stub", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both readers fall back to a shared environment variable, so a developer
    who exports one would see every refusal here pass for the wrong reason."""
    for name in ("SERPER_API_KEY", "OPENROUTER_API_KEY", "TAVILY_API_KEY", "DECK_SERPER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The routed product's folder, with a writer for the ``.env`` its launcher
    reads. Returns the writer so a case states only what the lane holds."""
    root = tmp_path / "agents"
    folder = root / FOLDER
    folder.mkdir(parents=True)
    (folder / "subagent.json").write_text(
        json.dumps({"name": LANE, "kind": "acp", "description": "builds a deck", "command": "x"}),
        encoding="utf-8",
    )
    (folder / "run.py").write_text("", encoding="utf-8")
    # The shipped shape: the folder's own provider block names the gateway its
    # launcher's key pays, which is what decides whether that key can also draw.
    (folder / "config.json").write_text(
        json.dumps({"providers": {"deck": {"apiBase": "https://openrouter.ai/api/v1"}}}), encoding="utf-8"
    )
    monkeypatch.setattr(va, "agents_root", lambda: root)

    def equip(**settings: str) -> None:
        lines = [f"{va.env_prefix(FOLDER)}_{name}={value}" for name, value in settings.items()]
        (folder / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return equip


def _host(**kw) -> AgentLoop:
    """A loop holding only what a case names, and a live config file that has
    nothing in it -- the host's own credentials are the variable under test."""
    workspace = Path(tempfile.mkdtemp())
    loop = AgentLoop(provider=_StubProvider(), workspace=workspace, model="stub", **wire(**kw))
    from raven.config.live import LiveConfig

    loop._live_config = LiveConfig(workspace / "absent-config.json")
    return loop


def _bare_host() -> AgentLoop:
    return _host()


def _serper_host() -> AgentLoop:
    return _host(
        search_api_key="sk-host-serper",
        media_config=MediaGenConfig.model_validate({"image": {"apiKey": "sk-host-image"}}),
    )


def test_a_lane_equipped_by_its_own_env_is_not_refused_by_a_bare_host(lane) -> None:
    """The first direction the host-side readers had backwards. This deployment
    holds no search key and no image key at all, and the lane is fully equipped
    through the two settings its own launcher resolves -- so there is nothing
    here for the route to be closed over."""
    lane(SERPER_API_KEY="sk-lane-serper", IMAGE_API_KEY="sk-lane-image")

    assert _bare_host()._routed_target_ready(LANE, NEEDS) is True


def test_a_host_on_another_vendor_does_not_open_a_lane_that_needs_serper(lane) -> None:
    """The inverse direction. The host's ``web_search`` is keyed and working, on
    Tavily; the lane's image search speaks Serper's image endpoint alone, and no
    Serper key exists anywhere -- so this route must stay closed even though the
    host would call itself search-capable."""
    lane(IMAGE_API_KEY="sk-lane-image")
    host = _host(
        web_search_provider="tavily",
        web_provider_keys={"tavily": "tv-host"},
        media_config=MediaGenConfig.model_validate({"image": {"apiKey": "sk-host-image"}}),
    )

    assert host._live_web_search_key(), "the host's own search must be keyed, or this proves nothing"
    assert host._routed_target_ready(LANE, ("image_search",)) is False


def test_a_serper_key_the_host_did_not_select_is_still_the_lanes_to_spend(lane) -> None:
    """The other half of reading the vendor rather than the selection: a host
    running Tavily may still hold a Serper key, and that key is exactly what the
    lane's image search spends."""
    lane()
    host = _host(
        web_search_provider="tavily",
        search_api_key="sk-host-serper",
        media_config=MediaGenConfig.model_validate({"image": {"apiKey": "sk-host-image"}}),
    )

    assert host._routed_target_ready(LANE, ("image_search",)) is True


def test_the_host_still_lends_what_the_lane_does_not_carry(lane) -> None:
    """The fallback is the inheritance the launchers perform: a folder naming no
    key of its own is rendered from the host's sections. A probe reading the
    folder alone would refuse every lane installed without an ``.env``."""
    lane()

    assert _serper_host()._routed_target_ready(LANE, NEEDS) is True


def test_a_bare_host_and_a_bare_lane_close_the_route(lane) -> None:
    """Neither side holds anything: the case the gate exists for."""
    lane()

    assert _bare_host()._routed_target_ready(LANE, NEEDS) is False


@pytest.mark.parametrize(
    ("declared", "equips", "expected"),
    [
        (("image_generation",), {"IMAGE_API_KEY": "sk-lane-image"}, True),
        (("image_generation",), {"SERPER_API_KEY": "sk-lane-serper"}, False),
        (("image_search",), {"SERPER_API_KEY": "sk-lane-serper"}, True),
        (("image_search",), {"IMAGE_API_KEY": "sk-lane-image"}, False),
    ],
    ids=["generation met", "generation missing", "search met", "search missing"],
)
def test_each_declared_requirement_is_answered_on_its_own(lane, declared, equips, expected) -> None:
    """One requirement is one question. A probe that answered both whenever
    either was asked would pass every case about the shipped route, which
    declares the pair."""
    lane(**equips)

    assert _bare_host()._routed_target_ready(LANE, declared) is expected


def test_a_requirement_this_raven_cannot_measure_is_treated_as_met(lane) -> None:
    """A manifest written for a later raven must not silently lose its route on
    an older one: the unknown name is warned about and the route stays open."""
    lane()

    assert _bare_host()._routed_target_ready(LANE, ("a_surface_from_the_future",)) is True


def test_a_route_declaring_nothing_is_open(lane) -> None:
    """The reader's half of the declaration rule. The backend does not call the
    probe for an undeclared route at all, and if it ever did, the answer must
    still be the pre-gate behaviour."""
    lane()

    assert _bare_host()._routed_target_ready(LANE, ()) is True


def test_a_target_with_no_folder_falls_back_to_the_host(lane) -> None:
    """A route to a row that is not a discovered product -- a hand-registered
    acp peer -- has no ``.env`` to read, so the host's own credentials are the
    only answer available and the gate must not read "no folder" as "no key"."""
    assert _serper_host()._routed_target_ready("Nobody", NEEDS) is True
    assert _bare_host()._routed_target_ready("Nobody", NEEDS) is False


def test_an_exported_key_the_lane_would_search_with_is_not_a_refusal(lane, monkeypatch: pytest.MonkeyPatch) -> None:
    """The last link of the lane's own chain. ``ppt_image_search`` resolves its
    key from its config *or* ``SERPER_API_KEY``, and a launched product inherits
    this environment -- so a deployment whose only key is exported holds one the
    lane would have searched with."""
    lane(IMAGE_API_KEY="sk-lane-image")
    monkeypatch.setenv("SERPER_API_KEY", "sk-exported")

    assert _bare_host()._routed_target_ready(LANE, NEEDS) is True


class TestTheKeyThatPaysForTheWordsPaysForThePictures:
    """The launcher's last image branch, which an explicit image key hides.

    ``configure_image_generation`` renders the lane's own LLM key into
    ``tools.media.image.apiKey`` when no image key exists anywhere and the
    endpoint is OpenRouter, the one gateway the generator speaks. A probe that
    looked only for an explicit image key refused a lane the launcher would
    have handed a working generator -- and it must stay a refusal towards any
    other address, because lending a chat credential to a gateway its owner
    never nominated for pictures is what that backfill exists to avoid.
    """

    def test_a_lane_holding_only_its_own_llm_key_can_still_draw(self, lane) -> None:
        """The reported case: a folder with its LLM key, its OpenRouter base and
        a search key, on a host holding nothing."""
        lane(API_KEY="sk-own", API_BASE="https://openrouter.ai/api/v1", SERPER_API_KEY="sk-lane-serper")

        assert _bare_host()._routed_target_ready(LANE, NEEDS) is True

    def test_the_gateway_is_read_off_the_folder_when_no_base_is_pinned(self, lane) -> None:
        """No ``API_BASE`` of its own: the address comes from the folder's own
        provider block, the way the launcher resolves it on the rendered config."""
        lane(API_KEY="sk-own", SERPER_API_KEY="sk-lane-serper")

        assert _bare_host()._routed_target_ready(LANE, NEEDS) is True

    def test_another_gateway_gets_nothing_lent_to_it(self, lane) -> None:
        lane(API_KEY="sk-own", API_BASE="https://gateway.example/v1", SERPER_API_KEY="sk-lane-serper")

        assert _bare_host()._routed_target_ready(LANE, ("image_generation",)) is False

    def test_an_image_base_pinned_elsewhere_declines_the_loan_too(self, lane) -> None:
        """Both endpoints are read. The words may be bought on OpenRouter while
        the deck pins its pictures to another account."""
        lane(API_KEY="sk-own", API_BASE="https://openrouter.ai/api/v1", IMAGE_API_BASE="https://pics.example/v1")

        assert _bare_host()._routed_target_ready(LANE, ("image_generation",)) is False

    def test_an_explicit_image_key_wins_wherever_the_words_are_bought(self, lane) -> None:
        lane(API_KEY="sk-own", API_BASE="https://gateway.example/v1", IMAGE_API_KEY="sk-lane-image")

        assert _bare_host()._routed_target_ready(LANE, ("image_generation",)) is True

    def test_a_folder_with_no_llm_key_of_its_own_still_falls_back_to_the_host(self, lane) -> None:
        """The inherit branch has no key to lend, so the host's image section is
        the only answer -- which is exactly what it inherits."""
        lane(SERPER_API_KEY="sk-lane-serper")

        assert _bare_host()._routed_target_ready(LANE, ("image_generation",)) is False
        assert _serper_host()._routed_target_ready(LANE, ("image_generation",)) is True
