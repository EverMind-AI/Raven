"""Load .env files into ``os.environ``.

Lookup order (first hit per key wins; existing ``os.environ`` always takes
precedence over files):

  1. ``$PROACTIVITY_EVAL_ENV_FILE`` (explicit override)
  2. ``<cwd>/.env``
  3. ``<repo-root>/.env`` (static relative to this package)
  4. ``<proactivity-eval>/.env``
  5. ``<hermes-home>/.env`` (legacy Hermes creds location)

Subprocesses (hermes, openclaw) inherit ``os.environ``, so loading once in
the parent is enough to propagate ``VLLM_BASE_URL`` / ``VLLM_MODEL_ID`` /
``JUDGE_*`` to every backend.
"""

from __future__ import annotations

import os
from pathlib import Path

from .hermes_home import _hermes_home_dir

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent  # proactivity-eval/
_REPO_ROOT = _PACKAGE_ROOT.parent


def _parse_env_file(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out.append((k, v))
    return out


def _candidate_env_files() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("PROACTIVITY_EVAL_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.cwd() / ".env")
    candidates.append(_REPO_ROOT / ".env")
    candidates.append(_PACKAGE_ROOT / ".env")
    try:
        candidates.append(_hermes_home_dir() / ".env")
    except Exception:
        pass

    seen: set[Path] = set()
    dedup: list[Path] = []
    for p in candidates:
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        if rp in seen:
            continue
        seen.add(rp)
        dedup.append(p)
    return dedup


def load_dotenvs() -> list[Path]:
    """Load all reachable .env files. Returns the list that were found."""
    loaded: list[Path] = []
    for path in _candidate_env_files():
        if not path.exists():
            continue
        for k, v in _parse_env_file(path):
            os.environ.setdefault(k, v)
        loaded.append(path)
    return loaded


__all__ = ["load_dotenvs"]
