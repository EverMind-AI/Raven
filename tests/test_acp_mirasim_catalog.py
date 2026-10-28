"""One Mirasim agent's menus, as the ACP shapes Raven reads off any agent."""

from __future__ import annotations

from typing import Any

from raven.acp.mirasim.catalog import (
    DEFAULT_ROUTE,
    IMAGE_AGENTS,
    OWN_KEY_GROUP,
    OWN_ROUTE,
    PLATFORM_GROUP,
    PLATFORM_ROUTE,
    PLATFORM_ROUTE_AGENTS,
    STEER_AGENTS,
    WIRE_ROUTES,
    agent_entry,
    installed_ids,
    read_catalog,
)
from raven.agent.subagent.mirasim_presets import MIRASIM_AGENTS
from tests.acp_schema import validate_def

RUNGS = [{"id": "low", "label": "Low"}, {"id": "high", "label": "High"}, {"id": "max", "label": "Max"}]
ROWS = [
    {"id": "claude", "label": "Claude Code", "kind": "cli", "installed": True, "version": "2.1.302"},
    {"id": "qwen", "label": "Qwen Code", "kind": "cli", "installed": True, "version": "0.25.2"},
    {"id": "opencode", "label": "OpenCode", "kind": "cli", "installed": False, "version": None},
]
"""Rows in the shape the host's ``listClis`` reports them (0.0.537)."""


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
    catalog = read_catalog("qwen", agent_entry(ROWS, "qwen"), catalog_doc())
    option = catalog.model_option(None)
    validate_def("SessionConfigOption", option)
    assert option["category"] == "model" and option["currentValue"] == "sonnet"
    groups = {group["group"]: [entry["value"] for entry in group["options"]] for group in option["options"]}
    # An unknown route is the agent's own: nothing says it bills anywhere else.
    assert groups == {"agent": ["sonnet"], PLATFORM_GROUP[0]: ["glm-5.3-flash"], OWN_KEY_GROUP[0]: ["profile:abc"]}
    assert option["options"][0]["name"] == "Qwen Code"


def test_an_agent_the_platform_runs_offers_each_of_its_own_models_on_both_routes() -> None:
    catalog = read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc())
    option = catalog.model_option(None)
    validate_def("SessionConfigOption", option)
    groups = {group["group"]: group["options"] for group in option["options"]}
    assert [entry["value"] for entry in groups["agent"]] == ["own:sonnet"]
    # The agent's own models lead the platform's list, ahead of the models only
    # the platform runs, the order the agent's own heading has them in.
    assert [entry["value"] for entry in groups[PLATFORM_GROUP[0]]] == ["platform:sonnet", "glm-5.3-flash"]
    assert [entry["value"] for entry in groups[OWN_KEY_GROUP[0]]] == ["profile:abc"]
    assert groups["agent"][0]["name"] == groups[PLATFORM_GROUP[0]][0]["name"] == "Sonnet"
    # With nothing chosen the session reports the model alone, which leaves the
    # route to Mirasim: that is the value Raven sends back to undo a choice.
    assert option["currentValue"] == "sonnet"
    assert catalog.model_option("platform:sonnet")["currentValue"] == "platform:sonnet"


def test_a_choice_names_the_model_mirasim_runs_and_the_route_it_runs_on() -> None:
    catalog = read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc())
    assert {
        choice: (catalog.mirasim_model(choice), catalog.route_for(choice))
        for choice in ("own:sonnet", "platform:sonnet", "sonnet", "glm-5.3-flash", "profile:abc", "opus-9")
    } == {
        "own:sonnet": ("sonnet", OWN_ROUTE),
        "platform:sonnet": ("sonnet", PLATFORM_ROUTE),
        "sonnet": ("sonnet", DEFAULT_ROUTE),
        "glm-5.3-flash": ("glm-5.3-flash", DEFAULT_ROUTE),
        "profile:abc": ("profile:abc", DEFAULT_ROUTE),
        "opus-9": ("opus-9", DEFAULT_ROUTE),
    }
    assert catalog.route_for(None) == DEFAULT_ROUTE
    assert all(catalog.offers_model(choice) for choice in ("own:sonnet", "platform:sonnet", "sonnet"))
    # Only the agent's own models take both routes: a platform model or a key has one.
    assert not catalog.offers_model("platform:glm-5.3-flash")
    assert not catalog.offers_model("own:profile:abc")


def test_an_agent_the_platform_does_not_run_is_left_on_mirasims_routes() -> None:
    catalog = read_catalog("qwen", agent_entry(ROWS, "qwen"), catalog_doc())
    assert catalog.route_for("sonnet") is None and catalog.route_for(None) is None
    assert catalog.mirasim_model("sonnet") == "sonnet"
    assert not catalog.offers_model("platform:sonnet") and not catalog.offers_model("own:sonnet")


def test_both_routes_of_a_model_take_its_rungs() -> None:
    catalog = read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc())
    assert catalog.rungs_for("own:sonnet") == catalog.rungs_for("platform:sonnet") == ("low", "high", "max")


def test_the_agents_the_platform_runs_are_four_of_the_presets() -> None:
    assert PLATFORM_ROUTE_AGENTS == {"claude", "codex", "kimi", "zcode"}
    assert PLATFORM_ROUTE_AGENTS <= {agent for agent, _, _ in MIRASIM_AGENTS.values()}


