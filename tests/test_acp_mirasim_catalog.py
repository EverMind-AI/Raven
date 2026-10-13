"""One mirasim agent's menus, as the ACP shapes Raven reads off any agent."""

from __future__ import annotations

from typing import Any

from raven.acp.mirasim.catalog import OWN_KEY_GROUP, PLATFORM_GROUP, read_catalog
from tests.acp_schema import validate_def

RUNGS = [{"id": "low", "label": "Low"}, {"id": "high", "label": "High"}, {"id": "max", "label": "Max"}]
AGENTS = {"agents": [{"id": "claude", "label": "Claude Code", "installed": True}]}


def catalog_doc(**over: Any) -> dict[str, Any]:
    doc = {
        "defaultModel": "sonnet",
        "defaultEffort": "high",
        "models": [
            {"id": "sonnet", "label": "Sonnet"},
            {"id": "glm-5.3-flash", "label": "GLM-5.3 Flash"},
            {"id": "profile:abc", "label": "My key"},
            {"id": "sonnet", "label": "Sonnet, listed twice"},
            {"label": "no id"},
            "not a row",
        ],
        "routeByModel": {"glm-5.3-flash": "relay", "profile:abc": "direct", "sonnet": "elsewhere"},
        "effort": RUNGS,
        "effortByModel": {"sonnet": RUNGS, "glm-5.3-flash": [], "profile:abc": RUNGS[:2]},
    }
    return {**doc, **over}


def test_the_menu_groups_each_model_by_who_pays_for_it() -> None:
    catalog = read_catalog("claude", AGENTS, catalog_doc())
    option = catalog.model_option(None)
    validate_def("SessionConfigOption", option)
    assert option["category"] == "model" and option["currentValue"] == "sonnet"
    groups = {group["group"]: [entry["value"] for entry in group["options"]] for group in option["options"]}
    # An unknown route is the agent's own: nothing says it bills anywhere else.
    assert groups == {"agent": ["sonnet"], PLATFORM_GROUP[0]: ["glm-5.3-flash"], OWN_KEY_GROUP[0]: ["profile:abc"]}
    assert option["options"][0]["name"] == "Claude Code"


def test_the_effort_rungs_are_session_modes_with_the_default_current() -> None:
    catalog = read_catalog("claude", AGENTS, catalog_doc())
    modes = catalog.modes(None)
    validate_def("SessionModeState", modes)
    assert modes["currentModeId"] == "high"
    assert [mode["id"] for mode in modes["availableModes"]] == ["low", "high", "max"]
    assert catalog.modes("max")["currentModeId"] == "max"


def test_no_mode_is_current_where_mirasim_names_no_default_effort() -> None:
    """ZCode CLI lists rungs and no default: offering one as current would be a guess."""
    assert read_catalog("claude", AGENTS, catalog_doc(defaultEffort=None)).modes(None) is None
    assert read_catalog("claude", AGENTS, catalog_doc(effort=[])).modes(None) is None


def test_a_model_takes_its_own_rungs_and_an_unlisted_one_the_agents() -> None:
    catalog = read_catalog("claude", AGENTS, catalog_doc())
    assert catalog.rungs_for("profile:abc") == ("low", "high")
    assert catalog.rungs_for("glm-5.3-flash") == ()
    assert catalog.rungs_for(None) == ("low", "high", "max")
    assert catalog.rungs_for("a-model-mirasim-added-later") == ("low", "high", "max")


def test_the_default_model_is_mirasims_word_even_when_its_menu_does_not_list_it() -> None:
    catalog = read_catalog("claude", AGENTS, catalog_doc(defaultModel="opus"))
    assert catalog.default_model == "opus"
    assert not catalog.offers_model("opus")
    assert read_catalog("claude", AGENTS, catalog_doc(defaultModel=None)).default_model == "sonnet"


def test_a_missing_catalog_leaves_the_agent_with_no_menu() -> None:
    catalog = read_catalog("claude", AGENTS, None)
    assert catalog.installed and catalog.label == "Claude Code"
    assert catalog.config_options(None) == []
    assert catalog.modes(None) is None
    unknown = read_catalog("ghost", AGENTS, None)
    assert not unknown.installed and unknown.label == "ghost"
