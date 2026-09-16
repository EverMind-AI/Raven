"""Unit tests for ``LiteLLMProvider._resolve_model`` across every provider spec.

A config model name is ``<provider config section>/<name the endpoint knows>``.
LiteLLM's own format is ``<its route>/<name the endpoint knows>`` and it consumes
exactly that first segment before issuing the request, so the two heads are
different namespaces that only coincide for some providers. The expectations
below are written out per provider rather than derived from the registry, so a
spec change that alters what reaches an endpoint has to be acknowledged here.
"""

import pytest

from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.registry import PROVIDERS

# provider section -> the model name that provider's own endpoint knows.
BARE_MODEL = {
    "custom": "MiniMax-M3",
    "azure_openai": "gpt-4o",
    "openrouter": "openai/gpt-5.6-terra",
    "aihubmix": "gpt-4o",
    "siliconflow": "Qwen/Qwen3.5-9B",
    "volcengine": "doubao-pro-32k",
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5.5",
    "openai_codex": "gpt-5-codex",
    "github_copilot": "gpt-4o",
    "deepseek": "deepseek-v4-flash",
    "gemini": "gemini-2.5-flash",
    "zai": "glm-4.6",
    "dashscope": "qwen-plus",
    "moonshot": "kimi-k2",
    "nvidia_nim": "nemotron-3-super-120b-a12b",
    "minimax": "minimax-m1",
    "minimax_cn_api": "MiniMax-M3",
    "minimax_global": "MiniMax-M2",
    "minimax_cn": "MiniMax-M2",
    "hosted_vllm": "Qwen3.5-9B",
    "lm_studio": "qwen3-8b",
    "ollama_chat": "llama3.2",
    "groq": "openai/gpt-oss-120b",
    "xai": "grok-4.6",
    "mistral": "mistral-large-latest",
    "together_ai": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "fireworks_ai": "accounts/fireworks/models/kimi-k2-instruct",
    "perplexity": "sonar-pro",
    "cerebras": "gpt-oss-120b",
    "huggingface": "deepseek-ai/DeepSeek-V4-Pro",
    "poe": "anthropic/claude-opus-4.8",
    "xiaomi_mimo": "mimo-v2.5",
    "baichuan": "Baichuan4-Turbo",
    "baidu_cloud": "ernie-5.1",
    "stepfun": "step-3.7-flash",
    "longcat": "longcat-2.0",
    "modelscope": "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "qiniu": "deepseek-v3",
    "ai302": "gpt-5.5",
    "dmxapi": "claude-opus-4.8",
    "burncloud": "gpt-5.5",
    "ocoolai": "gpt-5.5",
    "ppio": "deepseek/deepseek-v3",
    "lanyun": "deepseek-v3",
    "alayanew": "deepseek-v3",
    "sophnet": "DeepSeek-V3",
    "tokenhub": "deepseek-v3",
    "xirang": "deepseek-v3",
    "ph8": "gpt-5.5",
    "aionly": "gpt-5.5",
    "radeon_cloud": "deepseek-v3",
    "gpustack": "qwen3-8b",
    "ovms": "qwen3-8b",
    "bigmodel": "glm-4.6",
}

