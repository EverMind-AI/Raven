"""The model selector, over the protocol's stable configuration surface.

What is stable is the point of this file. ``session/set_model`` does not exist in
the schema and neither does ``models.availableModels`` -- both appear in older
material -- so the channel is a generic option list with one entry carrying
``category: "model"``. An agent that waited for either of the other two would
never be asked to switch a model.

Two of raven's own facts are asserted here as *visible* rather than merely true:
the switch is process-wide, and it is refused during a turn. Both are stated in
the option's ``description``, which the schema declares as text for the client to
display -- so a person reads the caveat where they make the choice, not in a
document they will not open.
"""

from __future__ import annotations

import pytest

from raven.acp.config_options import (
    MAX_MODELS_PER_PROVIDER,
    MODEL_DESCRIPTION,
    MODEL_OPTION_ID,
    model_option,
    set_model,
)
from tests.acp_schema import validate_def


def _catalogue(**overrides):
    options = {
        "model": "sonnet-5",
        "provider": "anthropic",
        "providers": [
            {
                "slug": "anthropic",
                "name": "Anthropic",
                "authenticated": True,
                "models": ["sonnet-5", "opus-5"],
                "model_labels": {"sonnet-5": "Claude Sonnet 5", "opus-5": "Claude Opus 5"},
            },
            {
                "slug": "openai",
                "name": "OpenAI",
                "authenticated": False,
                "models": ["gpt-9"],
                "model_labels": {},
            },
        ],
    }
    options.update(overrides)
    return options


def _caller(catalogue=None, *, error: Exception | None = None):
    calls: list[tuple[str, dict]] = []

    async def call(method: str, params: dict):
        calls.append((method, params))
        if error is not None:
            raise error
        if method == "model.options":
            return _catalogue() if catalogue is None else catalogue
        return {"applied": True}

    return call, calls


