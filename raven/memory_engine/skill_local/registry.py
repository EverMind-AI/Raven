"""SkillRegistry — data layer for skills.

Pure IO + frontmatter parsing + dependency checking. No rendering, no
retrieval logic. Ported from the pre-refactor ``agent/skills.py``, with
three-layer pool semantics (workspace > external > builtin) and the same
three-namespace metadata lookup (``raven > nanobot > openclaw``).

Layers (highest priority first):

  workspace : ``<workspace>/skills/``     — user's session/project pool
  external  : ``<skills_dir>/``           — user's curated library
                                            (e.g. mirror of skill_library
                                             output, mounted via
                                             ``config.skill_forge.skills_dir``)
  builtin   : packaged ``raven/skills/`` — ships with the install

Disk layout supported per layer (auto-detected per top-level dir):

  Flat (legacy):
    <root>/<skill>/SKILL.md
        → source = layer label (``workspace`` / ``external`` / ``builtin``)

  Nested (mirror from skill_library):
    <root>/<source>/<skill>/SKILL.md
        → source = ``<source>`` (e.g. ``anthropics``, ``antigravity``,
          ``awesome:foo/bar``)

External callers should go through :class:`SkillService`; this module
is an internal data layer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path

log = logging.getLogger(__name__)

from raven.memory_engine.skill_local.types import SkillMeta

# Default builtin skills directory — mirrors the path used by the legacy
# ``SkillsLoader`` so that replacing it is a drop-in change.
#
# Resolves to ``raven/memory_engine/skills/`` — the built-in markdown
# library lives under the memory_engine package alongside the skill code
# itself.
_DEFAULT_BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillRegistry:
    """Read-only view of the on-disk skill pool.

    Two-layer discovery: ``workspace/skills/`` takes precedence over the
    packaged ``builtin`` directory when names collide.
    """

    def __init__(
        self,
        workspace: Path,
        builtin_skills_dir: Path | None = None,
        external_skills_dir: Path | None = None,
        extra_dirs: "list[tuple[Path, str, bool]] | None" = None,
        scan_max_depth: int = 5,
    ):
        """
        Args:
            extra_dirs: R1 multi-directory support. Each tuple is
                ``(path, name, always_enabled)``. Later entries override
                earlier on name collision. ``external_skills_dir`` is a
                legacy parameter — prepended to ``extra_dirs``.
            scan_max_depth: R2 max recursion depth for SKILL.md scanning.
        """
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self._scan_max_depth = scan_max_depth
        try:
            self.workspace_skills.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._extra_dirs: list[tuple[Path, str, bool]] = []
        if external_skills_dir is not None:
            self._extra_dirs.append((external_skills_dir, "external", False))
        if extra_dirs:
            self._extra_dirs.extend(extra_dirs)
        self.builtin_skills = builtin_skills_dir or _DEFAULT_BUILTIN_SKILLS_DIR
        self._metas_cache: list[SkillMeta] | None = None
        # Primary key: (source, name). One entry per physical skill, mirror
        # entries with colliding names across sources are kept distinct here.
        self._by_full_key: dict[tuple[str, str], SkillMeta] | None = None
        # Secondary index: name → first-priority meta (workspace > external >
        # builtin > other sources alphabetical). For legacy callers that
        # don't carry a source.
        self._by_name: dict[str, SkillMeta] | None = None
        # Sources that need a partial rescan on the next ``list_all``.
        # Empty set + ``_metas_cache`` not None ⇒ cache is fresh.
        # Populated by :meth:`invalidate_source` so that small mutations
        # (e.g. an everos add) don't drop the whole cache.
        self._dirty_sources: set[str] = set()
        # Serializes cache reads/writes against the background
        # SkillFileWatcher thread. Without it, a watcher-driven
        # ``invalidate_source`` landing mid-rebuild would set a flag
        # that ``list_all``'s tail then clears, losing the update.
        # RLock so a future caller can compose ``invalidate_*`` inside
        # a locked section without deadlocking.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        """Drop the entire cache so the next ``list_all`` re-scans disk.

        Hard reset: all sources rebuild. Prefer :meth:`invalidate_source`
        when only one logical source changed (typical: everos skill
        add / update / retire) — it skips re-tokenizing the much larger
        builtin / workspace / external sets.
        """
        with self._lock:
            self._metas_cache = None
            self._by_full_key = None
            self._by_name = None
            self._dirty_sources.clear()

    def invalidate_source(self, source: str) -> None:
        """Mark a single source dirty; next ``list_all`` rebuilds only
        that source's slice and merges it back into the existing cache.

        Triggered by :meth:`SkillService.invalidate_skill_cache(source)`
        on every EverOSEvolver write so newly materialized
        ``everos`` SKILL.md files surface to the in-process
        selector without restart, while builtin / workspace / external
        scans are spared.

        No-op when there is no cache yet — the first ``list_all`` will
        do a full scan anyway.
        """
        with self._lock:
            if self._metas_cache is None:
                return
            self._dirty_sources.add(source)

    def resolve_source_for_path(self, path: Path) -> str | None:
        """Map a SKILL.md path back to its source label.

        Mirrors :meth:`_iter_skill_dirs` rules so a filesystem-watcher
        event can be routed to the right :meth:`invalidate_source`
        without re-walking the tree:

          - ``<workspace_skills>/<skill>/SKILL.md`` → ``"workspace"``
          - ``<workspace_skills>/<src>/.../SKILL.md`` → ``<src>``
          - same for ``external_skills`` / ``builtin_skills`` with the
            corresponding layer-label default

        Returns ``None`` when the path lives outside every known layer
        root, or directly at a layer root (the iterator skips those).

        Both ``path`` and the layer roots are passed through
        ``Path.resolve(strict=False)`` before comparison. This matters
        on macOS where ``/var/...`` and ``/tmp/...`` are symlinks to
        ``/private/var/...`` and ``/private/tmp/...`` — watchfiles
        reports the realpath form, while a caller's root may still be
        the symlinked form (or vice versa). Without normalisation
        ``relative_to`` would silently miss every event.

        ``strict=False`` makes resolve a no-op for the missing tail of
        a path, so this also works for delete events whose target file
        is already gone.
        """
        try:
            resolved_path = path.resolve(strict=False)
        except OSError:
            resolved_path = path
        layers: list[tuple[Path | None, str]] = [
            (self.workspace_skills, "workspace"),
        ]
        for ed_path, ed_label, _ in self._extra_dirs:
            layers.append((ed_path, ed_label))
        layers.append((self.builtin_skills, "builtin"))
        for root, default in layers:
            if root is None:
                continue
            try:
                resolved_root = root.resolve(strict=False)
            except OSError:
                resolved_root = root
            try:
                rel = resolved_path.parent.relative_to(resolved_root)
            except ValueError:
                continue
            parts = rel.parts
            if not parts:
                return None  # SKILL.md directly at root — iterator skips it
            return default if len(parts) == 1 else parts[0]
        return None

    def list_all(self) -> list[SkillMeta]:
        """All visible skills (compound (source, name) identity). Cached.

        Cross-source name collisions are preserved — both metas appear in
        the list. Within a single (source, name), the first physical path
        wins (later disk-redundant copies are skipped).

        Layer priority (workspace > builtin) only affects the secondary
        ``_by_name`` lookup when callers omit ``source``; the full list
        always contains every entry.

        When ``_dirty_sources`` is non-empty (typically a single source
        flagged by ``invalidate_source``), only those sources are
        rescanned and merged into the existing cache — saves the cost
        of re-tokenizing the unchanged builtin / workspace / external
        slices.

        Holds :attr:`_lock` for the full rebuild so a concurrent
        watcher-thread :meth:`invalidate_source` cannot race the cache
        tail-clear and lose the flag. Lock contention is negligible —
        invalidations are infrequent and rebuilds short.
        """
        with self._lock:
            return self._list_all_locked()

    def _list_all_locked(self) -> list[SkillMeta]:
        if self._metas_cache is not None and not self._dirty_sources:
            return self._metas_cache

        # Carry over still-valid cached entries (i.e. sources NOT in
        # ``_dirty_sources``). Full rebuild path treats every source as
        # dirty, which ``filter_in`` below rejects → starts empty.
        if self._metas_cache is not None:
            kept = [
                m for m in self._metas_cache
                if m.source not in self._dirty_sources
            ]
            wanted_dirty = self._dirty_sources
        else:
            kept = []
            wanted_dirty = None  # full scan: keep every source we find

        metas: list[SkillMeta] = list(kept)
        full_key: dict[tuple[str, str], SkillMeta] = {
            (m.source, m.name): m for m in kept
        }
        # Physical-directory dedupe runs on the directory name (a stable
        # key, possibly numeric for everos); ``full_key`` is keyed
        # by (source, display name) so by-name lookups still work when
        # display name diverges from dir name (everos writes
        # frontmatter name independently of the autoincrement-id dir).
        seen_dirs: set[tuple[str, str]] = {(m.source, m.path.parent.name) for m in kept}

        # Walk layers: workspace → extra_dirs (list order) → builtin.
        # R1: later extra_dirs override earlier on name collision.
        layers: list[tuple[Path, str, bool]] = [
            (self.workspace_skills, "workspace", True),
        ]
        for path, name, always_enabled in self._extra_dirs:
            layers.append((path, name, always_enabled))
        layers.append((self.builtin_skills, "builtin", True))

        for root, layer_label, layer_always_enabled in layers:
            if root is None or not root.exists():
                continue
            for skill_dir, source in self._iter_skill_dirs(
                root, default_source=layer_label,
                max_depth=self._scan_max_depth,
            ):
                if wanted_dirty is not None and source not in wanted_dirty:
                    continue
                dir_key = (source, skill_dir.name)
                if dir_key in seen_dirs:
                    continue
                seen_dirs.add(dir_key)
                meta = self._build_meta(
                    skill_dir, source,
                    always_enabled=layer_always_enabled,
                )
                if meta is None:
                    continue
                full_key.setdefault((source, meta.name), meta)
                metas.append(meta)

        # Build _by_name: later-mounted layers override earlier (R1).
        # Iterate in layer order (workspace → extra → builtin); within
        # metas the iteration already follows this order. Last-write-wins
        # among extra_dirs, but workspace always wins (comes first in
        # layers so it's overwritten by extra? No — workspace is the
        # user's own skills and must have highest priority).
        #
        # Strategy: iterate low-to-high priority so last write wins.
        # Priority: builtin < extra_dirs[0] < ... < extra_dirs[-1] < workspace.
        n_extra = len(self._extra_dirs)
        prio: dict[str, int] = {"builtin": 0}
        for i, (_, label, _) in enumerate(self._extra_dirs):
            prio[label] = i + 1
        prio["workspace"] = n_extra + 1

        by_name: dict[str, SkillMeta] = {}
        for m in sorted(metas, key=lambda x: (prio.get(x.source, 0), x.source)):
            prev = by_name.get(m.name)
            if prev is not None and prev.source != m.source:
                log.warning(
                    "Skill '%s' from '%s' shadowed by '%s'",
                    m.name, prev.source, m.source,
                )
            by_name[m.name] = m  # last write wins

        # R2: startup log
        source_counts: dict[str, int] = {}
        for m in metas:
            source_counts[m.source] = source_counts.get(m.source, 0) + 1
        parts_str = " ".join(
            f"{s}={c}" for s, c in sorted(source_counts.items())
        )
        log.info(
            "LocalPool loaded: %s (total=%d)", parts_str, len(metas),
        )

        self._metas_cache = metas
        self._by_full_key = full_key
        self._by_name = by_name
        self._dirty_sources.clear()
        return metas

    def get(self, name: str, source: str | None = None) -> SkillMeta | None:
        """Single skill's metadata (O(1) after first list_all).

        ``source=None`` returns the priority winner (workspace > builtin >
        first mirror source alphabetical). Pass ``source=`` for an exact
        compound-key lookup.
        """
        if self._by_name is None:
            self.list_all()  # populates both indices
        if source is not None:
            return (self._by_full_key or {}).get((source, name))
        return self._by_name.get(name) if self._by_name else None

    def get_body(self, name: str, source: str | None = None) -> str | None:
        """Full SKILL.md content. Resolves through ``list_all`` to handle
        nested layouts; layer priority used when ``source`` is omitted."""
        meta = self.get(name, source=source)
        if meta is None:
            return None
        try:
            return meta.path.read_text(encoding="utf-8")
        except OSError:
            return None

    def get_raw_metadata(
        self, name: str, source: str | None = None,
    ) -> dict | None:
        """Top-level frontmatter dict (YAML-lite parsed)."""
        body = self.get_body(name, source=source)
        if not body:
            return None
        return _parse_frontmatter(body)

    def check_available(
        self, name: str, source: str | None = None,
    ) -> bool:
        """True if all declared ``requires`` (bins, env) are satisfied."""
        meta = self.get(name, source=source)
        if meta is None:
            return False
        return _check_requirements(meta.requires)

    def get_missing_requirements(
        self, name: str, source: str | None = None,
    ) -> str:
        """Human-readable list of unmet requirements; empty when satisfied."""
        meta = self.get(name, source=source)
        if meta is None:
            return ""
        return _missing_requirements(meta.requires)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iter_skill_dirs(
        root: Path,
        default_source: str = "workspace",
        max_depth: int = 5,
    ):
        """Yield ``(skill_dir, source)`` pairs by recursive ``SKILL.md`` glob.

        ``max_depth`` (R2) caps how many directory levels below ``root``
        are searched. SKILL.md deeper than ``max_depth`` are silently
        skipped, preventing unbounded filesystem walks on huge mirrors.
        """
        if not root.exists():
            return

        all_md = list(root.rglob("SKILL.md"))
        all_md = [p for p in all_md if "/workspaces/" not in str(p)]
        all_md.sort(key=lambda p: (len(p.parts), str(p)))

        kept_parents: set[str] = set()
        root_str = str(root)
        for skill_md in all_md:
            try:
                rel = skill_md.parent.relative_to(root)
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) == 0:
                continue
            if len(parts) > max_depth:
                continue
            parent = skill_md.parent
            is_sub = False
            for ancestor in parent.parents:
                if str(ancestor) == root_str:
                    break
                if str(ancestor) in kept_parents:
                    is_sub = True
                    break
            if is_sub:
                continue
            kept_parents.add(str(parent))
            if len(parts) == 1:
                yield parent, default_source
            else:
                yield parent, parts[0]

    def _build_meta(
        self, skill_dir: Path, source: str,
        *, always_enabled: bool = True,
    ) -> SkillMeta | None:
        skill_file = skill_dir / "SKILL.md"
        try:
            body = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        frontmatter = _parse_frontmatter(body) or {}
        nested = _parse_nested_metadata(frontmatter.get("metadata", ""))
        requires = nested.get("requires", {}) if isinstance(nested.get("requires"), dict) else {}
        always = _resolve_always(frontmatter, nested)
        if not always_enabled:
            always = False
        content = _strip_frontmatter(body)

        # ``stable_key`` is the directory name. For everos it is
        # the sqlite ``skills.id`` string (the pipeline materializes
        # ``<workspace>/skills/everos/<sqlite_id>/SKILL.md``); for
        # everything else it equals the display name (legacy convention).
        stable_key = skill_dir.name
        # Display name prefers frontmatter ``name`` so everos skills
        # whose directory is a numeric id still surface a human-readable
        # label. Falls back to dir name for hand-authored skills lacking
        # that field.
        display_name = (frontmatter.get("name") or "").strip() or skill_dir.name
        description = frontmatter.get("description", "") or display_name

        return SkillMeta(
            id=f"{source}/{stable_key}",
            name=display_name,
            description=description,
            path=skill_file,
            content=content,
            source=source,  # type: ignore[arg-type]
            always=always,
            requires=requires,
            raw_frontmatter=frontmatter,
        )


# ----------------------------------------------------------------------
# Module-level helpers (pure functions, testable in isolation)
# ----------------------------------------------------------------------


def _parse_frontmatter(content: str) -> dict | None:
    """Minimal YAML-lite parser — matches legacy SkillsLoader behavior.

    Expected format::

        ---
        name: foo
        description: "bar"
        metadata: '{"raven": {...}}'
        ---

    Values are stripped of surrounding quotes; nested keys are not supported.
    Returns ``None`` when no frontmatter is present.
    """
    if not content.startswith("---"):
        return None
    m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return None
    metadata: dict = {}
    for line in m.group(1).split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip('"\'')
    return metadata


def _strip_frontmatter(content: str) -> str:
    """Return the markdown body with the leading ``---...---`` frontmatter removed."""
    if not content.startswith("---"):
        return content
    m = re.match(r"^---\n.*?\n---\n?", content, re.DOTALL)
    if not m:
        return content
    return content[m.end():]


def _parse_nested_metadata(raw: str) -> dict:
    """Extract Raven-namespaced metadata from the ``metadata`` JSON blob.

    Lookup order (first match wins): ``raven`` > ``nanobot`` > ``openclaw``.
    Returns ``{}`` on any failure.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    for key in ("raven", "nanobot", "openclaw"):
        if key in data and isinstance(data[key], dict):
            return data[key]
    return {}