# What `<section>/<bare>` must resolve to. The head is LiteLLM's route; whatever
# follows it is sent upstream verbatim, so no entry here may repeat the section
# name after the first segment.
QUALIFIED_RESOLUTION = {
    "custom": "openai/MiniMax-M3",
    "azure_openai": "azure_openai/gpt-4o",
    "openrouter": "openrouter/openai/gpt-5.6-terra",
    "aihubmix": "openai/gpt-4o",
    "siliconflow": "openai/Qwen/Qwen3.5-9B",
    "volcengine": "volcengine/doubao-pro-32k",
    "anthropic": "anthropic/claude-sonnet-5",
    "openai": "openai/gpt-5.5",
    "openai_codex": "openai_codex/gpt-5-codex",
    "github_copilot": "github_copilot/gpt-4o",
    "deepseek": "deepseek/deepseek-v4-flash",
    "gemini": "gemini/gemini-2.5-flash",
    "zai": "zai/glm-4.6",
    "dashscope": "dashscope/qwen-plus",
    "moonshot": "moonshot/kimi-k2",
    "nvidia_nim": "nvidia_nim/nemotron-3-super-120b-a12b",
    "minimax": "minimax/minimax-m1",
    "minimax_cn_api": "minimax/MiniMax-M3",
    # MiniMax Global/CN speak Anthropic's API, so the route is the driver's.
    "minimax_global": "anthropic/MiniMax-M2",
    "minimax_cn": "anthropic/MiniMax-M2",
    "hosted_vllm": "hosted_vllm/Qwen3.5-9B",
    "lm_studio": "lm_studio/qwen3-8b",
    "ollama_chat": "ollama_chat/llama3.2",
    "groq": "groq/openai/gpt-oss-120b",
    "xai": "xai/grok-4.6",
    "mistral": "mistral/mistral-large-latest",
    # LiteLLM routes these two on the underscored name, which is not the
    # hyphenated spelling a picker offers -- both reach the same section.
    "together_ai": "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "fireworks_ai": "fireworks_ai/accounts/fireworks/models/kimi-k2-instruct",
    "perplexity": "perplexity/sonar-pro",
    "cerebras": "cerebras/gpt-oss-120b",
    "huggingface": "huggingface/deepseek-ai/DeepSeek-V4-Pro",
    "poe": "poe/anthropic/claude-opus-4.8",
    "xiaomi_mimo": "xiaomi_mimo/mimo-v2.5",
    # The six CN vendors LiteLLM has no driver for speak OpenAI's API, so the
    # route is that driver's and the address in config is what distinguishes
    # them -- the same shape SiliconFlow and AiHubMix resolve to above.
    "baichuan": "openai/Baichuan4-Turbo",
    "baidu_cloud": "openai/ernie-5.1",
    "stepfun": "openai/step-3.7-flash",
    "longcat": "openai/longcat-2.0",
    "modelscope": "openai/Qwen/Qwen3-235B-A22B-Instruct-2507",
    "qiniu": "openai/deepseek-v3",
    # Thirteen resale gateways, all reached through OpenAI's driver: the route
    # is that driver's and the address in config is what tells them apart. The
    # id after it is the shelf's own spelling of somebody else's model and is
    # sent verbatim -- 302.AI's "gpt-5.5" is not OpenAI's, and is never treated
    # as OpenAI's despite the prefix, because the api_base decides where it goes.
    "ai302": "openai/gpt-5.5",
    "dmxapi": "openai/claude-opus-4.8",
    "burncloud": "openai/gpt-5.5",
    "ocoolai": "openai/gpt-5.5",
    "ppio": "openai/deepseek/deepseek-v3",
    "lanyun": "openai/deepseek-v3",
    "alayanew": "openai/deepseek-v3",
    "sophnet": "openai/DeepSeek-V3",
    "tokenhub": "openai/deepseek-v3",
    "xirang": "openai/deepseek-v3",
    "ph8": "openai/gpt-5.5",
    "aionly": "openai/gpt-5.5",
    "radeon_cloud": "openai/deepseek-v3",
    # Two self-hosted servers with no LiteLLM driver, so the same borrowed
    # route -- and the address, supplied per deployment, is the whole of what
    # sends the call to a box on the LAN instead of to OpenAI.
    "gpustack": "openai/qwen3-8b",
    "ovms": "openai/qwen3-8b",
    # Zhipu's CN platform rides the same driver as Z.ai, so the route is
    # that vendor's and the address in config is what picks the platform.
    "bigmodel": "zai/glm-4.6",
}

