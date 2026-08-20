"""Unit tests for MCP tool-name construction (sanitisation, length cap, collisions)."""

from __future__ import annotations

from raven.mcp.naming import (
    MAX_NAME_LENGTH,
    _assemble,
    legacy_tool_name,
    spellings,
    tool_name,
)


class TestBasicShape:
    def test_joins_server_and_tool_under_the_mcp_prefix(self):
        assert tool_name("openseo", "keyword_research") == "mcp_openseo_keyword_research"

    def test_a_clean_pair_has_only_one_spelling(self):
        # Nothing to sanitise and nothing to cap, so the compatibility path is
        # a no-op -- the measure of how narrow the renaming window really is.
        assert legacy_tool_name("openseo", "search") == tool_name("openseo", "search")


class TestSanitisation:
    def test_a_dash_is_left_alone(self):
        # Every provider accepts it: Anthropic's documented tool-name regex is
        # ^[a-zA-Z0-9_-]{1,64}$, OpenAI's docstring says "underscores and
        # dashes", and litellm's sanitiser excludes [^a-zA-Z0-9_-]. Replacing
        # it would rename a tool that already works.
        assert tool_name("bcp-search", "search") == "mcp_bcp-search_search"

    def test_a_dot_becomes_an_underscore(self):
        assert tool_name("db", "query.data") == "mcp_db_query_data"

    def test_a_slash_becomes_an_underscore(self):
        assert tool_name("gh", "actions/download-logs") == "mcp_gh_actions_download-logs"

    def test_non_ascii_becomes_underscores(self):
        # One underscore per replaced character: the two server characters and
        # the two tool characters, plus the two separators.
        assert tool_name("热点", "查询") == "mcp" + "_" + "__" + "_" + "__"

    def test_the_legacy_form_is_not_sanitised(self):
        # The shipped name was built from the raw pair. A blacklist entry
        # copied from a live deploy carries the slash, so cleaning it here
        # would reproduce a name that never existed.
        assert legacy_tool_name("gh", "actions/download-logs") == "mcp_gh_actions/download-logs"


class TestLengthCap:
    def test_a_short_name_is_left_alone(self):
        name = tool_name("openseo", "keyword_research")
        assert len(name) <= MAX_NAME_LENGTH
        assert name == "mcp_openseo_keyword_research"  # no suffix machinery kicked in

    def test_an_overlong_name_is_capped(self):
        # litellm truncates at 128, so a name between 65 and 128 characters
        # reaches Anthropic intact and is rejected there. The cap is ours.
        name = tool_name("a" * 40, "b" * 40)
        assert len(name) == MAX_NAME_LENGTH

    def test_an_overlong_name_stays_distinguishable(self):
        # Two tools that share a long prefix must not collapse onto one name.
        first = tool_name("search-engine-optimisation", "fetch_the_ranking_history_for_a_keyword")
        second = tool_name("search-engine-optimisation", "fetch_the_ranking_history_for_a_domain")
        assert first != second
        assert len(first) == len(second) == MAX_NAME_LENGTH

    def test_the_prefix_survives_truncation(self):
        assert tool_name("a" * 40, "b" * 40).startswith("mcp_")


class TestCollisions:
    def test_two_servers_that_sanitise_alike_get_different_names(self):
        # "a.b" and "a/b" both clean to "a_b"; without disambiguation their
        # tools would land on one registry key and one would vanish.
        first = tool_name("a.b", "run")
        second = tool_name("a/b", "run", taken={first})
        assert first != second

    def test_two_servers_advertising_the_same_tool_get_different_names(self):
        first = tool_name("alpha", "search")
        second = tool_name("alpha", "search", taken={first})
        assert first != second

    def test_the_suffix_depends_only_on_the_pair(self):
        # Same pair, different collision context -> same disambiguated name.
        once = tool_name("a.b", "run", taken={"mcp_a_b_run"})
        twice = tool_name("a.b", "run", taken={"mcp_a_b_run", "something_else"})
        assert once == twice

    def test_a_free_name_is_not_disambiguated(self):
        assert tool_name("openseo", "search", taken={"mcp_other_search"}) == "mcp_openseo_search"

    def test_a_disambiguated_name_still_respects_the_cap(self):
        name = tool_name("a" * 40, "b" * 40, taken={tool_name("a" * 40, "b" * 40)})
        assert len(name) == MAX_NAME_LENGTH


class TestSpellings:
    """Every name one pair could ever have been registered under.

    ``resolve_configured`` tests the consumer; these pin the generator itself,
    for the facts a registry-level test cannot show -- a spelling that must NOT
    be produced leaves no trace in a lookup that already returns nothing.
    """

    def test_offers_the_current_name(self):
        assert "mcp_openseo_search" in spellings("openseo", "search")

    def test_offers_the_pre_sanitising_name(self):
        assert "mcp_gh_actions/download-logs" in spellings("gh", "actions/download-logs")

    def test_offers_the_sanitised_name_too(self):
        assert "mcp_gh_actions_download-logs" in spellings("gh", "actions/download-logs")

    def test_offers_the_disambiguated_name(self):
        collided = tool_name("a.b", "run", taken={"mcp_a_b_run"})
        assert collided in spellings("a.b", "run")

    def test_does_not_invent_a_sanitised_form_for_a_dash_that_never_changed(self):
        # "bcp-search" is legal, so no deploy ever registered
        # "mcp_bcp_search_search" -- offering it would answer a config entry
        # with a spelling that has no config behind it.
        assert "mcp_bcp_search_search" not in spellings("bcp-search", "search")

    def test_does_not_offer_a_plain_form_an_overflowing_pair_never_had(self):
        # Over the cap, tool_name makes the suffix mandatory, so the truncated
        # no-suffix form is a name no build ever registered. One rule, one
        # home: _overflows is what both functions read.
        server, tool = "a" * 40, "b" * 40
        registered = tool_name(server, tool)
        forms = spellings(server, tool)
        assert registered in forms
        assert _assemble(server, tool) not in forms

    def test_still_offers_the_plain_form_for_a_pair_that_fits(self):
        assert _assemble("openseo", "search") in spellings("openseo", "search")


