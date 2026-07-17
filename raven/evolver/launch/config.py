"""Run-spec loading: YAML -> validated RunSpec (+ --smoke overlay).

The YAML shape:

    bench: appworld
    repo_root: /path/to/subject          # the repo being evolved
    base_sha: <commit>                   # optional; omitted -> repo_root HEAD at launch
    work_dir: ./evo_work

    models:                              # optional; omitted -> raven's own model
      driver:  {provider: claude_cli, model: claude-haiku-4-5}
      design:  {provider: claude_cli, model: claude-opus-4-8}
      verdict: {provider: openai_compat, base_url: ..., model: ...}

    funnel:                              # optional; SOP-aligned defaults
      k_screen: 1
      k_confirm: 3
      budget:      {max_why_per_round: 2, candidates_per_why: 3}
      termination: {patience: 10, max_rounds: 20}
      anchor:      {n_sentinel: 12, cull_sigma_mult: 1.5}

    bench_config: {...}                  # schema owned by the bench entry

    smoke: {...}                         # optional deep-merge overlay for --smoke

``--smoke`` applies built-in shrink defaults (1 WHY x 1 candidate x 1 round,
K=1) first, then the user's ``smoke:`` section on top, and suffixes work_dir
with ``_smoke`` so a smoke run never touches the real run's state.
"""

from __future__ import annotations

import copy
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from raven.evolver.orchestrator.config import (
    AnchorParams,
    Budget,
    OrchestratorConfig,
    Termination,
)

SMOKE_BUILTIN: dict = {
    "funnel": {
        "k_confirm": 1,
        "budget": {"max_why_per_round": 1, "candidates_per_why": 1, "recombinations_per_round": 0},
        "termination": {"patience": 1, "max_rounds": 1},
    },
}


class RunSpecError(ValueError):
    """A config file problem the user must fix; message says exactly what."""


def _redact_secrets(models: dict) -> dict:
    if not isinstance(models, dict):
        return models
    out = {}
    for role, spec in models.items():
        if isinstance(spec, dict):
            out[role] = {k: ("<redacted>" if "key" in k.lower() and k != "api_key_env" else v) for k, v in spec.items()}
        else:
            out[role] = spec
    return out


def deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass
class RunSpec:
    bench: str
    repo_root: Path
    base_sha: str
    work_dir: Path
    funnel: OrchestratorConfig
    models: dict = field(default_factory=dict)
    bench_config: dict = field(default_factory=dict)
    smoke: bool = False
    base_sha_defaulted: bool = False
    config_dir: Path = field(default_factory=Path.cwd)
    raw: dict = field(default_factory=dict)

    def snapshot(self) -> dict:
        """The effective configuration recorded in run_meta (drift guard).

        Secrets are redacted before they reach disk — work dirs get shared —
        and the redaction is a constant, so rotating a key does not trip the
        config-drift guard.
        """
        return {
            "bench": self.bench,
            "repo_root": str(self.repo_root),
            "base_sha": self.base_sha,
            "models": _redact_secrets(self.raw.get("models", {})),
            "funnel": self.raw.get("funnel", {}),
            "bench_config": self.raw.get("bench_config", {}),
            "smoke": self.smoke,
        }