# What a bare name resolves to. Pinned so that adding the qualified-name path
# cannot regress the callers that only ever had a bare name (CLI -m, hand-edited
# config, an extension's own model field). A bare name carries no section, so the
# route comes from whichever vendor the keywords name -- which is why the entries
# here can differ in vendor from the section they are listed under.
BARE_RESOLUTION = {
    "custom": "openai/MiniMax-M3",
    "azure_openai": "openai/gpt-4o",
    "openrouter": "openrouter/openai/gpt-5.6-terra",
    "aihubmix": "openai/gpt-4o",
    "siliconflow": "openai/Qwen/Qwen3.5-9B",
    "volcengine": "volcengine/doubao-pro-32k",
    "anthropic": "anthropic/claude-sonnet-5",
    "openai": "openai/gpt-5.5",
    "openai_codex": "openai/gpt-5-codex",
    "github_copilot": "openai/gpt-4o",
    "deepseek": "deepseek/deepseek-v4-flash",
    "gemini": "gemini/gemini-2.5-flash",
    "zai": "zai/glm-4.6",
    "dashscope": "dashscope/qwen-plus",
    "moonshot": "moonshot/kimi-k2",
    "nvidia_nim": "nvidia_nim/nemotron-3-super-120b-a12b",
    "minimax": "minimax/minimax-m1",
    "minimax_cn_api": "minimax/MiniMax-M3",
    "minimax_global": "minimax/MiniMax-M2",
    "minimax_cn": "minimax/MiniMax-M2",
    "hosted_vllm": "hosted_vllm/Qwen3.5-9B",
    "lm_studio": "lm_studio/qwen3-8b",
    "ollama_chat": "ollama_chat/llama3.2",
    "groq": "openai/gpt-oss-120b",
    "xai": "xai/grok-4.6",
    "mistral": "mistral/mistral-large-latest",
    # Together and Fireworks name their models with the weight vendor's path,
    # so a bare id has a head that claims nobody and is sent as written. That
    # is the safe outcome: prefixing it would hand the call to whichever vendor
    # the path happens to name.
    "together_ai": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "fireworks_ai": "accounts/fireworks/models/kimi-k2-instruct",
    "perplexity": "perplexity/sonar-pro",
    # Like Groq's entry above: the id names OpenAI's open-weights model and
    # nothing in it names Cerebras, so a bare spelling routes by that keyword.
    "cerebras": "openai/gpt-oss-120b",
    # The weight vendor's path again, claiming nobody, so it is sent as written.
    "huggingface": "deepseek-ai/DeepSeek-V4-Pro",
    # Poe is a gateway, and that is the whole point of the flag here: the id
    # names Anthropic, and only the gateway rule keeps a Poe subscription from
    # being billed as a call to Anthropic with Anthropic's key.
    "poe": "poe/anthropic/claude-opus-4.8",
    "xiaomi_mimo": "xiaomi_mimo/mimo-v2.5",
    "baichuan": "openai/Baichuan4-Turbo",
    "baidu_cloud": "openai/ernie-5.1",
    # No keyword of StepFun's appears in its own model ids, so a bare one names
    # nobody and is sent unprefixed rather than claimed by a vendor it does not
    # belong to -- the same outcome as Together's and Hugging Face's above.
    "stepfun": "step-3.7-flash",
    "longcat": "openai/longcat-2.0",
    "modelscope": "openai/Qwen/Qwen3-235B-A22B-Instruct-2507",
    "qiniu": "openai/deepseek-v3",
    # A gateway prefixes a bare id with its own route, so these answer the
    # same either way -- which is the protection: the id names Anthropic or
    # OpenAI, and neither one's key is what pays for it here.
    "ai302": "openai/gpt-5.5",
    "dmxapi": "openai/claude-opus-4.8",
    "burncloud": "openai/gpt-5.5",
    "ocoolai": "openai/gpt-5.5",
    "ppio": "openai/deepseek/deepseek-v3",
    "lanyun": "openai/deepseek-v3",
    "alayanew": "openai/deepseek-v3",
    "sophnet": "openai/DeepSeek-V3",
    "tokenhub": "openai/deepseek-v3",
    "xirang": "openai/deepseek-v3",
    "ph8": "openai/gpt-5.5",
    "aionly": "openai/gpt-5.5",
    "radeon_cloud": "openai/deepseek-v3",
    "gpustack": "openai/qwen3-8b",
    "ovms": "openai/qwen3-8b",
    # A GLM id names its vendor either way, so both spellings agree here --
    # which is why the platform has to be chosen by address, not by id.
    "bigmodel": "zai/glm-4.6",
}

