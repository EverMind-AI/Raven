"""A preference read while the process runs (raven/config/live.py).

The point of the module, and the thing worth pinning, is the difference between
"read once" and "read now". A switch on the page writes a file; if the answer was
settled at startup, the switch changed a file and nothing else, and the only way
to be believed was to quit.

So these are about the read: that it notices a change, that it does not re-parse
when nothing changed, and that the two failure modes a file has -- absent, and
briefly unparseable while something rewrites it -- answer differently. The last
one matters most: a torn read must keep the previous answer, because withholding
every tool for one turn because a file was mid-write is worse than being one
second stale.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.config import live as live_module
from raven.config.live import (
    LiveConfig,
    context_window_tokens,
    curator_pin,
    disabled_playbook_names,
    disabled_tool_names,
    held,
    hold_for_this_turn,
    max_tool_iterations,
    reasoning_effort,
    skill_gate_pin,
)


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestReadingNowRatherThanOnce:
    def test_a_change_on_disk_is_the_next_answer(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        live = LiveConfig(path)
        assert disabled_tool_names(live) == frozenset({"exec"})

        _write(path, {"tools": {"disabledTools": ["exec", "web_fetch"]}})

        assert disabled_tool_names(live) == frozenset({"exec", "web_fetch"})

    def test_turning_one_back_on_is_the_same_read(self, tmp_path: Path) -> None:
        """The direction that used to be impossible. Withholding is reversible;
        unregistering was not, because nothing remembered what to put back."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        live = LiveConfig(path)
        assert disabled_tool_names(live) == frozenset({"exec"})

        _write(path, {"tools": {"disabledTools": []}})

        assert disabled_tool_names(live) == frozenset()

    def test_unchanged_bytes_are_not_parsed_again(self, tmp_path: Path) -> None:
        """Asked once per assembled tool array, so the repeat cost has to be the
        read alone. Counted through the parser rather than asserted about timing.

        The read itself is not what is being saved -- see the class docstring on
        why a ``stat`` fingerprint cannot stand in for the content."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        live = LiveConfig(path)
        live.raw()

        parses = 0
        real = live_module.json.loads

        def counting(*a: object, **k: object) -> object:
            nonlocal parses
            parses += 1
            return real(*a, **k)  # type: ignore[arg-type]

        live_module.json.loads = counting  # type: ignore[assignment]
        try:
            for _ in range(5):
                live.raw()
        finally:
            live_module.json.loads = real  # type: ignore[assignment]

        assert parses == 0

    def test_a_same_length_rewrite_is_still_seen(self, tmp_path: Path) -> None:
        """The case a ``stat`` fingerprint got wrong: equal-length writes inside
        one clock tick share an ``(mtime_ns, size)`` pair, and the second one was
        then invisible for the life of the process.

        The collision is forced with ``os.utime`` rather than left to the clock.
        It is the filesystem's granularity that decides whether two real writes
        collide -- ns on APFS, coarse on the CI runner that caught this -- so a
        test that just writes twice quickly asserts nothing where the author runs
        it and everything where CI does."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        stamp = path.stat()
        live = LiveConfig(path)
        assert disabled_tool_names(live) == frozenset({"exec"})

        _write(path, {"tools": {"disabledTools": ["grep"]}})
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert path.stat().st_size == stamp.st_size
        assert disabled_tool_names(live) == frozenset({"grep"})


class TestTheTwoWaysAFileFails:
    def test_no_file_means_no_preferences(self, tmp_path: Path) -> None:
        live = LiveConfig(tmp_path / "absent.json")
        assert disabled_tool_names(live) == frozenset()

    def test_a_file_being_rewritten_keeps_the_last_good_answer(self, tmp_path: Path) -> None:
        """Every writer makes the file briefly unparseable, and that instant must
        not change any answer: emptying the set would offer a switched-off tool,
        and emptying the config would withhold every tool at once."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        live = LiveConfig(path)
        assert disabled_tool_names(live) == frozenset({"exec"})

        path.write_text("{ this is half a write", encoding="utf-8")

        assert disabled_tool_names(live) == frozenset({"exec"})

    def test_and_recovers_when_the_write_lands(self, tmp_path: Path) -> None:
        """The pairing case: keeping the old answer must not mean never looking
        again, which a stamp updated on failure would have caused."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        live = LiveConfig(path)
        live.raw()
        path.write_text("{ half", encoding="utf-8")
        assert disabled_tool_names(live) == frozenset({"exec"})

        _write(path, {"tools": {"disabledTools": ["grep"]}})

        assert disabled_tool_names(live) == frozenset({"grep"})

    def test_a_json_document_that_is_not_an_object_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert disabled_tool_names(LiveConfig(path)) == frozenset()


