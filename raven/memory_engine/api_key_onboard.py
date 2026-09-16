"""The ``raven onboard`` screen of a hosted backend: one key, one probe.

One class serves any :class:`HttpMemoryBackend`; a plugin's factory names which. The screen
asks for the key unless the shell already exports it, proves it with the
backend's own ``health()``, and records the slice under the backend's name
through ``set_plugin_config_fields`` -- a merge, so configuring one service
never touches another's key. A key that came from the environment is not
copied into the file: the file records only what the user typed here.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

import httpx

from raven.memory_engine.http_backend import HttpMemoryBackend
from raven.plugins import OnboardUI, PluginContext, StepOutcome


class ApiKeyOnboardStep:
    """The ``onboard`` contribution for one hosted backend."""

    def __init__(
        self,
        ctx: PluginContext,
        backend_cls: type[HttpMemoryBackend],
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._ctx = ctx
        self._cls = backend_cls
        # Tests hand the probe a transport; the wizard lets the backend open its own.
        self._client_factory = client_factory

    def run(
        self,
        ui: OnboardUI,
        *,
        step_no: int,
        non_interactive: bool,
        main_model: str | None,
        warnings: list[str],
        skip_test: bool,
    ) -> StepOutcome:
        cls = self._cls
        ui.step_header(step_no, ui.t("Long-term memory: {name}", name=cls.NAME))
        from_env = bool(os.environ.get(cls.ENV_KEY))
        while True:
            if from_env:
                ui.console.print(ui.t("  [dim]Using the API key from {var}.[/dim]", var=cls.ENV_KEY))
                api_key = os.environ[cls.ENV_KEY]
            else:
                ui.console.print(ui.t("  [dim]Keys: {url}[/dim]", url=cls.SIGNUP_URL))
                api_key = ui.prompt_api_key(cls.NAME, allow_back=True)
                if api_key is ui.back:
                    return StepOutcome.BACK
            if not skip_test:
                health = self._probe(api_key)
                if health is None or not health.ready:
                    hint = health.checks[0].hint if health and health.checks else ""
                    ui.console.print(
                        ui.t("  [yellow]✗ Couldn't verify {name}: {detail}[/yellow]", name=cls.NAME, detail=hint)
                    )
                    options = [(ui.t("Skip long-term memory"), "skip")]
                    if not from_env:
                        options.insert(0, (ui.t("Re-enter"), "rekey"))
                    choice = ui.failure_choice(options, non_interactive=non_interactive)
                    if choice == "rekey":
                        continue
                    return StepOutcome.DISABLED
                ui.console.print(ui.t("  [green]✓ {name} connected.[/green]", name=cls.NAME))
            self._record(api_key if not from_env else None)
            return StepOutcome.CONFIGURED

    def configured(self) -> bool:
        return bool(os.environ.get(self._cls.ENV_KEY) or self._ctx.config.get("api_key"))

    def _probe(self, api_key: str):
        # A backend built on this key alone, so the probe answers for what is
        # about to be recorded rather than for whatever the slice held before.
        config = {**self._ctx.config, "api_key": api_key}
        ctx = PluginContext(config=config, services=self._ctx.services, logger=self._ctx.logger)
        client = self._client_factory() if self._client_factory else None
        backend = self._cls(ctx, client=client)
        try:
            return asyncio.run(self._health_then_stop(backend))
        except Exception:  # noqa: BLE001 - a probe that cannot run reads as "not verified"
            return None

    @staticmethod
    async def _health_then_stop(backend: HttpMemoryBackend):
        try:
            return await backend.health()
        finally:
            await backend.stop()

    def _record(self, api_key: str | None) -> None:
        from raven.config.update import set_plugin_config_fields

        fields: dict[str, Any] = {"base_url": self._ctx.config.get("base_url") or self._cls.DEFAULT_BASE_URL}
        if api_key:
            fields["api_key"] = api_key
        set_plugin_config_fields(self._cls.NAME, fields)


__all__ = ["ApiKeyOnboardStep"]
