"""The seam through which a plugin contributes a screen to ``raven onboard``.

The host owns the wizard shell (console, translation, prompt helpers, the
back sentinel) and what only the host knows about providers (the lender of an
already-configured provider's credentials, and what the main chat model
resolves to), and hands the lot over as one ``OnboardUI``. The plugin owns
the screen's content. The plugin never writes ``memory.backend``: it returns a
``StepOutcome`` and the host records the choice, so the host's config key
stays the host's.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class StepOutcome(Enum):
    """What one onboard screen decided, for the host to record.

    The host records ``CONFIGURED`` as ``memory.backend = <contribution name>``
    and ``DISABLED`` as ``memory.backend = None``.
    """

    CONFIGURED = "configured"
    DISABLED = "disabled"
    BACK = "back"


@dataclass(frozen=True)
class OnboardUI:
    """The wizard shell, lent to a plugin's screen for the duration of one run."""

    console: Any
    t: Callable[..., str]
    require_questionary: Callable[[], Any]
    qmark: str
    back: object
    step_header: Callable[[int, str], None]
    failure_choice: Callable[..., str]
    back_placeholder: Callable[..., Any]
    prompt_api_key: Callable[..., Any]
    style: Any
    lend_provider_credentials: Callable[[str], dict[str, str]]
    keep_provider_credentials: Callable[..., None]
    """The mirror of ``lend_provider_credentials``: put a key on file.

    A screen that collects a credential for a shared setting has to leave it
    where the host looks, or the setting it then records points at a provider
    the host cannot reach. Lent rather than done by the plugin because where
    credentials live is the host's business."""
    resolve_main_model: Callable[[str], dict[str, Any]]
    set_embedding_endpoint: Callable[[dict[str, Any]], None]
    """Record an embedding endpoint in the host's own config.

    Lent because the endpoint is not the backend's to keep: a knowledge base
    reads the same block, and a screen that wrote it into its own file left the
    host's empty -- so the operator configured it here and every other reader
    still had to fall back. A backend that wants an endpoint of its own writes
    that where it keeps its own settings; this is the shared one."""


class OnboardStep(Protocol):
    """What a ``[[plugin.contributes.onboard]]`` factory returns."""

    def run(
        self,
        ui: OnboardUI,
        *,
        step_no: int,
        non_interactive: bool,
        main_model: str | None,
        warnings: list[str],
        skip_test: bool,
    ) -> StepOutcome: ...

    def configured(self) -> bool: ...


__tier__ = "contract"
__all__ = ["OnboardStep", "OnboardUI", "StepOutcome"]