class TestBothSpellingsCount:
    def test_the_wire_name_the_page_writes(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        assert disabled_tool_names(LiveConfig(path)) == frozenset({"exec"})

    def test_the_snake_case_name_the_schema_reads(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabled_tools": ["exec"]}})
        assert disabled_tool_names(LiveConfig(path)) == frozenset({"exec"})

    def test_a_file_carrying_both_counts_both(self, tmp_path: Path) -> None:
        """A switch that only counts under one spelling is a switch that works
        from one surface: `settings.set` writes the camelCase name and the
        loader's schema reads the snake_case one."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"], "disabled_tools": ["grep"]}})
        assert disabled_tool_names(LiveConfig(path)) == frozenset({"exec", "grep"})

    def test_a_list_holding_junk_keeps_only_the_names(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec", 7, None, "grep"]}})
        assert disabled_tool_names(LiveConfig(path)) == frozenset({"exec", "grep"})

    def test_a_value_that_is_not_a_list_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": "exec"}})
        assert disabled_tool_names(LiveConfig(path)) == frozenset()


class TestThePlaybookSwitch:
    """``playbooks.disabled`` has one spelling and one reader, and is read live
    for the same reason the tool switch is: the next model call, not the next
    process."""

    def test_a_disabled_playbook_is_the_next_answer(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"playbooks": {"disabled": ["weekly_report"]}})
        live = LiveConfig(path)
        assert disabled_playbook_names(live) == frozenset({"weekly_report"})

        _write(path, {"playbooks": {"disabled": ["standup"]}})
        # A second write inside the clock's granularity would leave the stamp
        # unchanged, and the read is allowed to trust the stamp.
        os.utime(path, (1, 1))
        assert disabled_playbook_names(live) == frozenset({"standup"})

    def test_nothing_disabled_is_an_empty_set_not_a_missing_key(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {})
        assert disabled_playbook_names(LiveConfig(path)) == frozenset()

    def test_a_value_that_is_not_a_list_of_names_disables_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"playbooks": {"disabled": "weekly_report"}})
        assert disabled_playbook_names(LiveConfig(path)) == frozenset()

        _write(path, {"playbooks": {"disabled": ["ok", 7, None]}})
        assert disabled_playbook_names(LiveConfig(path)) == frozenset({"ok"})


class TestExecExtraDenySlice:
    """The live exec deny list is a safety gate: an invalid candidate must
    dispense no new answer, never a coerced replacement of the last valid one."""

    def _live(self, tmp_path, payload):
        import json

        from raven.config.live import LiveConfig

        path = tmp_path / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return LiveConfig(path)

    def test_a_valid_list_is_the_answer(self, tmp_path):
        from raven.config.live import exec_extra_deny_patterns

        live = self._live(tmp_path, {"tools": {"exec": {"extraDenyPatterns": ["\\bosascript\\b"]}}})
        assert exec_extra_deny_patterns(live) == ["\\bosascript\\b"]

    def test_a_schema_invalid_list_dispenses_no_answer(self, tmp_path):
        # ExecToolConfig rejects [123]; coercing it to ["123"] here replaced
        # the last valid deny rule with junk -- the command the valid config
        # blocked then executed.
        from raven.config.live import exec_extra_deny_patterns

        live = self._live(tmp_path, {"tools": {"exec": {"extraDenyPatterns": [123]}}})
        assert exec_extra_deny_patterns(live) is None

    def test_no_key_on_disk_is_no_answer(self, tmp_path):
        from raven.config.live import exec_extra_deny_patterns

        live = self._live(tmp_path, {"tools": {"exec": {"timeout": 30}}})
        assert exec_extra_deny_patterns(live) is None

    def test_an_invalid_web_search_section_dispenses_no_answer(self, tmp_path):
        from raven.config.live import web_search_key

        live = self._live(tmp_path, {"tools": {"web": {"search": {"apiKey": 123}}}})
        assert web_search_key(live) is None

    def test_a_vendor_slot_is_read_live(self, tmp_path):
        from raven.config.live import web_provider_key

        live = self._live(tmp_path, {"tools": {"web": {"providers": {"tavily": {"apiKey": "tv"}}}}})
        assert web_provider_key(live, "tavily") == "tv"
        assert web_provider_key(live, "serper") == "", "a present subtree answers for every vendor in it"

    def test_no_providers_subtree_is_no_answer(self, tmp_path):
        from raven.config.live import web_provider_key

        live = self._live(tmp_path, {"tools": {"web": {"search": {"apiKey": "sk"}}}})
        assert web_provider_key(live, "serper") is None

    def test_an_invalid_providers_subtree_dispenses_no_answer(self, tmp_path):
        from raven.config.live import web_provider_key

        live = self._live(tmp_path, {"tools": {"web": {"providers": {"tavily": {"apiKey": 123}}}}})
        assert web_provider_key(live, "tavily") is None

    def test_an_unknown_vendor_reads_empty_rather_than_raising(self, tmp_path):
        from raven.config.live import web_provider_key

        live = self._live(tmp_path, {"tools": {"web": {"providers": {"tavily": {"apiKey": "tv"}}}}})
        assert web_provider_key(live, "bing") == ""


class TestRejectedCandidatesKeepTheLastAdmittedSlice:
    """A schema-rejected edit must not roll a credential back to the boot
    value: the last admitted live answer keeps serving until a valid candidate
    or the section's removal replaces it."""

    def _live(self, tmp_path):
        from raven.config.live import LiveConfig

        return LiveConfig(tmp_path / "config.json"), tmp_path / "config.json"

    def _write(self, path, payload):
        import json

        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_a_rejected_web_key_keeps_the_last_admitted_one(self, tmp_path):
        from raven.config.live import web_search_key

        live, path = self._live(tmp_path)
        self._write(path, {"tools": {"web": {"search": {"apiKey": "sk-live"}}}})
        assert web_search_key(live) == "sk-live"

        self._write(path, {"tools": {"web": {"search": {"apiKey": 123}}}})
        assert web_search_key(live) == "sk-live", "a rejected candidate must not reach the boot fallback"

    def test_two_vendors_last_good_answers_do_not_collide(self, tmp_path):
        """One memo slot for the pair would have the second vendor's rejected
        edit serve the first vendor's key."""
        from raven.config.live import web_provider_key

        live, path = self._live(tmp_path)
        self._write(path, {"tools": {"web": {"providers": {"serper": {"apiKey": "sk"}, "tavily": {"apiKey": "tv"}}}}})
        assert web_provider_key(live, "serper") == "sk"
        assert web_provider_key(live, "tavily") == "tv"

        self._write(path, {"tools": {"web": {"providers": {"serper": {"apiKey": "sk"}, "tavily": {"apiKey": 123}}}}})
        assert web_provider_key(live, "tavily") == "tv", "a rejected edit keeps this vendor's last answer"
        assert web_provider_key(live, "serper") == "sk", "and does not hand it the other vendor's"

    def test_a_removed_section_forgets_the_memory(self, tmp_path):
        from raven.config.live import web_search_key

        live, path = self._live(tmp_path)
        self._write(path, {"tools": {"web": {"search": {"apiKey": "sk-live"}}}})
        assert web_search_key(live) == "sk-live"

        self._write(path, {"tools": {}})
        assert web_search_key(live) is None, "no section is the constructor lane, not the last live answer"

    def test_a_rejected_media_candidate_keeps_the_last_admitted_slice(self, tmp_path):
        from raven.config.live import media_tool_config

        live, path = self._live(tmp_path)
        self._write(path, {"tools": {"media": {"image": {"apiKey": "sk-live"}}}})
        assert media_tool_config(live, "image").api_key == "sk-live"

        self._write(path, {"tools": {"media": {"image": {"apiKey": 123}}}})
        kept = media_tool_config(live, "image")
        assert kept is not None and kept.api_key == "sk-live"


class TestTheBorrowInputIsAdmittedOnTheSameTerms:
    """The media answer has two credential inputs; rejecting only the tool's
    own slice left the borrow's: an invalid ``providers.openrouter`` edit was
    degraded to an empty borrow and recorded as a new valid answer, dropping
    the previously borrowed key."""

    def _live(self, tmp_path):
        from raven.config.live import LiveConfig

        return LiveConfig(tmp_path / "config.json"), tmp_path / "config.json"

    def _write(self, path, payload):
        import json

        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_an_invalid_openrouter_edit_keeps_the_borrowed_key(self, tmp_path):
        from raven.config.live import media_tool_config

        live, path = self._live(tmp_path)
        self._write(
            path,
            {
                "tools": {"media": {"image": {"model": "some/model"}}},
                "providers": {"openrouter": {"apiKey": "sk-live"}},
            },
        )
        assert media_tool_config(live, "image").api_key == "sk-live"

        self._write(
            path, {"tools": {"media": {"image": {"model": "some/model"}}}, "providers": {"openrouter": {"apiKey": 123}}}
        )
        kept = media_tool_config(live, "image")
        assert kept is not None and kept.api_key == "sk-live", (
            "an invalid borrow input must reject the combined answer, not degrade it to keyless"
        )

    def test_no_openrouter_section_is_a_real_keyless_answer(self, tmp_path):
        from raven.config.live import media_tool_config

        live, path = self._live(tmp_path)
        self._write(path, {"tools": {"media": {"image": {"model": "some/model"}}}})
        cfg = media_tool_config(live, "image")
        assert cfg is not None and cfg.model == "some/model" and cfg.api_key == ""

    def test_a_tool_with_its_own_key_ignores_an_invalid_borrow_input(self, tmp_path):
        from raven.config.live import media_tool_config

        live, path = self._live(tmp_path)
        self._write(
            path, {"tools": {"media": {"image": {"apiKey": "sk-own"}}}, "providers": {"openrouter": {"apiKey": 123}}}
        )
        cfg = media_tool_config(live, "image")
        assert cfg is not None and cfg.api_key == "sk-own", "the borrow is not consulted, so it cannot veto"


def test_permissions_node_that_stops_validating_keeps_the_last_policy(tmp_path):
    """A broken live edit must not un-deny a default-allow tool.

    Resetting to the schema's defaults on a validation failure dropped the
    user's explicit deny rules -- reproduced: mode=full with read_file=deny,
    then the tier misspelled, read back as mode=ask with no rules at all.
    """
    from raven.config.live import LiveConfig, permissions_config

    path = tmp_path / "config.json"
    path.write_text('{"permissions": {"mode": "full", "tools": {"read_file": "deny"}}}')
    live = LiveConfig(path)
    assert permissions_config(live).tools == {"read_file": "deny"}

    path.write_text('{"permissions": {"mode": "full", "tools": {"read_file": "not-a-tier"}}}')
    kept = permissions_config(live)
    assert kept.mode == "full"
    assert kept.tools == {"read_file": "deny"}

    path.write_text('{"permissions": {"mode": "smart"}}')
    assert permissions_config(live).mode == "smart"


def test_permissions_node_invalid_from_the_start_answers_defaults(tmp_path):
    from raven.config.live import LiveConfig, permissions_config

    path = tmp_path / "config.json"
    path.write_text('{"permissions": {"mode": "godmode"}}')
    fresh = permissions_config(LiveConfig(path))
    assert fresh.mode == "smart"
    assert fresh.tools == {}


def test_a_retired_destructive_toggle_is_accepted_ignored_and_flagged():
    """The key used to lift a recursive-delete refusal that no longer exists.
    An old config still carrying it must load, must not change any ruling, and
    must be told so -- the same terms memoryWindow retired on."""
    from raven.config.schema import ExecToolConfig

    cfg = ExecToolConfig.model_validate({"allowDestructiveCommands": True})
    assert cfg.should_warn_deprecated_allow_destructive is True
    assert "allowDestructiveCommands" not in cfg.model_dump(by_alias=True)
    assert ExecToolConfig().should_warn_deprecated_allow_destructive is False


class TestSubsystemPinsAndTheCap:
    """The three values that used to be settled when the loop was built.

    A pin and an iteration cap are sentences about the next call, not work to
    redo, so they belong on this side of the line the module docstring draws.
    Each is written by ``settings.set``, which follows the spelling already in
    the file -- so each is read under both.
    """

    def test_the_curator_pin_is_read_from_either_spelling(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"context": {"curatorModel": "m", "curatorProvider": "deepseek"}})
        assert curator_pin(LiveConfig(path)) == ("m", "deepseek")

        _write(path, {"context": {"curator_model": "m2", "curator_provider": "gemini"}})
        assert curator_pin(LiveConfig(path)) == ("m2", "gemini")

    def test_the_gate_pin_is_read_from_either_spelling_of_the_block_too(self, tmp_path: Path) -> None:
        """``skillForge`` is the block ``settings.set`` writes; ``skill_forge``
        is what the loader's schema calls it, and a hand-written config may
        hold either."""
        path = tmp_path / "config.json"
        _write(path, {"skillForge": {"llmGateModel": "g", "llmGateProvider": "deepseek"}})
        assert skill_gate_pin(LiveConfig(path)) == ("g", "deepseek")

        _write(path, {"skill_forge": {"llm_gate_model": "g2", "llm_gate_provider": "gemini"}})
        assert skill_gate_pin(LiveConfig(path)) == ("g2", "gemini")

    def test_an_unset_pin_is_two_nones(self, tmp_path: Path) -> None:
        """Unset and empty both mean "follow the conversation's model", which
        is what the resolver turns into None rather than a half pair."""
        path = tmp_path / "config.json"
        _write(path, {"context": {"curatorModel": ""}})
        assert curator_pin(LiveConfig(path)) == (None, None)
        _write(path, {})
        assert curator_pin(LiveConfig(path)) == (None, None)

    def test_a_repointed_pin_is_the_next_answer(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"context": {"curatorModel": "before"}})
        live = LiveConfig(path)
        assert curator_pin(live)[0] == "before"

        _write(path, {"context": {"curatorModel": "after"}})

        assert curator_pin(live)[0] == "after"

    def test_the_cap_is_read_from_either_spelling(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"maxToolIterations": 7}}})
        assert max_tool_iterations(LiveConfig(path)) == 7

        _write(path, {"agents": {"defaults": {"max_tool_iterations": 9}}})
        assert max_tool_iterations(LiveConfig(path)) == 9

    def test_a_cap_that_is_not_a_positive_int_is_no_answer(self, tmp_path: Path) -> None:
        """None means "keep what the loop was built with". A bool is an int in
        Python and would otherwise cap the loop at 1."""
        path = tmp_path / "config.json"
        for value in ("40", 0, -1, True, None):
            _write(path, {"agents": {"defaults": {"maxToolIterations": value}}})
            assert max_tool_iterations(LiveConfig(path)) is None

    def test_the_loop_reads_its_cap_through_this(self, tmp_path: Path) -> None:
        """The property is the whole consumer: a cap raised on the settings
        page applies to the next turn, and an unset one keeps the built one."""
        from types import SimpleNamespace

        from raven.agent.loop import AgentLoop

        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"maxToolIterations": 7}}})
        loop = SimpleNamespace(_live_config=LiveConfig(path), _default_max_iterations=40)

        assert AgentLoop.max_iterations.fget(loop) == 7

        _write(path, {})

        assert AgentLoop.max_iterations.fget(loop) == 40


