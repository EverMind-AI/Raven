"""The full-auto launch face; see the class docstring for why it stays off the menu."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from oncall_flow.tools.ops import _campaign_dir
from raven.contracts.tool import Tool


class OpsTuneLaunchTool(Tool):
    """Launch a detached, adaptive tuning campaign on a remote Docker host.

    Deliberately NOT a manifest contribution (fork parity): with the turnkey
    launch on the menu the model never steers round by round, so the
    agent-in-the-loop path stays ops_submit + ops_check_later + ops_tune_status
    and full-auto is reached by whoever constructs this face on purpose --
    the module entry (``python -m oncall_flow.tune``, D5) is the same
    machinery without the detach.
    """

    def __init__(self, llm_base_url: str | None = None, llm_model: str | None = None) -> None:
        self._llm_base_url = llm_base_url or ""
        self._llm_model = llm_model or ""

    def _endpoint(self) -> tuple[str, str]:
        """The proposer LLM (base_url, model), constructor-supplied.

        The fork fell back to reading the host config; a plugin holds no host
        config, so whoever constructs this face names the endpoint (the plugin
        slice is where a product would put it).
        """
        return self._llm_base_url, self._llm_model

    @property
    def name(self) -> str:
        return "ops_tune_launch"

    @property
    def description(self) -> str:
        return (
            "Launch a long-running adaptive tuning campaign on a remote Docker host and "
            "return immediately with a handle. Each round an LLM proposes hyperparameter "
            "configs, each config is scored in a container, and the best is kept. The "
            "campaign runs detached and survives this session (durable via a ledger), so "
            "use ops_tune_status with the returned ledger path to check progress. Use this "
            "when the user asks to tune / search / optimize hyperparameters on a remote machine."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Remote host running Docker (IP or name)."},
                "port": {"type": "integer", "description": "SSH port (default 22)."},
                "key": {"type": "string", "description": "SSH private key path (default ~/.ssh/id_rsa)."},
                "objective": {
                    "type": "string",
                    "description": "What to optimize, in words, handed to the proposer LLM.",
                },
                "remote_dir": {"type": "string", "description": "Remote working dir (default /root/raven-ops)."},
                "image": {"type": "string", "description": "Container image (default python:3.12-slim)."},
                "app_dir": {"type": "string", "description": "Local trial dir to sync (default benchmarks/ops_bm25)."},
                "k1": {"type": "array", "items": {"type": "number"}, "description": "Seed BM25 k1 values (round 0)."},
                "b": {"type": "array", "items": {"type": "number"}, "description": "Seed BM25 b values (round 0)."},
                "metric": {"type": "string", "description": "Metric to optimize (default ndcg)."},
                "goal": {"type": "string", "enum": ["max", "min"], "description": "max or min (default max)."},
                "max_rounds": {"type": "integer", "description": "Max proposer rounds (default 5)."},
            },
            "required": ["host"],
        }

    async def execute(
        self,
        host: str,
        port: int = 22,
        key: str = "~/.ssh/id_rsa",
        objective: str = "Tune BM25 k1 and b to maximize nDCG@10 on the corpus.",
        remote_dir: str = "/root/raven-ops",
        image: str = "python:3.12-slim",
        app_dir: str = "benchmarks/ops_bm25",
        k1: list[float] | None = None,
        b: list[float] | None = None,
        metric: str = "ndcg",
        goal: str = "max",
        max_rounds: int = 5,
        **kwargs: Any,
    ) -> str:
        llm_base_url, llm_model = self._endpoint()
        if not (llm_base_url and llm_model):
            return (
                "Cannot launch: no LLM endpoint is configured for the proposer. "
                "Configure a chat provider (base URL + model) first."
            )
        cdir = _campaign_dir(host, objective)
        cdir.mkdir(parents=True, exist_ok=True)
        ledger_path = cdir / "ledger.json"
        log_path = cdir / "run.log"

        argv = [
            sys.executable,
            "-m",
            "oncall_flow.tune",
            "--adaptive",
            "--host",
            host,
            "--port",
            str(port),
            "--key",
            key,
            "--remote-dir",
            remote_dir,
            "--image",
            image,
            "--app-dir",
            app_dir,
            "--metric",
            metric,
            "--goal",
            goal,
            "--max-rounds",
            str(max_rounds),
            "--objective",
            objective,
            "--llm-base-url",
            llm_base_url,
            "--llm-model",
            llm_model,
            "--ledger",
            str(ledger_path),
        ]
        for v in k1 or [0.6, 1.0, 1.4]:
            argv += ["--k1", str(v)]
        for v in b or [0.3, 0.6, 0.9]:
            argv += ["--b", str(v)]

        log_fh = open(log_path, "w")  # noqa: SIM115 -- handed to the detached child; closed on its exit
        # The fork anchored cwd at its repo root so the default app_dir resolved;
        # the module entry resolves app_dir itself, so the child inherits ours.
        proc = subprocess.Popen(
            argv,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return (
            f"Launched adaptive tuning campaign (pid {proc.pid}) on {host}.\n"
            f"objective: {objective}\n"
            f"ledger: {ledger_path}\n"
            f"log: {log_path}\n"
            f"It runs detached and survives this session. Check progress with "
            f"ops_tune_status(ledger='{ledger_path}')."
        )
