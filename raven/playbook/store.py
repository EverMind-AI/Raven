"""Persistence: PlaybookSpec <-> one ``playbook.md`` per directory.

Layout, per the agreed field definition:

    <root>/<name>/playbook.md        # one directory, one file, nothing else

``playbook.md`` has three regions: a two-field frontmatter (name /
description — all an index needs), a human-readable body the machine never
parses, and one fenced ``yaml playbook-spec`` block holding every machine
field. Being under the scan root *is* the discriminator — no marker fields,
no sidecar. A generator's open questions and assumptions go into the body
(the ``## Open questions`` section) for a human to read; whether the
playbook is matchable on this machine lives in config
(``playbooks.disabled``), never in the file — the file is the distribution
unit.

The library is two such roots layered: the builtin root ships inside the
package (read-only by construction — no write path targets it), the user
root lives under agent home and is where every save lands. A user directory
with a builtin's name shadows the builtin: same command surface, the user's
file wins, and the load path says so once per shadowed name.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from loguru import logger

from raven.playbook.types import NAME_RE, PlaybookSpec

#: Playbooks that ship with the package. Kept next to the code so the
#: package-data glob picks the directories up; may not exist in a source
#: checkout that has none yet.
BUILTIN_ROOT = Path(__file__).parent / "builtin"

PlaybookOrigin = Literal["builtin", "user"]

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_BLOCK_RE = re.compile(r"```yaml\s+playbook-spec\n(.*?)\n```", re.DOTALL)


class PlaybookExistsError(FileExistsError):
    """Raised when save() would clobber an existing playbook."""


class PlaybookStore:
    """The two-layer playbook library: a writable user root over a builtin root.

    ``root`` is the user layer. ``builtin_root`` defaults to the packaged
    library; tests pass their own. Reads resolve user-first; writes go to the
    user layer only, so the builtin layer cannot be modified through this
    class at all.
    """

    def __init__(self, root: Path, *, builtin_root: Path | None = None) -> None:
        self._root = root
        self._builtin_root = BUILTIN_ROOT if builtin_root is None else builtin_root
        self._shadow_warned: set[str] = set()

    def path_for(self, name: str) -> Path:
        """The playbook.md that ``load`` would read: user layer first."""
        user = self._root / name / "playbook.md"
        if user.exists():
            return user
        builtin = self._builtin_root / name / "playbook.md"
        if builtin.exists():
            return builtin
        return user

    def origin_of(self, name: str) -> PlaybookOrigin | None:
        """Which layer serves ``name`` (None = not in the library)."""
        if (self._root / name / "playbook.md").exists():
            return "user"
        if (self._builtin_root / name / "playbook.md").exists():
            return "builtin"
        return None

    def is_shadowing(self, name: str) -> bool:
        """Whether a user playbook hides a builtin of the same name."""
        return (self._root / name / "playbook.md").exists() and (self._builtin_root / name / "playbook.md").exists()

    def list_ids(self) -> list[str]:
        return sorted(self._layer_ids(self._root) | self._layer_ids(self._builtin_root))

    @staticmethod
    def _layer_ids(root: Path) -> set[str]:
        if not root.is_dir():
            return set()
        return {p.parent.name for p in root.glob("*/playbook.md")}

    def save(self, spec: PlaybookSpec, *, notes: list[str] | None = None, overwrite: bool = False) -> Path:
        """Write one playbook into the user layer; returns the playbook.md path.

        ``notes`` are the generator's review lines (open questions,
        assumptions, missing capabilities) — rendered into the human body
        for review, never into the machine block.

        Always writes under the user root — with ``overwrite`` over a builtin
        name that creates a shadow, it never touches the builtin file.

        Raises:
            `PlaybookExistsError`:
                The name exists in either layer and ``overwrite`` is false —
                regeneration must go through revise, not clobber (nor
                silently shadow a builtin).
        """
        # Checked at the write, not only in the schema: the name is the
        # directory the file lands in, and both creation entries reach here
        # with a name that skipped pydantic (``model_copy`` runs no
        # validators; the tool argument validator has no ``pattern`` branch).
        # Without this, ``../x`` or an absolute name writes outside the
        # library root.
        if not re.fullmatch(NAME_RE, spec.name or ""):
            raise ValueError(f"playbook name {spec.name!r} must match {NAME_RE}")
        if self.origin_of(spec.name) is not None and not overwrite:
            raise PlaybookExistsError(
                f"playbook {spec.name!r} already exists at {self.path_for(spec.name)};"
                " revise it instead of regenerating"
            )
        path = self._root / spec.name / "playbook.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(spec, notes or []), encoding="utf-8")
        return path

    def load(self, name: str) -> PlaybookSpec:
        if self.is_shadowing(name) and name not in self._shadow_warned:
            # Once per store instance, not per load: the runtime reloads the
            # library each loop start and the point is a hint, not a nag.
            self._shadow_warned.add(name)
            logger.warning(
                "User playbook {!r} shadows the builtin of the same name; the user file wins",
                name,
            )
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
        return PlaybookSpec.model_validate(data)


def _render(spec: PlaybookSpec, notes: list[str]) -> str:
    # Serialized, not interpolated: ``load`` parses this region with
    # ``yaml.safe_load``, and ``description`` is model-written prose where a
    # colon or a leading ``#`` is ordinary. Interpolating produced files that
    # could not be read back -- ``description: due diligence: scan first`` is
    # a YAML mapping error, and a leading ``#`` silently became a comment. The
    # wide ``width``
    # keeps a long description on one line, so the region also stays readable
    # to a line-oriented parser.
    front = yaml.safe_dump(
        {"name": spec.name, "description": spec.description},
        allow_unicode=True,
        sort_keys=False,
        width=10**6,
    )
    block = yaml.safe_dump(spec.block_dump(), allow_unicode=True, sort_keys=False, width=100)
    return f"---\n{front}---\n\n{_body(spec, notes)}\n```yaml playbook-spec\n{block}```\n"


def _body(spec: PlaybookSpec, notes: list[str]) -> str:
    """The human-readable region — informational only, never parsed."""
    lines = [f"# {spec.name}", "", spec.description, ""]
    if spec.params:
        lines.append("Params: " + ", ".join(f"{k} ({v.description})" for k, v in spec.params.items()))
    if spec.nodes:
        lines.append("Steps: " + " -> ".join(n.id for n in spec.nodes))
    if notes:
        lines += ["", "## Open questions", ""]
        lines += [f"- {note}" for note in notes]
    lines.append("")
    return "\n".join(lines)
