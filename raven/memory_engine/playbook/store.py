"""Persistence: PlaybookSpec <-> one ``playbook.md`` per directory.

Layout, per the agreed field definition:

    <root>/<name>/playbook.md        # frontmatter + human body + fenced block
    <root>/<name>/.provenance.json   # generator-side sidecar (status/provenance)

``playbook.md`` has three regions: a two-field frontmatter (name /
description — all an index needs), a human-readable body the machine never
parses, and one fenced ``yaml playbook-spec`` block holding every machine
field. Being under the scan root *is* the discriminator — no marker fields.

The sidecar carries what the field definition gives no home to but the
generation lifecycle needs (draft gating, the immutable region for revise).
A hand-written playbook without a sidecar loads as ``status: ready`` — a
human putting a file in the directory is the human review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from raven.memory_engine.playbook.types import PlaybookSpec, Provenance

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_BLOCK_RE = re.compile(r"```yaml\s+playbook-spec\n(.*?)\n```", re.DOTALL)


class PlaybookExistsError(FileExistsError):
    """Raised when save() would clobber an existing playbook."""


class PlaybookStore:
    """One directory of playbook directories."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def path_for(self, name: str) -> Path:
        return self._root / name / "playbook.md"

    def _sidecar_for(self, name: str) -> Path:
        return self._root / name / ".provenance.json"

    def list_ids(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(p.parent.name for p in self._root.glob("*/playbook.md"))

    def save(self, spec: PlaybookSpec, *, overwrite: bool = False) -> Path:
        """Write one playbook (file + sidecar); returns the playbook.md path.

        Raises:
            `PlaybookExistsError`:
                The name exists and ``overwrite`` is false — regeneration
                must go through revise, not clobber.
        """
        path = self.path_for(spec.name)
        if path.exists() and not overwrite:
            raise PlaybookExistsError(
                f"playbook {spec.name!r} already exists at {path}; revise it instead of regenerating"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(spec), encoding="utf-8")
        self._sidecar_for(spec.name).write_text(
            json.dumps(
                {"status": spec.status, "provenance": spec.provenance.model_dump(by_alias=True)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def load(self, name: str) -> PlaybookSpec:
        text = self.path_for(name).read_text(encoding="utf-8")
        fm_match = _FRONTMATTER_RE.match(text)
        if fm_match is None:
            raise ValueError(f"playbook {name!r}: no frontmatter in playbook.md")
        front = yaml.safe_load(fm_match.group(1)) or {}
        block_match = _BLOCK_RE.search(text)
        if block_match is None:
            raise ValueError(f"playbook {name!r}: no '```yaml playbook-spec' block in playbook.md")
        data = yaml.safe_load(block_match.group(1)) or {}
        data["name"] = front.get("name")
        data["description"] = front.get("description")
        if data["name"] != name:
            raise ValueError(f"playbook {name!r}: frontmatter name {data['name']!r} != directory name")

        sidecar = self._sidecar_for(name)
        if sidecar.exists():
            side = json.loads(sidecar.read_text(encoding="utf-8"))
            data["status"] = side.get("status", "ready")
            data["provenance"] = side.get("provenance") or {}
        else:
            data["status"] = "ready"
        return PlaybookSpec.model_validate(data)


def _render(spec: PlaybookSpec) -> str:
    # Serialized, not interpolated: ``load`` parses this region with
    # ``yaml.safe_load``, and ``description`` is model-written prose where a
    # colon or a leading ``#`` is ordinary. Interpolating produced files that
    # could not be read back -- ``description: 尽调时: 先扫描`` is a YAML mapping
    # error, and a leading ``#`` silently became a comment. The wide ``width``
    # keeps a long description on one line, so the region also stays readable
    # to a line-oriented parser.
    front = yaml.safe_dump(
        {"name": spec.name, "description": spec.description},
        allow_unicode=True,
        sort_keys=False,
        width=10**6,
    )
    block = yaml.safe_dump(spec.block_dump(), allow_unicode=True, sort_keys=False, width=100)
    return f"---\n{front}---\n\n{_body(spec)}\n```yaml playbook-spec\n{block}```\n"


def _body(spec: PlaybookSpec) -> str:
    """The human-readable region — informational only, never parsed."""
    lines = [f"# {spec.name}", "", spec.description, ""]
    if spec.params:
        lines.append("参数：" + "、".join(f"{k}（{v.description}）" for k, v in spec.params.items()))
    if spec.nodes:
        lines.append("步骤：" + " → ".join(n.id for n in spec.nodes))
    lines.append("")
    return "\n".join(lines)


def load_sidecar_provenance(root: Path, name: str) -> Provenance | None:
    """Read just the sidecar, for tooling that only needs the lifecycle."""
    path = root / name / ".provenance.json"
    if not path.exists():
        return None
    side = json.loads(path.read_text(encoding="utf-8"))
    return Provenance.model_validate(side.get("provenance") or {})
