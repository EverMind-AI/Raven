"""Whether a provider is usable, asked of every module that answers it.

Decided in one place, ``providers.auth``. Three modules used to decide it
independently and disagreed:

* ``config.schema._has_credentials`` gates routing -- a section it rejects is
  skipped when matching a model id to a provider.
* ``config.update_providers.list_providers`` gates display -- it is what
  ``raven provider list`` and the pickers show.
* ``cli._helpers.check_provider_credentials`` gates startup -- it decides
  whether ``raven agent`` runs at all.

A provider the second accepted and the first rejected was configured according
to the CLI and invisible to the router -- Gemini holding only ``api_key_list``
read as ready in ``provider list`` and refused to start.

(``registry.credential_kind`` is deliberately absent: it answers what shape a
provider's credentials take, not whether they are present. It is a fourth
implementation of a different question.)

These assert the one answer, and that all three ask it. The per-implementation
records exist so that a change to any single answer is visible rather than
silently rebalancing them back into disagreement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from raven.config.schema import Config
from raven.providers.auth import key_refusal

#: Provider sections paired with the model id that selects them. Each case is a
#: shape of credential material, not a vendor: "the key is in the plural field",
#: "the address is set but no key", "nothing is set at all".
SCENARIOS: dict[str, dict[str, Any]] = {
    "gemini_key_list_only": {
        "provider": "gemini",
        "model": "gemini/gemini-2.5-flash",
        "section": {"apiKeyList": ["AIzaTEST"]},
    },
    "gemini_key_only": {
        "provider": "gemini",
        "model": "gemini/gemini-2.5-flash",
        "section": {"apiKey": "AIzaTEST"},
    },
    "gemini_empty": {
        "provider": "gemini",
        "model": "gemini/gemini-2.5-flash",
        "section": {},
    },
    "anthropic_key": {
        "provider": "anthropic",
        "model": "anthropic/claude-sonnet-5",
        "section": {"apiKey": "sk-ant-TEST"},
    },
    "anthropic_empty": {
        "provider": "anthropic",
        "model": "anthropic/claude-sonnet-5",
        "section": {},
    },
    "azure_key_without_base": {
        "provider": "azure_openai",
        "model": "azure_openai/my-deployment",
        "section": {"apiKey": "az-TEST"},
    },
    "azure_key_and_base": {
        "provider": "azure_openai",
        "model": "azure_openai/my-deployment",
        "section": {"apiKey": "az-TEST", "apiBase": "https://x.openai.azure.com"},
    },
    "ollama_base_only": {
        "provider": "ollama_chat",
        "model": "ollama_chat/llama3.2",
        "section": {"apiBase": "http://localhost:11434"},
    },
    "ollama_empty": {
        "provider": "ollama_chat",
        "model": "ollama_chat/llama3.2",
        "section": {},
    },
    "anthropic_endpoints_only": {
        "provider": "anthropic",
        "model": "anthropic/claude-sonnet-5",
        "section": {"endpoints": [{"label": "primary", "apiKey": "sk-ant-TEST"}]},
    },
    "anthropic_endpoints_all_keys_empty": {
        "provider": "anthropic",
        "model": "anthropic/claude-sonnet-5",
        "section": {"endpoints": [{"label": "primary", "apiKey": ""}, {"label": "backup", "apiKey": ""}]},
    },
}


def _config_file(tmp_path: Path, case: dict[str, Any]) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "providers": {case["provider"]: case["section"]},
                "agents": {"defaults": {"model": case["model"]}},
            }
        ),
        encoding="utf-8",
    )
    return path


def _routing_says(case: dict[str, Any], path: Path) -> bool:
    """Would the router match this model to this provider's section?"""
    config = Config.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return config.get_provider(case["model"]) is not None


def _display_says(case: dict[str, Any], path: Path) -> bool:
    """Would `raven provider list` show this provider as configured?"""
    from raven.config.update_providers import list_providers

    rows = list_providers(config_path=path)
    row = next((r for r in rows if r["name"] == case["provider"]), None)
    return bool(row and row["configured"])


def _startup_says(case: dict[str, Any], path: Path) -> bool:
    """Would `raven agent` start?"""
    from raven.cli._helpers import check_provider_credentials
    from raven.providers.auth import MissingCredentialsError

    config = Config.model_validate(json.loads(path.read_text(encoding="utf-8")))
    try:
        check_provider_credentials(config)
    except MissingCredentialsError:
        return False
    return True


