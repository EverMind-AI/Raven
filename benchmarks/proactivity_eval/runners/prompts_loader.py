"""Load YAML prompt definitions and render placeholders via str.format.

Prompt YAML shape:
    system: |
      <system prompt text with {placeholder} markers>
    user: |
      <user message text with {placeholder} markers>

Rendering rules:
- Standard Python str.format() — {name} substituted from kwargs.
- Literal '{' or '}' in the template must be escaped as '{{' or '}}'.
- Missing placeholder at render time raises KeyError (fail fast — don't let
  a silently-empty placeholder poison an eval run).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_prompt(path: Path | str) -> dict[str, str]:
    """Read a prompt YAML file and return its {system, user} templates.

    Templates retain their {placeholder} markers — no substitution yet.
    """
    path = Path(path)
    data: Any = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping; got {type(data).__name__}")
    for key in ("system", "user"):
        if key not in data:
            raise KeyError(f"{path} missing required key: {key}")
        if not isinstance(data[key], str):
            raise TypeError(f"{path} key '{key}' must be a string")
    return {"system": data["system"], "user": data["user"]}


def render(template: dict[str, str], **kwargs: Any) -> dict[str, str]:
    """Fill placeholders in system + user strings.

    Uses str.format, so placeholders look like {name} and literal braces
    must be escaped as {{ / }} in the template source.
    """
    return {k: v.format(**kwargs) for k, v in template.items()}


__all__ = ["load_prompt", "render"]