def _build_funnel(repo_root: Path, work_dir: Path, funnel: dict) -> OrchestratorConfig:
    if not isinstance(funnel, dict):
        raise RunSpecError(f"funnel: must be a mapping, got {type(funnel).__name__}")
    known = {"k_screen", "k_confirm", "anchor", "budget", "termination", "sealed_test_split"}
    unknown = set(funnel) - known
    if unknown:
        raise RunSpecError(f"funnel: unknown keys {sorted(unknown)}")
    try:
        cfg = OrchestratorConfig(
            repo_root=repo_root,
            work_dir=work_dir,
            driver_llm_spec={},
            k_screen=int(funnel.get("k_screen", 1)),
            k_confirm=int(funnel.get("k_confirm", 3)),
            anchor=AnchorParams(**(funnel.get("anchor") or {})),
            budget=Budget(**(funnel.get("budget") or {})),
            termination=Termination(**(funnel.get("termination") or {})),
            sealed_test_split=funnel.get("sealed_test_split", "test"),
            sealed_output_dir=work_dir / "sealed",
        )
    except (TypeError, ValueError) as exc:
        raise RunSpecError(f"funnel: {exc}") from exc
    if cfg.k_screen < 1 or cfg.k_confirm < 1:
        raise RunSpecError("funnel: k_screen and k_confirm must be >= 1")
    if cfg.budget.max_why_per_round < 1 or cfg.budget.candidates_per_why < 1:
        raise RunSpecError("funnel: budget.max_why_per_round and budget.candidates_per_why must be >= 1")
    if cfg.termination.patience < 1 or cfg.termination.max_rounds < 1:
        raise RunSpecError("funnel: termination.patience and termination.max_rounds must be >= 1")
    return cfg


def load_run_spec(config_path: str | Path, *, smoke: bool = False) -> RunSpec:
    path = Path(config_path)
    if not path.is_file():
        raise RunSpecError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise RunSpecError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RunSpecError(f"{path}: top level must be a mapping")

    if smoke:
        overlay = data.pop("smoke", {}) or {}
        data = deep_merge(deep_merge(data, SMOKE_BUILTIN), overlay)
    else:
        data.pop("smoke", None)

    missing = [k for k in ("bench", "repo_root", "work_dir") if not data.get(k)]
    if missing:
        raise RunSpecError(f"{path}: missing required keys: {missing}")

    def _abs(value: str) -> Path:
        # Relative paths resolve against the config file, not the CWD — a
        # resume from another directory must find the same work_dir, not
        # silently start a fresh run.
        p = Path(value).expanduser()
        return p if p.is_absolute() else (path.parent / p).resolve()

    repo_root = _abs(data["repo_root"])
    if not (repo_root / ".git").exists():
        raise RunSpecError(f"repo_root is not a git checkout: {repo_root}")

    base_sha = str(data.get("base_sha") or "").strip()
    base_sha_defaulted = not base_sha
    if base_sha_defaulted:
        base_sha = _resolve_head(repo_root)
        data["base_sha"] = base_sha
    work_dir = _abs(data["work_dir"])
    if smoke:
        work_dir = work_dir.with_name(work_dir.name + "_smoke")

    models = data.get("models") or {}
    if not isinstance(models, dict):
        raise RunSpecError("models: must be a mapping of role -> provider spec")
    unknown_roles = set(models) - {"driver", "design", "verdict"}
    if unknown_roles:
        raise RunSpecError(f"models: unknown roles {sorted(unknown_roles)}")
    for role, spec_val in models.items():
        if not isinstance(spec_val, dict):
            raise RunSpecError(f"models.{role}: must be a mapping (provider/model/...), got {type(spec_val).__name__}")

    return RunSpec(
        bench=str(data["bench"]),
        repo_root=repo_root,
        base_sha=base_sha,
        work_dir=work_dir,
        funnel=_build_funnel(repo_root, work_dir, data.get("funnel") or {}),
        models=models,
        bench_config=data.get("bench_config") or {},
        smoke=smoke,
        base_sha_defaulted=base_sha_defaulted,
        config_dir=path.parent.resolve(),
        raw=data,
    )


def _resolve_head(repo_root: Path) -> str:
    """Resolve the subject repo's HEAD when the yaml omits ``base_sha``.

    Resolved to a full sha at load time and recorded in the config snapshot,
    so the run stays pinned to the commit HEAD pointed at when it started —
    resuming after the repo gained commits trips the drift guard instead of
    silently moving the root.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RunSpecError(f"git is not runnable ({exc}) — install git") from exc
    if proc.returncode != 0:
        raise RunSpecError(f"base_sha omitted and resolving HEAD of {repo_root} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


__all__ = ["RunSpec", "RunSpecError", "load_run_spec", "deep_merge", "SMOKE_BUILTIN"]
