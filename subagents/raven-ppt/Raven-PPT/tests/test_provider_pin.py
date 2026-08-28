"""Tests for raven.providers.pin -- which provider a model change should pin.

``agents.defaults.provider`` overrides what a model id says, so a stale one sends
the new model's request to the old vendor with the old vendor's key. The picker
kept the two in step; the CLI did not. The CLI then told users to edit the
field by hand -- a field no command wrote -- and the two surfaces answered the
same question differently. These assert the one rule, and that both ask it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.providers import pin

#: (model, explicit provider, current pin) -> what must be written.
#: None means "cannot tell": the caller has to ask rather than write a guess.
#:
#: Every case runs with the named vendors configured, because the answer depends
#: on it: a pin is consulted before anything else and is answered with that
#: vendor's section whether or not it holds credentials, so pinning an
#: unconfigured one fails every request on a missing key -- never reaching the
#: fallback that lets a gateway serve a model whose id names the vendor behind it.
CASES: list[tuple[str, str, str, str | None]] = [
    # An explicit choice wins outright -- a picker selection or a --provider flag.
    ("claude-sonnet-5", "anthropic", "openai", "anthropic"),
    # A qualified id names its own vendor, which beats a stale pin.
    ("anthropic/claude-sonnet-5", "", "openai", "anthropic"),
    ("deepseek/deepseek-chat", "", "", "deepseek"),
    # Prefixed but no spec of ours: keeping the pin would hand its key to a
    # vendor it does not belong to.
    ("mistral/mistral-large", "", "openai", "auto"),
    ("mistral/mistral-large", "", "", "auto"),
    # Bare, nothing pinned: no key to mis-route, so auto-detection answers.
    ("some-unqualified-model", "", "", "auto"),
    ("some-unqualified-model", "", "auto", "auto"),
    # Bare, and the pinned provider serves it: the pin was right.
    ("deepseek-chat", "", "deepseek", "deepseek"),
    # Bare, and the pinned provider does not: neither keeping nor dropping it is
    # safe, so the caller must ask.
    ("some-model-nobody-serves", "", "deepseek", None),
]


#: The vendors these cases assume the user has set up. Everything the table
#: expects to be pinned must be in here, or the rule correctly answers `auto`.
CONFIGURED = {"anthropic", "deepseek", "openai"}


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(pin, "_is_configured", lambda name: name in CONFIGURED)


@pytest.mark.parametrize(("model", "provider", "pinned", "expected"), CASES)
def test_the_rule(model, provider, pinned, expected):
    assert pin.resolve(model, provider=provider, pinned=pinned) == expected


def test_a_vendor_with_no_configuration_is_not_pinned(monkeypatch):
    """The pin is consulted before the gateway fallback, so naming an
    unconfigured vendor stops routing dead. `auto` is the answer that reaches
    whichever gateway is actually serving the model."""
    monkeypatch.setattr(pin, "_is_configured", lambda name: False)

    assert pin.resolve("anthropic/claude-sonnet-5") == pin.AUTO
    # An explicit choice is still the user speaking, and still wins.
    assert pin.resolve("anthropic/claude-sonnet-5", provider="anthropic") == "anthropic"


def test_a_pin_naming_no_known_provider_answers_instead_of_raising():
    """The pinned name is free text from the config file, so it can be a typo.

    ``_is_configured`` takes a registered name and lets an unknown one raise,
    but this one is whatever the file says. A misspelling that propagated as a
    KeyError would abort over a config line rather than report it, so the lookup
    treats an unknown section as an empty one and the answer is None: nothing
    about this model can be told, which is the caller's cue to ask.
    """
    assert pin.resolve("zzz-house-brand-model", provider="", pinned="opemai") is None


def test_a_hand_typed_id_at_a_custom_endpoint_is_not_routed_by_its_name():
    """The pin is what makes an unqualified id safe, so it is not optional.

    The wizard qualifies ids it read from a provider's own ``/v1/models``, but a
    custom endpoint has the user type one, and what they type is the name their
    server answers to -- often a name some other vendor also uses. Routed by
    keyword, "gpt-4o" against a local server reaches OpenAI and spends OpenAI's
    key. The pin decides first, so the section just configured is the one that
    answers, whatever the id happens to be called.
    """
    for typed in ("gpt-4o", "claude-sonnet-5", "my-local-model"):
        assert pin.resolve(typed, provider="custom", pinned="") == "custom", typed


def test_a_local_deployment_keeps_its_pin_without_being_asked():
    """Its server names whatever models it likes and there is no key to mis-route,
    so a bare id under a local pin is that deployment's."""
    from raven.providers.registry import PROVIDERS

    local = next(spec.name for spec in PROVIDERS if spec.is_local)
    assert pin.resolve("whatever-it-serves", pinned=local) == local


