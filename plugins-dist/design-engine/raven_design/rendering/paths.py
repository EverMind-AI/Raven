"""Enforce input and output path boundaries for rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from raven_design.rendering.models import RenderError


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


@dataclass(frozen=True)
class RenderPathPolicy:
    workspace: Path
    media_root: Path
    runtime_root: Path
    restrict_to_workspace: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", self.workspace.expanduser().resolve())
        object.__setattr__(self, "media_root", self.media_root.expanduser().resolve())
        object.__setattr__(self, "runtime_root", self.runtime_root.expanduser().resolve())

    @property
    def input_roots(self) -> tuple[Path, ...]:
        return (self.workspace, self.media_root)

    @property
    def output_roots(self) -> tuple[Path, ...]:
        return (self.workspace, self.runtime_root)

    def resolve_source(self, value: str | Path) -> Path:
        candidate = self._from_workspace(value)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise RenderError("file_not_found", "The source file does not exist.") from exc
        if not resolved.is_file():
            raise RenderError("unsafe_input", "The source must be a regular file.")
        if self.restrict_to_workspace and not _is_within(resolved, self.input_roots):
            raise RenderError("path_not_allowed", "The source is outside the allowed roots.")
        return resolved

    def resolve_asset_root(self, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        candidate = self._from_workspace(value)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise RenderError("path_not_allowed", "asset_root does not exist.") from exc
        if not resolved.is_dir():
            raise RenderError("path_not_allowed", "asset_root must be a directory.")
        self._reject_broad(resolved)
        if self.restrict_to_workspace and not _is_within(resolved, self.input_roots):
            raise RenderError("path_not_allowed", "asset_root is outside the allowed roots.")
        return resolved

    def resolve_output_dir(self, value: str | Path, *, internal: bool = False) -> Path:
        candidate = self._from_workspace(value)
        resolved = candidate.resolve(strict=False)
        self._reject_broad(resolved)
        if resolved.exists() and not resolved.is_dir():
            raise RenderError("path_not_allowed", "The output path is not a directory.")
        if self.restrict_to_workspace:
            roots = self.output_roots if internal else (self.workspace,)
            if not _is_within(resolved, roots):
                raise RenderError("path_not_allowed", "The output directory is outside the allowed root.")
        return resolved

    def _from_workspace(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else self.workspace / candidate

    @staticmethod
    def _reject_broad(path: Path) -> None:
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise RenderError("path_not_allowed", "The path is too broad.")