class TestTheWindowIsResolvedPerTurn:
    """``agents.defaults.contextWindowTokens`` reaches a turn through its binding.

    The number used to live in two places -- one copy frozen on the loop that
    only the usage report read, one on each binding that the budget read -- so a
    write reached neither until a restart, and after a model switch the two
    disagreed: the trimming ran on the new number while the page showed the old
    model's catalogue size. One source now, resolved once when a turn starts.
    """

    def test_the_window_is_read_from_either_spelling(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"contextWindowTokens": 50000}}})
        assert context_window_tokens(LiveConfig(path)) == 50000

        _write(path, {"agents": {"defaults": {"context_window_tokens": 60000}}})
        assert context_window_tokens(LiveConfig(path)) == 60000

    def test_no_override_is_no_answer(self, tmp_path: Path) -> None:
        """None lets the rates ladder answer with the model's own window, which
        is what an unset key means."""
        path = tmp_path / "config.json"
        for value in ("50000", 0, -1, True, None):
            _write(path, {"agents": {"defaults": {"contextWindowTokens": value}}})
            assert context_window_tokens(LiveConfig(path)) is None
        _write(path, {})
        assert context_window_tokens(LiveConfig(path)) is None

    def test_a_turn_takes_the_window_the_file_has_when_it_starts(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from raven.agent.loop import AgentLoop
        from raven.providers.binding import ModelBinding

        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"contextWindowTokens": 50000}}})
        loop = SimpleNamespace(_live_config=LiveConfig(path))
        built = ModelBinding(object(), "some/model")

        pinned = AgentLoop._with_live_window(loop, built)

        assert pinned.configured_window == 50000
        assert pinned.model == built.model and pinned.provider is built.provider

    def test_an_unchanged_number_keeps_the_binding_it_was_given(self, tmp_path: Path) -> None:
        """Same object, so the window it already resolved from the rates
        catalogue is not resolved again every turn."""
        from types import SimpleNamespace

        from raven.agent.loop import AgentLoop
        from raven.providers.binding import ModelBinding

        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"contextWindowTokens": 50000}}})
        loop = SimpleNamespace(_live_config=LiveConfig(path))
        binding = ModelBinding(object(), "some/model", 50000)

        assert AgentLoop._with_live_window(loop, binding) is binding

    def test_clearing_the_key_drops_the_override(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from raven.agent.loop import AgentLoop
        from raven.providers.binding import ModelBinding

        path = tmp_path / "config.json"
        _write(path, {})
        loop = SimpleNamespace(_live_config=LiveConfig(path))

        dropped = AgentLoop._with_live_window(loop, ModelBinding(object(), "some/model", 50000))

        assert dropped.configured_window is None

    def test_a_turn_already_running_keeps_its_number(self, tmp_path: Path) -> None:
        """Resolved once at the start and then held: the budget is read several
        times while a turn runs, and a number that moved between those reads
        would leave one turn disagreeing with itself."""
        from types import SimpleNamespace

        from raven.agent.loop import AgentLoop
        from raven.providers.binding import ModelBinding, use_binding

        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"contextWindowTokens": 50000}}})
        loop = SimpleNamespace(_live_config=LiveConfig(path), _default_binding=ModelBinding(object(), "d/m"))
        turn_binding = AgentLoop._with_live_window(loop, ModelBinding(object(), "some/model"))

        with use_binding(turn_binding):
            _write(path, {"agents": {"defaults": {"contextWindowTokens": 9999}}})
            assert AgentLoop.context_window_tokens.fget(loop) == 50000

        assert AgentLoop._with_live_window(loop, turn_binding).configured_window == 9999