_ALWAYS_TRUTHY = {"true", "1", "yes"}
_ALWAYS_KNOWN = _ALWAYS_TRUTHY | {"false", "0", "no", ""}


def _parse_always_value(raw: object) -> bool:
    """Strict always parser (R3). Only explicit truthy values count."""
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        lower = raw.strip().lower()
        if lower not in _ALWAYS_KNOWN:
            log.warning(
                "Unrecognized 'always' value '%s' — treated as false. "
                "Expected true/false/yes/no/1/0.", raw,
            )
        return lower in _ALWAYS_TRUTHY
    return False


def _resolve_always(frontmatter: dict, nested: dict) -> bool:
    """Resolve always flag from nested metadata (priority) or frontmatter.

    R3 fix: ``"false"`` is now correctly treated as ``False`` instead of
    the old behavior where ``bool("false") == True``.
    """
    nested_val = nested.get("always")
    if nested_val is not None:
        return _parse_always_value(nested_val)
    return _parse_always_value(frontmatter.get("always"))


def _check_requirements(requires: dict) -> bool:
    for b in requires.get("bins", []):
        if not isinstance(b, str):
            continue
        if not shutil.which(b):
            return False
    for env in requires.get("env", []):
        if not isinstance(env, str):
            continue
        if not os.environ.get(env):
            return False
    return True


def _missing_requirements(requires: dict) -> str:
    missing: list[str] = []
    for b in requires.get("bins", []):
        if not isinstance(b, str):
            continue
        if not shutil.which(b):
            missing.append(f"CLI: {b}")
    for env in requires.get("env", []):
        if not isinstance(env, str):
            continue
        if not os.environ.get(env):
            missing.append(f"ENV: {env}")
    return ", ".join(missing)