class TestTheOffer:
    async def test_it_is_a_model_categorised_select_and_matches_the_schema(self):
        call, _ = _caller()

        option = await model_option(call)

        assert option["id"] == MODEL_OPTION_ID
        assert option["category"] == "model", "the category is how a client finds the model picker"
        assert option["type"] == "select"
        validate_def("SessionConfigOption", option)

    async def test_the_caveats_are_where_a_person_will_read_them(self):
        """The schema declares ``description`` as text for the client to display,
        and what it has to say is the scope: the switch binds this session and
        leaves the installation default alone, and a turn already running keeps
        the model it started on. Neither is inferable from the protocol's shape,
        and the earlier copy said the opposite of both."""
        call, _ = _caller()

        option = await model_option(call)

        assert option["description"] == MODEL_DESCRIPTION
        assert "scoped to this session" in option["description"]
        assert "during a turn is allowed" in option["description"]

    async def test_the_current_value_is_read_back_for_the_session(self):
        """The switch is session-scoped, so the value shown has to be the
        session's. Asked without the id, ``model.options`` answers from the
        installation default and a switch that applied would report as though it
        had not."""
        call, calls = _caller()

        await model_option(call, session_id="acp:s1")

        assert calls == [("model.options", {"session_id": "acp:s1"})]

    async def test_with_no_session_yet_it_asks_for_the_installation_default(self):
        """``initialize`` has no session to ask about, and sending a null id
        would be a parameter the runtime has to reject or ignore."""
        call, calls = _caller()

        await model_option(call)

        assert calls == [("model.options", {})]

    async def test_the_current_value_names_its_provider(self):
        """Stored the way every other surface stores it, so the value a client
        sends back is the one ``config.set`` already understands."""
        call, _ = _caller()

        option = await model_option(call)

        assert option["currentValue"] == "anthropic/sonnet-5"

    async def test_the_current_value_is_always_selectable(self):
        """A dropdown whose current value is not among its options renders with
        nothing selected. A model configured by hand, or newer than the bundled
        catalogue, is a real case -- so it is added rather than hidden."""
        call, _ = _caller(_catalogue(model="my-local-finetune", provider="custom"))

        option = await model_option(call)

        values = [o["value"] for group in option["options"] for o in group["options"]]
        assert option["currentValue"] in values
        validate_def("SessionConfigOption", option)

    async def test_options_are_grouped_by_provider_with_labels_as_descriptions(self):
        call, _ = _caller()

        option = await model_option(call)

        groups = {group["group"]: group for group in option["options"]}
        assert list(groups) == ["anthropic"]
        assert groups["anthropic"]["name"] == "Anthropic"
        assert groups["anthropic"]["options"][0] == {
            "value": "anthropic/sonnet-5",
            "name": "sonnet-5",
            "description": "Claude Sonnet 5",
        }

    async def test_a_catalogue_that_already_qualifies_its_ids_is_not_qualified_twice(self):
        """Measured against the real catalogue, which does qualify them: the
        fixtures above use bare ids, so nothing here would have caught
        ``anthropic/anthropic/claude-opus-5`` -- a value ``config.set`` refuses
        and a dropdown nobody can use."""
        call, _ = _caller(
            _catalogue(
                model="anthropic/opus-5",
                providers=[
                    {
                        "slug": "anthropic",
                        "name": "Anthropic",
                        "authenticated": True,
                        "models": ["anthropic/opus-5", "anthropic/sonnet-5"],
                        "model_labels": {},
                    }
                ],
            )
        )

        option = await model_option(call)

        values = [o["value"] for group in option["options"] for o in group["options"]]
        assert values == ["anthropic/opus-5", "anthropic/sonnet-5"]
        assert option["currentValue"] == "anthropic/opus-5"
        assert [group["group"] for group in option["options"]] == ["anthropic"], (
            "the current model resolved inside its own group, so no separate Current group is needed"
        )

    async def test_the_visible_name_drops_the_provider_it_is_already_grouped_under(self):
        call, _ = _caller(
            _catalogue(
                providers=[
                    {
                        "slug": "anthropic",
                        "name": "Anthropic",
                        "authenticated": True,
                        "models": ["anthropic/opus-5"],
                        "model_labels": {},
                    }
                ]
            )
        )

        option = await model_option(call)

        group = next(g for g in option["options"] if g["group"] == "anthropic")
        assert group["options"][0]["name"] == "opus-5", "saying the same word twice"

    async def test_the_provider_in_use_is_offered_even_when_it_reports_no_credential(self):
        """``authenticated`` reports whether raven's own config holds a
        credential. Measured on a machine where every provider reported false
        while ``anthropic/claude-opus-4-5`` was answering: filtering on that flag
        alone hid every model that worked."""
        call, _ = _caller(
            _catalogue(
                providers=[
                    {
                        "slug": "anthropic",
                        "name": "Anthropic",
                        "authenticated": False,
                        "models": ["sonnet-5", "opus-5"],
                        "model_labels": {},
                    }
                ]
            )
        )

        option = await model_option(call)

        assert [group["group"] for group in option["options"]] == ["anthropic"]
        assert len(option["options"][0]["options"]) == 2

    async def test_an_unauthenticated_provider_is_left_out(self):
        """Its ids would be selectable and every selection would fail on a
        missing credential -- a dropdown that lies about what it can do."""
        call, _ = _caller()

        option = await model_option(call)

        assert not any(group["group"] == "openai" for group in option["options"])

    async def test_a_provider_with_no_models_is_left_out(self):
        call, _ = _caller(
            _catalogue(providers=[{"slug": "anthropic", "name": "A", "authenticated": True, "models": []}])
        )

        option = await model_option(call)

        assert [group["group"] for group in option["options"]] == ["current"], (
            "the empty provider contributes nothing, and the model in use is still worth offering"
        )

    async def test_a_long_catalogue_is_capped(self):
        """Providers with hundreds of ids exist, and a client rendering all of
        them is a client nobody can pick from."""
        call, _ = _caller(
            _catalogue(
                providers=[
                    {
                        "slug": "big",
                        "name": "Big",
                        "authenticated": True,
                        "models": [f"m{n}" for n in range(MAX_MODELS_PER_PROVIDER + 20)],
                    }
                ]
            )
        )

        option = await model_option(call)

        big = next(group for group in option["options"] if group["group"] == "big")
        assert len(big["options"]) == MAX_MODELS_PER_PROVIDER

    async def test_the_model_in_use_is_offered_even_with_no_catalogue_at_all(self):
        """The check for "nothing to offer" has to come *after* the current value
        is considered. A working installation whose credentials come from the
        environment reports no provider at all, and returning early on the group
        list alone hid the model that was actually running."""
        call, _ = _caller(_catalogue(providers=[]))

        option = await model_option(call)

        assert option["currentValue"] == "anthropic/sonnet-5"
        assert [group["group"] for group in option["options"]] == ["current"]
        validate_def("SessionConfigOption", option)

    async def test_nothing_configured_and_nothing_running_offers_nothing(self):
        """An option whose list is empty is a dropdown a person can open and not
        choose from, which reads as broken rather than as "set this up first"."""
        call, _ = _caller(_catalogue(model="", provider="", providers=[]))

        assert await model_option(call) is None

    @pytest.mark.parametrize("catalogue", ["nope", {}, {"providers": "lots"}, {"model": ""}, {"providers": [None, 5]}])
    async def test_a_malformed_catalogue_offers_nothing(self, catalogue):
        call, _ = _caller(catalogue)

        assert await model_option(call) is None

    async def test_a_catalogue_that_is_literally_none_offers_nothing(self):
        """Split out because the fixture reads ``None`` as "use the default"."""

        async def call(method: str, params: dict):
            return None

        assert await model_option(call) is None

    async def test_a_corrupt_provider_entry_costs_only_itself(self):
        call, _ = _caller(
            _catalogue(
                providers=[
                    None,
                    5,
                    {"name": "no slug", "authenticated": True, "models": ["x"]},
                    {"slug": "anthropic", "name": "Anthropic", "authenticated": True, "models": ["sonnet-5"]},
                ]
            )
        )

        option = await model_option(call)

        assert [group["group"] for group in option["options"]] == ["anthropic"]

    async def test_a_failing_catalogue_does_not_fail_the_session(self):
        """This is asked during ``session/new``. A missing model surface must not
        take the handshake down with it."""
        call, _ = _caller(error=RuntimeError("provider registry is unreachable"))

        assert await model_option(call) is None