class _FakeToolDef:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "stub"
        self.inputSchema = {"type": "object", "properties": {}}


def _wrapper(server: str, tool: str, *, taken=frozenset()):
    from raven.mcp.client import MCPToolWrapper

    return MCPToolWrapper(session=None, server_name=server, tool_def=_FakeToolDef(tool), taken=taken)


class TestWrapperNamesItself:
    """The wrapper is what actually registers names, so it is what the naming
    rules are pinned against -- not a hand-written string."""

    def test_wrapper_registers_the_sanitised_name(self):
        assert _wrapper("gh", "actions/download-logs").name == "mcp_gh_actions_download-logs"

    def test_wrapper_keeps_a_legal_dash(self):
        assert _wrapper("bcp-search", "search").name == "mcp_bcp-search_search"

    def test_wrapper_hands_the_registry_its_origin(self):
        # The registry stores names; recovering the pair from one is not
        # possible, so the wrapper declares it at registration. It is not a
        # public lookup: the one queryable copy lives in the registry.
        ref = _wrapper("bcp-search", "search").ref
        assert (ref.server, ref.tool) == ("bcp-search", "search")
        assert ref.name == "mcp_bcp-search_search"

    def test_wrapper_caps_an_overlong_name(self):
        assert len(_wrapper("s" * 40, "t" * 40).name) == MAX_NAME_LENGTH

    def test_wrapper_disambiguates_against_names_already_taken(self):
        first = _wrapper("alpha", "search")
        second = _wrapper("alpha", "search", taken={first.name})
        assert first.name != second.name


class TestRegistryResolvesConfiguredNames:
    def test_resolves_the_wrapper_name_by_exact_match(self):
        from raven.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        tool = _wrapper("openseo", "search")
        registry.register(tool, origin=tool.ref)
        assert registry.resolve_configured(tool.name) == [tool.name]

    def test_resolves_a_pre_sanitising_entry_to_the_sanitised_registration(self):
        # The fallback's actual job: the registry holds the cleaned name, the
        # config entry still says the one that shipped.
        from raven.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        tool = _wrapper("gh", "actions/download-logs")
        registry.register(tool, origin=tool.ref)
        assert tool.name == "mcp_gh_actions_download-logs"
        assert registry.resolve_configured("mcp_gh_actions/download-logs") == [tool.name]

    def test_an_entry_naming_a_live_tool_resolves_to_exactly_that_tool(self):
        """Exact match wins outright, and does not fan out from there.

        The registry is keyed by name, so an entry naming a tool that exists
        names *that* tool. Fanning out reached across servers: two different
        pairs can produce one historical spelling, and one of them may be a
        third tool's current name -- see the cross-server case below.
        """
        from raven.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        first = _wrapper("a.b", "run")
        registry.register(first, origin=first.ref)
        second = _wrapper("a/b", "run", taken={first.name})
        registry.register(second, origin=second.ref)

        assert first.name == "mcp_a_b_run"
        assert second.name != first.name
        assert registry.resolve_configured("mcp_a_b_run") == [first.name]

    def test_an_entry_never_reaches_a_server_it_does_not_name(self):
        """The cross-server fan-out this exact-match rule exists to stop.

        ``mcp_openseo_search_v2`` is the pre-sanitising spelling of both
        ``('openseo', 'search_v2')`` and ``('openseo_search', 'v2')``, because
        the separator is legal inside either component. It is also the first
        one's current name. Generating spellings for every origin therefore
        matched a tool from a server the entry never mentioned, whose own
        registered name looks nothing like it.
        """
        from raven.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        first = _wrapper("openseo", "search_v2")
        registry.register(first, origin=first.ref)
        second = _wrapper("openseo_search", "v2", taken={first.name})
        registry.register(second, origin=second.ref)

        assert first.name == "mcp_openseo_search_v2"
        assert second.name.startswith("mcp_openseo_search_v2_")
        assert registry.resolve_configured("mcp_openseo_search_v2") == [first.name]

    def test_a_collapsed_entry_no_tool_carries_names_every_pair_it_cleans_from(self):
        # The fallback's real job: a spelling no tool holds today. "a.b" and
        # "a/b" both clean to "a_b", so if neither ended up with the plain
        # spelling the entry genuinely means both.
        from raven.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        first = _wrapper("gh", "run.one")
        registry.register(first, origin=first.ref)
        second = _wrapper("gh", "run/one", taken={first.name})
        registry.register(second, origin=second.ref)

        assert registry.resolve_configured("mcp_gh_run.one") == [first.name]
        assert registry.resolve_configured("mcp_gh_run/one") == [second.name]

    def test_returns_nothing_for_an_entry_naming_nothing(self):
        from raven.agent.tools.registry import ToolRegistry

        assert ToolRegistry().resolve_configured("mcp_ghost_tool") == []

    def test_leaves_non_mcp_names_to_exact_matching(self):
        from raven.agent.tools.base import Tool
        from raven.agent.tools.registry import ToolRegistry

        class _Builtin(Tool):
            name = "read_file"
            description = "stub"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs):
                return ""

        registry = ToolRegistry()
        registry.register(_Builtin())
        assert registry.resolve_configured("read_file") == ["read_file"]
        assert registry.resolve_configured("read_files") == []