# ---------------------------------------------------------------------------
# The property that made this a module: both surfaces write the same pair.
# ---------------------------------------------------------------------------


def _cli_writes(tmp_path: Path, model: str, provider: str, pinned: str) -> tuple[str, str] | None:
    from typer.testing import CliRunner

    from raven.cli.commands import app

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"agents": {"defaults": {"provider": pinned}}}), encoding="utf-8")

    args = ["provider", "use", model] + (["--provider", provider] if provider else [])
    result = CliRunner().invoke(app, args)
    if result.exit_code != 0:
        return None
    defaults = json.loads(config.read_text(encoding="utf-8"))["agents"]["defaults"]
    return defaults["model"], defaults.get("provider", "")


def _tui_writes(tmp_path: Path, model: str, provider: str, pinned: str) -> tuple[str, str] | None:
    from raven.tui_rpc.errors import ConfigValidationError
    from raven.tui_rpc.methods import config as config_methods

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"agents": {"defaults": {"provider": pinned}}}), encoding="utf-8")

    params = {"key": "model", "value": model}
    if provider:
        params["provider"] = provider
    try:
        config_methods._set_model(params, model, None)
    except ConfigValidationError:
        return None
    defaults = json.loads(config.read_text(encoding="utf-8"))["agents"]["defaults"]
    return defaults["model"], defaults.get("provider", "")


@pytest.mark.parametrize(("model", "provider", "pinned", "expected"), CASES)
def test_the_cli_and_the_picker_write_the_same_pair(monkeypatch, tmp_path, model, provider, pinned, expected):
    """Same inputs, same ``(model, provider)`` on disk -- or both refuse.

    Asserted as agreement between the two surfaces rather than each against a
    table, because the failure being prevented is precisely that one of them
    quietly grows a rule the other does not have.
    """
    from raven.config.loader import set_config_path

    path = tmp_path / "config.json"
    set_config_path(path)
    monkeypatch.setattr("raven.tui_rpc.methods.config._config_path", lambda: path)
    monkeypatch.setattr("raven.config.update.get_config_path", lambda: path)

    cli = _cli_writes(tmp_path, model, provider, pinned)
    tui = _tui_writes(tmp_path, model, provider, pinned)

    assert cli == tui, f"{model!r} (provider={provider!r}, pinned={pinned!r}): CLI wrote {cli}, picker wrote {tui}"
    if expected is None:
        assert cli is None, "an id nobody can place must be refused, not written"
    else:
        assert cli is not None and cli[1] == expected


# ---------------------------------------------------------------------------
# Every writer, not just the one that was fixed
# ---------------------------------------------------------------------------


def test_no_surface_writes_the_default_model_without_deciding_its_pin():
    """The hole was fixed at one call site and stayed open at four others.

    ``raven provider use`` was made to write ``agents.defaults.provider``, and
    the warning that a stale pin makes a switch ineffective was deleted on the
    grounds that it had become impossible. It had not: onboarding wrote the model
    through its own helper and left the pin alone, so finishing the wizard on
    Anthropic while DeepSeek was pinned routed Anthropic's model to DeepSeek --
    with DeepSeek's key. A rule enforced at one caller is not enforced.

    Scanned rather than asserted per call site, because the next writer is the
    one nobody thought of -- and **both spellings count**. An earlier version of
    this guard looked only for ``set_default_model`` and was therefore blind to
    ``tui_rpc/methods/config.py``, which writes the same field through
    ``_set_nested`` and happens to be correct.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "raven"
    definition = root / "config" / "update.py"
    offenders: list[str] = []

    for path in sorted(root.rglob("*.py")):
        if path == definition:
            continue  # the function itself
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        # A *write* of the pin, not a mention of it. Keying on the string alone
        # was satisfied by the `_get_nested(payload, "agents.defaults.provider")`
        # read two lines above the write, so this exempted the very file its
        # docstring names -- deleting both pin writes there left it green.
        writes_pin = any(
            isinstance(call, ast.Call)
            and any(isinstance(a, ast.Constant) and a.value == "agents.defaults.provider" for a in call.args)
            and (call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", ""))
            in {"_set_nested", "set_nested"}
            for call in ast.walk(tree)
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")

            if name == "set_default_model":
                if not any(kw.arg == "provider" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(root.parent)}:{node.lineno} (set_default_model)")
                continue

            # The other spelling: a raw nested write of the same key.
            targets = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if "agents.defaults.model" in targets and not writes_pin:
                offenders.append(f"{path.relative_to(root.parent)}:{node.lineno} (raw key write)")

    assert not offenders, (
        "these write the model and leave the pin to whatever it was; decide it with "
        "providers.pin.resolve and write both:\n" + "\n".join(offenders)
    )