class TestOneTurnOneAnswer:
    """A preference is read live; a turn still gets one answer for it.

    A turn asks several times and sometimes minutes apart, so "read at the
    moment of use" and "one answer per turn" are not the same rule. The cap is
    the sharp case: it is first read after context assembly, which can spend
    minutes in the curator, so an edit made while a turn is running would
    otherwise change that turn.
    """

    def test_a_held_value_is_read_once(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"maxToolIterations": 7}}})
        live = LiveConfig(path)

        with hold_for_this_turn():
            assert held("cap", lambda: max_tool_iterations(live)) == 7
            _write(path, {"agents": {"defaults": {"maxToolIterations": 99}}})
            assert held("cap", lambda: max_tool_iterations(live)) == 7

        assert held("cap", lambda: max_tool_iterations(live)) == 99

    def test_a_hold_ends_with_its_turn(self, tmp_path: Path) -> None:
        """Outside one, every read is live again -- which is what a caller below
        ``run_turn`` and every background path gets, and what this did before
        there was a hold at all. A hold that outlived its turn would freeze the
        setting for the rest of the process."""
        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"maxToolIterations": 7}}})
        live = LiveConfig(path)

        with hold_for_this_turn():
            assert held("cap", lambda: max_tool_iterations(live)) == 7

        _write(path, {"agents": {"defaults": {"maxToolIterations": 99}}})
        assert held("cap", lambda: max_tool_iterations(live)) == 99
        _write(path, {"agents": {"defaults": {"maxToolIterations": 55}}})
        assert held("cap", lambda: max_tool_iterations(live)) == 55

    def test_the_cap_is_resolved_when_the_turn_opens_not_at_its_first_read(self, tmp_path: Path) -> None:
        """The crux: resolved lazily it would still be read after assembly,
        which is exactly the window an operator's edit lands in."""
        from types import SimpleNamespace

        from raven.agent.loop import AgentLoop

        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"maxToolIterations": 7}}})
        loop = SimpleNamespace(_live_config=LiveConfig(path), _default_max_iterations=40)

        with AgentLoop._turn_scope(loop):
            _write(path, {"agents": {"defaults": {"maxToolIterations": 99}}})
            assert AgentLoop.max_iterations.fget(loop) == 7

        assert AgentLoop.max_iterations.fget(loop) == 99

    def test_a_turn_with_no_override_keeps_the_built_cap(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from raven.agent.loop import AgentLoop

        path = tmp_path / "config.json"
        _write(path, {})
        loop = SimpleNamespace(_live_config=LiveConfig(path), _default_max_iterations=40)

        with AgentLoop._turn_scope(loop):
            _write(path, {"agents": {"defaults": {"maxToolIterations": 99}}})
            assert AgentLoop.max_iterations.fget(loop) == 40


class TestTheRestOfWhatAWriteDidNotReach:
    """The settings a write reached the file but not the process.

    None of these was ever reload-only -- the surface said "Saved." and nothing
    said otherwise -- so they were the silent half of the same class the
    reload-only set named. Each is read where it is used now; what the process
    was built with answers when the file has no opinion.
    """

    def test_the_effort_is_read_from_either_spelling(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"reasoningEffort": "high"}}})
        assert reasoning_effort(LiveConfig(path)) == "high"
        _write(path, {"agents": {"defaults": {"reasoning_effort": "low"}}})
        assert reasoning_effort(LiveConfig(path)) == "low"
        _write(path, {})
        assert reasoning_effort(LiveConfig(path)) is None

    def test_the_loop_holds_its_effort_for_the_turn(self, tmp_path: Path) -> None:
        """It rides every model call of the turn, so two calls of one turn at
        two efforts is the split the subsystem pins were fixed for."""
        from types import SimpleNamespace

        from raven.agent.loop import AgentLoop

        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"reasoningEffort": "high"}}})
        loop = SimpleNamespace(_live_config=LiveConfig(path), _default_max_iterations=40)

        with AgentLoop._turn_scope(loop):
            _write(path, {"agents": {"defaults": {"reasoningEffort": "low"}}})
            assert AgentLoop.default_reasoning_effort.fget(loop) == "high"

        assert AgentLoop.default_reasoning_effort.fget(loop) == "low"

    def test_no_configured_effort_passes_nothing(self, tmp_path: Path) -> None:
        """None leaves the provider's own default standing; an explicit None
        would switch it off instead."""
        from types import SimpleNamespace

        from raven.agent.loop import AgentLoop

        path = tmp_path / "config.json"
        _write(path, {})
        loop = SimpleNamespace(_live_config=LiveConfig(path))

        assert AgentLoop.default_reasoning_effort.fget(loop) is None

    def test_personalization_follows_the_file_over_the_built_value(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from raven.agent.loop import AgentLoop

        path = tmp_path / "config.json"
        _write(path, {"agents": {"defaults": {"enablePersonalization": True}}})
        loop = SimpleNamespace(_live_config=LiveConfig(path), enable_personalization=False)

        assert AgentLoop.personalization_enabled.fget(loop) is True

        _write(path, {"agents": {"defaults": {"enablePersonalization": False}}})
        assert AgentLoop.personalization_enabled.fget(loop) is False

        # No opinion on file: what the process was built with answers.
        _write(path, {})
        assert AgentLoop.personalization_enabled.fget(loop) is False
        loop.enable_personalization = True
        assert AgentLoop.personalization_enabled.fget(loop) is True

    def test_the_recall_depth_follows_the_file(self, tmp_path: Path, monkeypatch) -> None:
        from raven.context_engine.segments.memory import MemorySegmentBuilder

        path = tmp_path / "config.json"
        _write(path, {"memory": {"memoryTopK": 9}})
        monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
        builder = MemorySegmentBuilder(memory_store=None, backend=None, memory_top_k=5)

        assert builder._top_k() == 9
        _write(path, {})
        assert builder._top_k() == 5, "what it was built with answers when the file does not"

    def test_a_spawn_takes_the_web_vendors_the_file_has(self, tmp_path: Path, monkeypatch) -> None:
        """The keys beside them have been read live since they landed; the
        vendor was the half of the pair that still owed a restart."""
        from types import SimpleNamespace

        from raven.agent.subagent.manager import SubagentManager

        path = tmp_path / "config.json"
        _write(path, {"tools": {"web": {"search": {"provider": "brave"}, "fetch": {"provider": "exa"}}}})
        monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
        mgr = SimpleNamespace(web_search_provider="serper", web_fetch_provider="jina")

        assert SubagentManager._web_search_provider_now(mgr) == "brave"
        assert SubagentManager._web_fetch_provider_now(mgr) == "exa"

        _write(path, {})
        assert SubagentManager._web_search_provider_now(mgr) == "serper"
        assert SubagentManager._web_fetch_provider_now(mgr) == "jina"

    def test_the_exec_ceiling_follows_the_file(self, tmp_path: Path, monkeypatch) -> None:
        """A sub-agent's copy of this tool outlives the spawn that built it, so
        a timeout copied at construction outlived every edit to it."""
        from raven.agent.tools.shell import ExecTool

        path = tmp_path / "config.json"
        _write(path, {"tools": {"exec": {"timeout": 300}}})
        monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
        tool = ExecTool(working_dir=str(tmp_path), timeout=60)

        assert tool.timeout == 300
        _write(path, {})
        assert tool.timeout == 60

    def test_these_are_held_for_the_turn_as_well(self, tmp_path: Path, monkeypatch) -> None:
        """Same rule as the cap and the pins, and the same reason twice over: a
        turn that runs two commands must not straddle an edit between them, and
        the repeat read costs a dict lookup instead of a file read."""
        from raven.agent.tools.shell import ExecTool

        path = tmp_path / "config.json"
        _write(path, {"tools": {"exec": {"timeout": 300}}})
        monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
        tool = ExecTool(working_dir=str(tmp_path), timeout=60)

        with hold_for_this_turn():
            assert tool.timeout == 300
            _write(path, {"tools": {"exec": {"timeout": 900}}})
            assert tool.timeout == 300

        assert tool.timeout == 900

    def test_the_main_web_tools_follow_the_saved_vendor(self, tmp_path: Path, monkeypatch) -> None:
        """The keys have been read live since they landed and resolve against
        the selection, so a vendor frozen at registration kept the pair on the
        old endpoint -- with the new vendor's key never reaching anything."""
        from raven.agent.tools.web import WebSearchTool

        path = tmp_path / "config.json"
        _write(path, {})
        monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
        vendor = {"now": "serper"}
        tool = WebSearchTool(api_key=lambda: f"{vendor['now']}-key", provider=lambda: vendor["now"])

        assert tool.provider == "serper"
        assert tool.api_key == "serper-key"

        vendor["now"] = "exa"

        assert tool.provider == "exa"
        assert tool.api_key == "exa-key"

    def test_an_unknown_vendor_keeps_the_tool_working(self, tmp_path: Path, monkeypatch) -> None:
        """The file is read while a turn runs, so a typo in it must not take the
        tool down mid-call; the vendor it was registered with answers."""
        from raven.agent.tools.web import WebSearchTool

        tool = WebSearchTool(api_key="k", provider=lambda: "not-a-vendor")

        assert tool.provider == "serper"

    def test_a_spawn_hands_down_the_keys_the_file_has(self, tmp_path: Path, monkeypatch) -> None:
        """The vendor a sub-agent runs on is read live, so handing it the keys
        the manager was built with leaves the other half of the pair behind."""
        from types import SimpleNamespace

        from raven.agent.subagent.manager import SubagentManager

        path = tmp_path / "config.json"
        _write(path, {"tools": {"web": {"providers": {"exa": {"apiKey": "exa-live"}}}}})
        monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
        mgr = SimpleNamespace(web_provider_keys={"serper": "serper-boot"})

        keys = SubagentManager._web_provider_keys_now(mgr)

        assert keys == {"serper": "serper-boot", "exa": "exa-live"}

    def test_a_cleared_vendor_key_revokes_the_one_a_spawn_booted_with(self, tmp_path: Path, monkeypatch) -> None:
        """Absent and cleared are different answers.

        The caller merges this over what it booted with, so a cleared vendor
        dropped from the answer would restore the credential the settings
        surface just removed -- and the next sub-agent would keep spending it.
        """
        from types import SimpleNamespace

        from raven.agent.subagent.manager import SubagentManager

        path = tmp_path / "config.json"
        _write(path, {"tools": {"web": {"providers": {"exa": {"apiKey": ""}}}}})
        monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
        mgr = SimpleNamespace(web_provider_keys={"exa": "exa-boot", "serper": "serper-boot"})

        keys = SubagentManager._web_provider_keys_now(mgr)

        assert keys["exa"] == "", "the cleared vendor must not fall back to the boot credential"
        assert keys["serper"] == "serper-boot", "a vendor the file says nothing about keeps its boot value"


class TestLiveVendorKeyFollowsTheCanonicalOrder:
    """``live_vendor_key`` answers what ``WebToolsConfig.vendor_key`` answers for
    the same file, so a tool reading its key live never loses a key the process
    booted with. Review measured the Jina leaf falling out the moment any
    ``providers`` subtree appeared: the schema-backed slot reader answers an
    empty key for a vendor the subtree does not name, and that empty was read
    as a revocation before the leaf or the boot value was consulted."""

    ROWS = {
        "the jina leaf alone": ({"jinaApiKey": "sk-jina-paid"}, "jina", "sk-jina-paid"),
        "the jina leaf beside providers.serper": (
            {"jinaApiKey": "sk-jina-paid", "providers": {"serper": {"apiKey": "sk-serper"}}},
            "jina",
            "sk-jina-paid",
        ),
        "the serper leaf beside providers.firecrawl": (
            {"search": {"apiKey": "sk-serper-legacy"}, "providers": {"firecrawl": {"apiKey": "fc"}}},
            "serper",
            "sk-serper-legacy",
        ),
        "a cleared slot beside the serper leaf": (
            {"search": {"apiKey": "sk-serper-legacy"}, "providers": {"serper": {"apiKey": ""}}},
            "serper",
            "sk-serper-legacy",
        ),
        "a cleared slot with no leaf": ({"providers": {"tavily": {"apiKey": ""}}}, "tavily", ""),
        "a slot wins over its leaf": (
            {"jinaApiKey": "sk-jina-legacy", "providers": {"jina": {"apiKey": "sk-jina-slot"}}},
            "jina",
            "sk-jina-slot",
        ),
    }

    @pytest.mark.parametrize("row", sorted(ROWS))
    def test_the_live_key_is_the_canonical_key(self, tmp_path: Path, row: str) -> None:
        from raven.config.live import live_vendor_key
        from raven.config.schema import WebToolsConfig

        web, vendor, expected = self.ROWS[row]
        path = tmp_path / "config.json"
        _write(path, {"tools": {"web": web}})
        boot = WebToolsConfig.model_validate(web).vendor_key(vendor)

        assert boot == expected, "the canonical resolver is the oracle"
        assert live_vendor_key(LiveConfig(path), vendor, boot=boot) == expected

    def test_a_vendor_the_file_says_nothing_about_keeps_its_boot_key(self, tmp_path: Path) -> None:
        """A ``providers`` subtree naming other vendors says nothing about this
        one, so the key a harness passed through with no file entry behind it
        stays; a subtree naming it with an empty key revokes it."""
        from raven.config.live import live_vendor_key

        path = tmp_path / "config.json"
        _write(path, {"tools": {"web": {"providers": {"serper": {"apiKey": "sk-serper"}}}}})
        live = LiveConfig(path)

        assert live_vendor_key(live, "tavily", boot="tv-harness") == "tv-harness"
        _write(path, {"tools": {"web": {"providers": {"serper": {"apiKey": "sk-serper"}, "tavily": {"apiKey": ""}}}}})
        assert live_vendor_key(live, "tavily", boot="tv-harness") == ""

    @pytest.mark.parametrize(("leaf", "vendor"), [("jinaApiKey", "jina"), ("search", "serper")])
    def test_a_rotated_leaf_reaches_the_next_read(self, tmp_path: Path, leaf: str, vendor: str) -> None:
        """The boot value would satisfy a leaf the file still carries unchanged;
        a leaf rotated in the file is what the live read of it is for, and a
        ``providers`` subtree naming another vendor must not hide it."""
        from raven.config.live import live_vendor_key

        def web(key: str) -> dict:
            value = {"apiKey": key} if leaf == "search" else key
            return {leaf: value, "providers": {"firecrawl": {"apiKey": "fc"}}}

        path = tmp_path / "config.json"
        _write(path, {"tools": {"web": web("sk-old")}})
        live = LiveConfig(path)
        assert live_vendor_key(live, vendor, boot="sk-old") == "sk-old"

        _write(path, {"tools": {"web": web("sk-new")}})

        assert live_vendor_key(live, vendor, boot="sk-old") == "sk-new"

    def test_a_revoked_leaf_is_an_answer_not_a_miss(self, tmp_path: Path) -> None:
        from raven.config.live import live_vendor_key

        path = tmp_path / "config.json"
        _write(path, {"tools": {"web": {"jinaApiKey": ""}}})

        assert live_vendor_key(LiveConfig(path), "jina", boot="sk-jina-boot") == ""

    def test_a_leaf_the_schema_rejects_dispenses_no_new_answer(self, tmp_path: Path) -> None:
        from raven.config.live import web_jina_key

        path = tmp_path / "config.json"
        _write(path, {"tools": {"web": {"jinaApiKey": "sk-jina-paid"}}})
        live = LiveConfig(path)
        assert web_jina_key(live) == "sk-jina-paid"

        _write(path, {"tools": {"web": {"jinaApiKey": ["not", "a", "key"]}}})
        assert web_jina_key(live) == "sk-jina-paid", "the last admitted key keeps serving"

        _write(path, {"tools": {"web": {"search": {"apiKey": "x"}}}})
        assert web_jina_key(live) is None, "no leaf is no answer"
