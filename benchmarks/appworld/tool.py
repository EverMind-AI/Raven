"""The single agent tool for AppWorld: execute Python in the AppWorld REPL.

AppWorld is pinned to pydantic v1 (via sqlmodel) while Raven needs pydantic
v2, so the two cannot share a venv. We therefore run AppWorld as its own HTTP
``environment`` server (``appworld serve environment``, in the AppWorld venv) and
talk to it over HTTP from here — this module never imports ``appworld``.

This tool routes the agent's code to ``POST {env_url}/execute`` (the stateful
in-server world REPL): variables, imports and logins persist across calls. One
env server holds ONE world at a time, so the batch runner gives each concurrent
task its own server/port.

This file is deliberately plain vanilla: it adds nothing to the model's inputs
or outputs beyond the REPL round-trip. It sits inside the evolution whitelist,
so improvements to it are the evolver's job — shipping pre-built assists here
would let a candidate "win" by switching them on.
"""

from __future__ import annotations

import asyncio
from typing import Any

import requests

from raven.agent.tools.base import Tool


class AppWorldExecuteTool(Tool):
    def __init__(self, env_url: str, task_id: str, timeout: float = 180.0) -> None:
        self._env_url = env_url.rstrip("/")
        self._task_id = task_id
        self._timeout = timeout
        self.saw_complete_task = False

    @property
    def name(self) -> str:
        return "execute"

    @property
    def description(self) -> str:
        return (
            "Execute Python code in the AppWorld environment and return its stdout. "
            "Call app APIs via apis.<app>.<api>(...). State (variables, imports, logins) "
            "persists across calls like a Jupyter notebook, so build up the solution "
            "incrementally. You MUST print() anything you want to see — only stdout is "
            "returned. Discover APIs with apis.api_docs.show_api_descriptions(app_name=...) "
            "and apis.api_docs.show_api_doc(app_name=..., api_name=...). When the task is "
            "done call apis.supervisor.complete_task(answer=...) exactly once."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to run in the stateful AppWorld REPL.",
                }
            },
            "required": ["code"],
        }

    def _post_execute(self, code: str) -> str:
        if "complete_task" in code:
            self.saw_complete_task = True
        resp = requests.post(
            f"{self._env_url}/execute",
            json={"task_id": self._task_id, "code": code},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json().get("output", "")

    async def execute(self, code: str) -> str:
        # Blocking HTTP -> run off the event loop so the loop stays responsive.
        return await asyncio.to_thread(self._post_execute, code)