SPEC_NAMES = [spec.name for spec in PROVIDERS]


def _provider(section: str) -> LiteLLMProvider:
    """Build a provider for ``section`` without touching ``os.environ``.

    ``api_key=None`` skips ``_setup_env``; ``find_gateway`` still identifies a
    gateway/local spec from ``provider_name`` alone, which is the only part of
    the instance ``_resolve_model`` reads.
    """
    return LiteLLMProvider(api_key=None, provider_name=section)


def test_expectation_tables_cover_every_registered_provider() -> None:
    """A new spec must arrive with its own expectations, not silently skip them."""
    assert set(BARE_MODEL) == set(SPEC_NAMES)
    assert set(QUALIFIED_RESOLUTION) == set(SPEC_NAMES)
    assert set(BARE_RESOLUTION) == set(SPEC_NAMES)


@pytest.mark.parametrize("section", SPEC_NAMES)
def test_qualified_model_name_resolves_to_expected_litellm_name(section: str) -> None:
    qualified = f"{section}/{BARE_MODEL[section]}"

    assert _provider(section)._resolve_model(qualified) == QUALIFIED_RESOLUTION[section]


@pytest.mark.parametrize("section", SPEC_NAMES)
def test_bare_model_name_resolution_is_unchanged(section: str) -> None:
    assert _provider(section)._resolve_model(BARE_MODEL[section]) == BARE_RESOLUTION[section]


@pytest.mark.parametrize("section", SPEC_NAMES)
def test_config_section_head_never_reaches_the_endpoint(section: str) -> None:
    """The part after LiteLLM's route is sent upstream verbatim, so a surviving
    section name becomes a model the endpoint has never heard of -- the
    ``custom/MiniMax-M3`` failure this guards against."""
    qualified = f"{section}/{BARE_MODEL[section]}"

    resolved = _provider(section)._resolve_model(qualified)
    _, _, sent_upstream = resolved.partition("/")

    assert not sent_upstream.startswith(f"{section}/")


@pytest.mark.parametrize("section", SPEC_NAMES)
@pytest.mark.parametrize("form", ["qualified", "bare"])
def test_resolve_model_is_idempotent(section: str, form: str) -> None:
    """Resolution runs per call, and a resolved name is a legal input again
    (a session stores what it resolved). Applying it twice must not stack
    prefixes."""
    bare = BARE_MODEL[section]
    model = f"{section}/{bare}" if form == "qualified" else bare
    provider = _provider(section)

    once = provider._resolve_model(model)

    assert provider._resolve_model(once) == once


@pytest.mark.parametrize(
    ("section", "litellm_native"),
    [
        ("hosted_vllm", "hosted_vllm/Qwen3.5-9B"),
        ("ollama_chat", "ollama_chat/llama3.2"),
        ("custom", "openai/MiniMax-M3"),
        ("siliconflow", "openai/Qwen/Qwen3.5-9B"),
        ("openrouter", "openrouter/openai/gpt-5.6-terra"),
        ("volcengine", "volcengine/doubao-pro-32k"),
    ],
)
def test_litellm_route_head_is_left_alone(section: str, litellm_native: str) -> None:
    """A name already in LiteLLM's own form is passed through: the head is the
    route, not a config section, so it must not be rewritten or duplicated."""
    assert _provider(section)._resolve_model(litellm_native) == litellm_native