def _status_says(case: dict[str, Any], path: Path) -> bool:
    """Would `raven status` print this provider as set up?

    Driven through the command rather than the helper it calls: this gate is a
    line of formatting logic, and testing the helper would have missed it for
    the same reason the agreement test missed the gate itself.
    """
    from typer.testing import CliRunner

    from raven.cli.commands import app
    from raven.config.loader import set_config_path
    from raven.providers.registry import find_by_name

    set_config_path(path)
    try:
        result = CliRunner().invoke(app, ["status"])
    finally:
        set_config_path(None)  # type: ignore[arg-type]

    spec = find_by_name(case["provider"])
    label = (spec.label if spec else case["provider"]).lower()
    for line in result.stdout.splitlines():
        if line.strip().lower().startswith(label):
            return "not set" not in line
    return False


#: Every surface that decides whether a provider is usable. A gate absent from
#: this map is a gate the agreement test cannot see -- which is how `raven
#: status` and the router's final fallback kept their own rules through a change
#: that claimed to unify them. Adding a gate means adding it here.
ANSWERS = {
    "routing": _routing_says,
    "display": _display_says,
    "startup": _startup_says,
    "status": _status_says,
}


@pytest.mark.parametrize("name", sorted(SCENARIOS), ids=lambda n: n)
def test_every_gate_gives_the_same_verdict(name: str, tmp_path: Path) -> None:
    """One set of credentials, one verdict, whoever is asking.

    Disagreement here is always user-visible: the CLI reports a provider as
    ready that the agent then refuses to start on, or the reverse.
    """
    case = SCENARIOS[name]
    path = _config_file(tmp_path, case)
    verdicts = {who: ask(case, path) for who, ask in ANSWERS.items()}
    assert len(set(verdicts.values())) == 1, f"{name}: {verdicts}"


def test_a_key_in_the_plural_field_is_a_configured_provider(tmp_path: Path) -> None:
    """Gemini accepts a list of keys, and a list with a key in it is credentials.

    Called out separately because it is the case that shipped broken: display
    said yes, routing and startup said no, so the provider appeared configured
    and the agent would not run on it.
    """
    case = SCENARIOS["gemini_key_list_only"]
    path = _config_file(tmp_path, case)
    assert _display_says(case, path)
    assert _routing_says(case, path)
    assert _startup_says(case, path)


def test_a_key_in_an_endpoints_entry_is_a_configured_provider(tmp_path: Path) -> None:
    """An endpoints-only section is exactly as usable as a flat key -- routing and
    startup both read the resolved list (``provider_endpoints``), not the flat
    field, so a gate that only looked at the flat field would reject a section
    its own request path can serve.
    """
    case = SCENARIOS["anthropic_endpoints_only"]
    path = _config_file(tmp_path, case)
    assert _display_says(case, path)
    assert _routing_says(case, path)
    assert _startup_says(case, path)


def test_an_endpoints_list_with_every_key_empty_is_not_configured(tmp_path: Path) -> None:
    case = SCENARIOS["anthropic_endpoints_all_keys_empty"]
    path = _config_file(tmp_path, case)
    assert not _display_says(case, path)
    assert not _routing_says(case, path)
    assert not _startup_says(case, path)


def test_credential_status_ok_for_an_endpoints_only_section() -> None:
    from raven.config.schema import ProviderConfig
    from raven.providers.auth import credential_status

    section = ProviderConfig.model_validate({"endpoints": [{"label": "a", "apiKey": "sk-1"}]})
    assert credential_status("anthropic", section).ok


def test_credential_status_not_ok_when_every_endpoint_key_is_empty() -> None:
    from raven.config.schema import ProviderConfig
    from raven.providers.auth import credential_status

    section = ProviderConfig.model_validate({"endpoints": [{"label": "a", "apiKey": ""}, {"label": "b", "apiKey": ""}]})
    assert not credential_status("anthropic", section).ok


def test_a_provider_whose_key_lives_in_a_list_sends_a_key(tmp_path: Path) -> None:
    """Passing the gate is not enough; the request has to carry a credential.

    Gemini accepts several keys under one section. Reading ``api_key`` directly
    at the call site sent an empty string for a section holding only the list --
    a provider that every check called configured, failing at the API instead of
    at startup, which is the worst of both.
    """
    case = SCENARIOS["gemini_key_list_only"]
    path = _config_file(tmp_path, case)
    config = Config.model_validate(json.loads(path.read_text(encoding="utf-8")))

    provider = config.get_provider(case["model"])
    assert provider is not None
    assert provider.effective_api_key == "AIzaTEST"
    assert config.get_api_key(case["model"]) == "AIzaTEST"


