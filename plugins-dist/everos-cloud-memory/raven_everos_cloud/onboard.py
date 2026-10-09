"""The ``raven onboard`` screen for EverOS Cloud: one key, one probe.

The screen asks for the key unless the file or the environment already holds
one, proves it with the backend's own ``health()``, and records a typed key
under this plugin's slice through ``set_plugin_config_fields`` -- a merge, so
it never touches another plugin's slice. A key that came from the environment
is not copied into the file: the file records only what a person typed here.

The one sentence beyond the key: memories are shared by every Raven that uses
this key under the same ``memory.userId``, which is the host's field, not this
plugin's -- so the hint names it and offers no control for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from raven.config import update as config_update
from raven.plugins import OnboardUI, PluginContext, StepOutcome
from raven_everos_cloud.backend import ENV_KEY, KEYS_URL, PLUGIN_ID, EverosCloudBackend, resolve_api_key


class CloudKeyScreen:
    """The ``onboard`` contribution: step 4 when `everos-cloud` is the chosen backend."""

    def __init__(self, ctx: PluginContext, *, client_factory: Callable[[], httpx.AsyncClient] | None = None) -> None:
        self._ctx = ctx
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
        ui.step_header(step_no, ui.t("Long-term memory: EverOS Cloud"))
        key, source = resolve_api_key(self._ctx.config)
        while True:
            if source == "env":
                ui.console.print(ui.t("  [dim]Using the API key from {var}.[/dim]", var=ENV_KEY))
            elif source == "file":
                ui.console.print(ui.t("  [dim]Using the API key on file.[/dim]"))
            else:
                ui.console.print(ui.t("  [dim]Keys: {url}[/dim]", url=KEYS_URL))
                typed = ui.prompt_api_key("EverOS Cloud", allow_back=True)
                if typed is ui.back:
                    return StepOutcome.BACK
                key, source = str(typed), "typed"
            ui.console.print(
                ui.t(
                    "  [dim]Memories are shared by every Raven that uses this key under user id {uid}; "
                    "change memory.userId in config.json to keep devices apart.[/dim]",
                    uid=self._ctx.services.user_id,
                )
            )
            if not skip_test:
                health = self._probe(key)
                if health is None or not health.ready:
                    hint = health.checks[0].hint if health and health.checks else ""
                    ui.console.print(
                        ui.t("  [yellow]✗ Couldn't verify EverOS Cloud: {detail}[/yellow]", detail=hint or "")
                    )
                    options = [(ui.t("Skip long-term memory"), "skip")]
                    if source != "env":
                        options.insert(0, (ui.t("Re-enter"), "rekey"))
                    if ui.failure_choice(options, non_interactive=non_interactive) == "rekey":
                        key, source = "", None
                        continue
                    return StepOutcome.DISABLED
                ui.console.print(ui.t("  [green]✓ EverOS Cloud connected.[/green]"))
            if source == "typed":
                config_update.set_plugin_config_fields(PLUGIN_ID, {"api_key": key})
            return StepOutcome.CONFIGURED

    def configured(self) -> bool:
        return bool(resolve_api_key(self._ctx.config)[0])

    def _probe(self, api_key: str) -> Any:
        # A backend built on this key alone, so the probe answers for what is
        # about to be recorded rather than for whatever the slice held before.
        ctx = PluginContext(
            config={**self._ctx.config, "api_key": api_key},
            services=self._ctx.services,
            logger=self._ctx.logger,
        )
        client = self._client_factory() if self._client_factory else None
        backend = EverosCloudBackend(ctx, client=client)
        try:
            return asyncio.run(self._health_then_stop(backend))
        except Exception:  # noqa: BLE001 - a probe that cannot run reads as "not verified"
            return None

    @staticmethod
    async def _health_then_stop(backend: EverosCloudBackend) -> Any:
        try:
            return await backend.health()
        finally:
            await backend.stop()


def make_onboard_step(ctx: PluginContext) -> CloudKeyScreen:
    return CloudKeyScreen(ctx)