def test_the_platform_heading_reads_as_the_family_does() -> None:
    """The page calls this family "Mirasim" and each member "<agent>-Mirasim", so its menu does too."""
    option = read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc()).model_option(None)
    assert {group["group"]: group["name"] for group in option["options"]} == {
        "agent": "Claude Code",
        PLATFORM_GROUP[0]: "Mirasim",
        OWN_KEY_GROUP[0]: "Own key",
    }


def test_the_effort_rungs_are_a_thought_level_option_with_the_default_current() -> None:
    catalog = read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc())
    option = catalog.effort_option(None)
    assert option["id"] == "effort"
    assert option["category"] == "thought_level"
    assert option["currentValue"] == "high"
    assert [(choice["value"], choice["name"]) for choice in option["options"]] == [
        ("low", "Low"),
        ("high", "High"),
        ("max", "Max"),
    ]
    assert catalog.effort_option("max")["currentValue"] == "max"


def test_no_effort_option_where_mirasim_names_no_default_effort() -> None:
    """ZCode CLI lists rungs and no default: offering one as current would be a guess."""
    assert (
        read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc(defaultEffort=None)).effort_option(None) is None
    )
    assert read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc(effort=[])).effort_option(None) is None


def test_the_effort_menu_offers_only_the_rungs_its_model_takes() -> None:
    """A turn drops a rung its model lacks, so a menu offering one offers a
    choice that does nothing; the menu is read per model, as the turn is."""
    catalog = read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc())
    assert catalog.effort_option(None, "glm-5.3-flash") is None
    assert catalog.config_options("glm-5.3-flash", "max") == [catalog.model_option("glm-5.3-flash")]

    narrow = catalog.effort_option("max", "profile:abc")
    validate_def("SessionConfigOption", narrow)
    assert [(choice["value"], choice["name"]) for choice in narrow["options"]] == [("low", "Low"), ("high", "High")]
    assert narrow["currentValue"] == "high", "a rung the model lacks is not reported as current"

    unlisted = catalog.effort_option(None, "a-model-mirasim-added-later")
    assert [choice["value"] for choice in unlisted["options"]] == ["low", "high", "max"]
    no_default = read_catalog(
        "claude", agent_entry(ROWS, "claude"), catalog_doc(effortByModel={"profile:abc": RUNGS[:1]})
    )
    assert no_default.effort_option(None, "profile:abc") is None, "no rung of its own to mark as current"


def test_a_model_takes_its_own_rungs_and_an_unlisted_one_the_agents() -> None:
    catalog = read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc())
    assert catalog.rungs_for("profile:abc") == ("low", "high")
    assert catalog.rungs_for("glm-5.3-flash") == ()
    assert catalog.rungs_for(None) == ("low", "high", "max")
    assert catalog.rungs_for("a-model-mirasim-added-later") == ("low", "high", "max")


def test_the_default_model_is_mirasims_word_even_when_its_menu_does_not_list_it() -> None:
    catalog = read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc(defaultModel="opus"))
    assert catalog.default_model == "opus"
    assert not catalog.offers_model("opus")
    assert read_catalog("claude", agent_entry(ROWS, "claude"), catalog_doc(defaultModel=None)).default_model == "sonnet"


def test_a_missing_catalog_leaves_the_agent_with_no_menu() -> None:
    catalog = read_catalog("claude", agent_entry(ROWS, "claude"), None)
    assert catalog.installed and catalog.label == "Claude Code"
    assert catalog.config_options(None) == []
    assert catalog.effort_option(None) is None
    unknown = read_catalog("ghost", agent_entry(ROWS, "ghost"), None)
    assert not unknown.installed and unknown.label == "ghost"


def test_the_agent_list_is_read_off_the_hosts_rows() -> None:
    assert agent_entry(ROWS, "qwen") == ROWS[1]
    assert agent_entry(ROWS, "ghost") is None and agent_entry("not rows", "claude") is None
    assert installed_ids([*ROWS, {"id": 3, "installed": True}, "junk"]) == ["claude", "qwen"]
    assert installed_ids(None) == []


def test_each_model_keeps_its_context_window_for_the_usage_a_turn_reports() -> None:
    doc = catalog_doc(
        models=[{"id": "sonnet", "label": "Sonnet", "contextWindow": 200000}, {"id": "glm", "contextWindow": True}]
    )
    catalog = read_catalog("claude", agent_entry(ROWS, "claude"), doc)
    # The window is the model's whatever route the menu offers it on.
    assert catalog.context_window("sonnet") == 200000
    assert catalog.model("platform:sonnet").context_window == 200000
    assert catalog.context_window("glm") is None and catalog.context_window(None) is None


def test_routes_go_out_as_the_prompt_frame_spells_them() -> None:
    assert WIRE_ROUTES == {OWN_ROUTE: "local", PLATFORM_ROUTE: "cloud", DEFAULT_ROUTE: None}


def test_steering_and_images_are_offered_where_they_were_measured() -> None:
    assert STEER_AGENTS == {"claude", "codex", "dsh", "pi", "zcode"}
    assert IMAGE_AGENTS <= STEER_AGENTS | {"kimi", "antigravity", "qwen", "grok"}
    assert {agent for agent, _, _ in MIRASIM_AGENTS.values()} >= STEER_AGENTS | IMAGE_AGENTS