def test_only_the_auth_module_decides_configuredness_from_a_key() -> None:
    """No surface may read a key off a provider section to decide if it is set up.

    Six surfaces did, with six rules, and the divergence was invisible because
    each looked reasonable alone.

    Matched on the syntax tree rather than on a line pattern, and on three
    spellings of the read -- see ``key_reads`` for why the net is this wide.
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "raven"

    # Every entry is argued, because an unargued allowlist is the line-pattern
    # guard again with extra steps.
    allowed = {
        # Not an LLM provider section: a tool's own key (deep research, media
        # generation, web search), the router's, or EverOS's.
        "raven/agent/loop/main.py",
        "raven/agent/tools/deep_research.py",
        "raven/agent/tools/media_gen.py",
        "raven/agent/tools/web.py",
        "raven/cli/agent_commands.py",
        "raven/cli/deep_research_commands.py",
        "raven/cli/gateway_commands.py",
        "raven/cli/tui_commands.py",
        "raven/config/update_everos.py",
        "raven/config/update_tools.py",
        "raven/providers/transcription.py",
        # Reads a key in order to *use* it -- put it on the request, redact it
        # for display, rotate it -- rather than to rule on whether a provider is
        # set up.
        "raven/config/schema.py",
        "raven/config/update_providers.py",
        "raven/providers/litellm_provider.py",
        "raven/cli/_helpers.py",
        "raven/cli/onboard_commands.py",
        # Carries the wizard's EverOS cluster split out of onboard_commands --
        # same reads, same argument, new file name.
        "raven/cli/onboard_everos.py",
        # The connection-material reading layer itself: resolves flat fields,
        # api_key_list and endpoints into one list for whoever sends requests.
        # Configuredness still rules through auth, which consults this shape
        # via its own _present.
        "raven/providers/endpoints.py",
        "raven/cli/provider_commands.py",
        "raven/cli/status_commands.py",
        "raven/tui_rpc/methods/model.py",
        "raven/tui_rpc/methods/setup.py",
        "raven/providers/azure_openai_provider.py",
        "raven/providers/base.py",
        "raven/providers/minimax_oauth_provider.py",
        "raven/providers/per_model_provider.py",
        # Other subsystems' credentials entirely: the skill hub, the evolver's
        # judge, the EverOS memory backend, an embedding script.
        "raven/config/update.py",
        "raven/context_engine/factory.py",
        "raven/evolver/judge/llm_client.py",
        "raven/plugin/memory/everos/backend.py",
        "raven/routing/generate_embeddings.py",
    }

    names = {"api_key", "api_key_list", "apiKey", "apiKeyList"}

    def key_reads(tree: ast.AST) -> list[int]:
        """Every read of a credential field, in any of its three spellings.

        Deliberately not narrowed to "reads in a truthiness context": every
        recognizer of that context misses a shape -- an attribute read on a
        passthrough section, `v.get("apiKey")` on a raw payload, the same call
        inside a comprehension.

        So it flags the read and the allowlist carries the argument. A file that
        legitimately touches a key says why, once, here -- which is a claim a
        reviewer can check, unlike a pattern's silence.
        """
        found: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in names:
                found.append(node.lineno)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and arg.value in names:
                    found.append(node.lineno)
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                if node.slice.value in names:
                    found.append(node.lineno)
        return found

    offenders = sorted(
        f"{path.relative_to(root.parent)}:{line}"
        for path in root.rglob("*.py")
        if str(path.relative_to(root.parent)) not in allowed
        for line in key_reads(ast.parse(path.read_text()))
    )
    assert not offenders, "decide configuredness through providers.auth.credential_status: " + ", ".join(offenders)


#: The six vendors issue #254 identified as unconfigurable by a bare key --
#: each needs credential material the onboarding wizard's generic single-key
#: prompt has no field for.
_KEY_REFUSED_VENDORS = ("chatgpt", "bedrock", "sagemaker", "vertex_ai", "azure", "cloudflare")


@pytest.mark.parametrize("vendor", _KEY_REFUSED_VENDORS)
def test_key_refusal_names_a_reason_for_vendors_a_key_cannot_configure(vendor: str) -> None:
    reason = key_refusal(vendor)
    assert reason is not None
    assert reason.strip()


def test_key_refusal_chatgpt_points_at_ravens_own_oauth_path() -> None:
    """chatgpt is the one vendor where a *different* Raven path already exists."""
    reason = key_refusal("chatgpt")
    assert reason is not None
    assert "openai-codex" in reason or "openai_codex" in reason


@pytest.mark.parametrize("vendor", ["gigachat", "openai", "anthropic", "custom", "deepseek"])
def test_key_refusal_is_none_for_vendors_a_key_configures(vendor: str) -> None:
    """Everyone else -- including gigachat, whose key merely has an odd shape."""
    assert key_refusal(vendor) is None


def test_key_refusal_normalizes_hyphen_and_case() -> None:
    """Matched the same way every other provider-name comparison is made."""
    assert key_refusal("Vertex-AI") == key_refusal("vertex_ai")
    assert key_refusal("BEDROCK") == key_refusal("bedrock")