class TestApplying:
    async def test_it_writes_through_the_runtimes_own_setter(self):
        call, calls = _caller()

        await set_model(call, session_id="acp:s1", value="anthropic/opus-5")

        assert calls == [
            ("model.options", {"session_id": "acp:s1"}),
            (
                "config.set",
                {
                    "key": "model",
                    "value": "anthropic/opus-5",
                    "provider": "anthropic",
                    "session_id": "acp:s1",
                },
            ),
        ]

    async def test_without_the_provider_every_selection_is_refused(self):
        """``config.set`` requires the provider as its own field -- a model id
        does not name whose credential serves it -- so a call that sends only the
        value fails before applying anything. Every model switch through this
        surface did."""
        call, calls = _caller()

        await set_model(call, session_id="acp:s1", value="anthropic/opus-5")

        applied = [params for method, params in calls if method == "config.set"]
        assert applied and applied[0].get("provider"), "the write must name a provider"

    async def test_the_provider_is_looked_up_rather_than_split_off_the_value(self):
        """A gateway serving a vendor's already-qualified id is the case the
        runtime's separate field exists for: ``openrouter`` offering
        ``anthropic/claude-haiku-4-5`` bills OpenRouter, and splitting the string
        would have named Anthropic and spent a credential nobody chose."""
        catalogue = {
            "model": "openrouter/x",
            "provider": "openrouter",
            "providers": [
                {"slug": "openrouter", "authenticated": True, "models": ["anthropic/claude-haiku-4-5"]},
            ],
        }
        call, calls = _caller(catalogue)

        await set_model(call, session_id="acp:s1", value="anthropic/claude-haiku-4-5")

        applied = [params for method, params in calls if method == "config.set"]
        assert applied[0]["provider"] == "openrouter"

    async def test_a_tie_goes_to_the_provider_already_in_use(self):
        """Two providers can offer the same option value. Of the two, the one the
        session is already running on is the credential the person picked."""
        catalogue = {
            "model": "anthropic/claude-haiku-4-5",
            "provider": "anthropic",
            "providers": [
                {"slug": "openrouter", "authenticated": True, "models": ["anthropic/claude-haiku-4-5"]},
                {"slug": "anthropic", "authenticated": True, "models": ["claude-haiku-4-5"]},
            ],
        }
        call, calls = _caller(catalogue)

        await set_model(call, session_id="acp:s1", value="anthropic/claude-haiku-4-5")

        applied = [params for method, params in calls if method == "config.set"]
        assert applied[0]["provider"] == "anthropic"

    async def test_a_model_no_provider_offers_is_refused_rather_than_guessed(self):
        """Inventing a provider here would spend a credential the person never
        chose, so a value this catalogue cannot place is refused instead."""
        call, calls = _caller()

        with pytest.raises(ValueError):
            await set_model(call, session_id="acp:s1", value="nobody/serves-this")

        assert [method for method, _ in calls] == ["model.options"], "nothing was applied"

    async def test_the_current_selection_needs_no_search(self):
        """Re-picking the model already in use is a real click, and the catalogue
        already names its provider -- so it is answered from there rather than
        found again in the group list, which a hand-configured model would not
        appear in at all."""
        catalogue = {"model": "some-vendor/exotic-1", "provider": "some-vendor", "providers": []}
        call, calls = _caller(catalogue)

        await set_model(call, session_id="acp:s1", value="some-vendor/exotic-1")

        applied = [params for method, params in calls if method == "config.set"]
        assert applied[0]["provider"] == "some-vendor"

    async def test_an_unreachable_catalogue_refuses_rather_than_writes(self):
        """The lookup is the only source of the provider, so losing it has to stop
        the write: applying without one raises inside the runtime, and applying
        with a guess spends a credential nobody chose."""
        call, calls = _caller(error=RuntimeError("provider registry is unreachable"))

        with pytest.raises(ValueError):
            await set_model(call, session_id="acp:s1", value="anthropic/opus-5")

        assert [method for method, _ in calls] == ["model.options"]

    async def test_a_malformed_catalogue_entry_is_skipped_not_trusted(self):
        """The catalogue crosses an RPC boundary, so its shape is checked rather
        than assumed. A junk entry must not become the provider a switch bills."""
        catalogue = {
            "model": "anthropic/sonnet-5",
            "provider": "anthropic",
            "providers": [
                "not-a-dict",
                {"slug": "", "authenticated": True, "models": ["opus-5"]},
                {"slug": "groq", "authenticated": True, "models": [None, "opus-5"]},
            ],
        }
        call, calls = _caller(catalogue)

        await set_model(call, session_id="acp:s1", value="groq/opus-5")

        applied = [params for method, params in calls if method == "config.set"]
        assert applied[0]["provider"] == "groq"

    async def test_the_session_id_is_passed_so_the_switch_stays_scoped_to_it(self):
        """``config.set`` with a session_id binds that session and leaves
        ``agents.defaults`` alone. Omitting it would widen a per-session option
        into an installation-wide one."""
        call, calls = _caller()

        await set_model(call, session_id="acp:busy", value="anthropic/opus-5")

        applied = [params for method, params in calls if method == "config.set"]
        assert applied[0]["session_id"] == "acp:busy"

    @pytest.mark.parametrize("value", [None, "", 5, [], {"model": "x"}])
    async def test_an_unusable_value_is_refused_before_the_write(self, value):
        call, calls = _caller()

        with pytest.raises(ValueError):
            await set_model(call, session_id="acp:s1", value=value)

        assert calls == []
