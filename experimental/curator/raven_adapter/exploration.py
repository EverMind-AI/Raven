"""Give Curator native Raven tools over a source snapshot and a disposable draft workspace."""

import asyncio
import json
import shutil
from copy import deepcopy
from dataclasses import dataclass
from fnmatch import fnmatch
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from raven.agent.tools.file_search import FindTool, GrepTool
from raven.agent.tools.filesystem import ListDirTool, ReadFileTool
from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.shell import ExecTool
from raven.permissions import BuiltinRulings, PermissionGate
from raven.sandbox import build_executor

from ..harness import Artifact
from .materialize import _write, extend_artifact, write_package

_REPOSITORY = Path(__file__).resolve().parents[3]
_SOURCE_PATHS = (
    "raven",
    "experimental/curator",
    "tests",
    "docs",
    "docs-site/docs",
    "pyproject.toml",
    "uv.lock",
    "CONTEXT.md",
    "CONTEXT-MAP.md",
    "AGENTS.md",
)
# Reference-only sources: exploration reads them from its snapshot and the installed harness never runs them, so a
# live edit during a curation cannot split its evidence from what it will run against. Only the snapshot must hold.
_REFERENCE_PATHS = ("tests", "docs", "docs-site/docs", "CONTEXT.md", "CONTEXT-MAP.md", "AGENTS.md")
_EXCLUDED = frozenset({".git", ".worktree", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache"})


@dataclass(frozen=True)
class Withheld:
    """Repository files the Curator may not read: an evaluation side keeping answers the curated worker must earn.

    `paths` are glob patterns relative to the repository. A snapshot file whose content holds any of `markers` is
    withheld too, so a reference that names the evaluation side cannot carry it in under another name.
    """

    paths: tuple[str, ...] = ()
    markers: tuple[bytes, ...] = ()


def _digest(path):
    return sha256(path.read_bytes()).hexdigest()


class _Exec(ExecTool):
    """Use the native command implementation with this curation's owned lifecycle."""

    def __init__(self, owner, config):
        super().__init__(
            working_dir=str(owner.root),
            timeout=config.tools.exec.timeout,
            restrict_to_workspace=True,
            path_append=config.tools.exec.path_append,
            executor=owner.executor,
            follow_binding=False,
        )
        self.owner = owner

    @property
    def description(self):
        return "Run a foreground shell command in the curation workspace using the configured Raven executor."

    @property
    def parameters(self):
        schema = deepcopy(super().parameters)
        for name in ("machine", "run_in_background", "working_dir"):
            schema["properties"].pop(name, None)
        schema["additionalProperties"] = False
        return schema

    async def execute(self, **kwargs):
        if kwargs.get("machine") or kwargs.get("run_in_background"):
            raise ValueError("curation commands must finish in the current execution environment")
        if kwargs.pop("working_dir", None) not in (None, "", str(self.owner.root)):
            raise ValueError("curation commands run in the curation workspace only")
        await self.owner.start_executor()
        return await super().execute(**kwargs)


class Exploration:
    """Own per-curation resources without constructing a worker AgentLoop.

    Source files and input facts are copied once and checked before handoff.
    Native file tools stay inside this workspace. Shell commands are offered only
    when the configured Raven executor is an OS sandbox: a DirectExecutor could
    read anything on the host. Commands may write scratch files, but changes to
    source or facts invalidate curation. Files the caller names in `withheld` never
    enter the snapshot.
    """

    def __init__(
        self,
        config,
        inspection,
        *,
        repository=_REPOSITORY,
        source_paths=_SOURCE_PATHS,
        withheld=Withheld(),
        executor=None,
        root=None,
    ):
        self.config = config.model_copy(deep=True)
        self.inspection = inspection
        self.withheld = withheld
        self.repository = Path(repository).resolve()
        self.source_paths = tuple(source_paths)
        self.mounts = {}
        for relative in self.source_paths:
            path = self.repository / relative
            if not path.resolve().is_relative_to(self.repository):
                raise ValueError(f"source path escapes repository: {relative}")
            if path.exists() and not path.is_symlink():
                self.mounts[f"source/{relative}"] = path
        roots = [Path(value) for value in inspection.facts.get("source_roots", ())]
        roots.extend(Path(entry.get("root", entry["path"])) for entry in inspection.sources.values())
        for path in sorted(set(roots), key=lambda value: (len(value.parts), str(value))):
            # A root inside an existing mount is already there; a root above one (a package
            # climbed to the repository's top-level directory) must not widen the declared scope.
            if any(path.is_relative_to(existing) or existing.is_relative_to(path) for existing in self.mounts.values()):
                continue
            if not path.exists() or path.is_symlink():
                raise ValueError(f"material root is unavailable: {path}")
            name = (
                f"source/{path.relative_to(self.repository)}"
                if path.is_relative_to(self.repository)
                else f"materials/{sha256(str(path).encode()).hexdigest()[:12]}/{path.name}"
            )
            self.mounts[name] = path
        for entry in inspection.sources.values():
            path = Path(entry["path"])
            if any(path.is_relative_to(existing) for existing in self.mounts.values()):
                continue
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"registered source is unavailable: {path}")
            name = (
                f"source/{path.relative_to(self.repository)}"
                if path.is_relative_to(self.repository)
                else f"materials/{sha256(str(path).encode()).hexdigest()[:12]}/{path.name}"
            )
            self.mounts[name] = path
        self.temporary = TemporaryDirectory(prefix="raven-curator-explore-") if root is None else None
        self.root = Path(self.temporary.name) if self.temporary else Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.originals = {}
        self.protected = {}
        self._marked = {}
        self.started = False
        self.start_attempted = False
        self.start_lock = asyncio.Lock()
        try:
            self.executor = executor or build_executor(
                self.config.tools.sandbox, self.root, sandbox_dir=lambda name: self.root / ".sandbox" / name
            )
            gate = PermissionGate(
                config_source=lambda: self.config.permissions,
                builtin=BuiltinRulings(extra_deny_patterns=self.config.tools.exec.extra_deny_patterns),
                allow_ask=False,
            )
            self.registry = ToolRegistry(permission_gate=gate)
            if (self.root / "snapshot.json").exists():
                self._restore()
            else:
                self._snapshot()
                self._save()
            for implementation in (ReadFileTool, ListDirTool, GrepTool, FindTool):
                self.registry.register(
                    implementation(workspace=self.root, allowed_dirs=(self.root,), follow_binding=False)
                )
            if self.executor.is_sandboxed:
                self.registry.register(_Exec(self, self.config))
        except BaseException:
            if self.temporary:
                self.temporary.cleanup()
            raise

    def _withheld(self, file, *, snapshot=False):
        base = self.root / "source" if snapshot else self.repository
        if not file.is_relative_to(base):
            return False
        relative = file.relative_to(base).as_posix()
        if any(fnmatch(relative, pattern) for pattern in self.withheld.paths):
            return True
        key = (file, *(lambda stat: (stat.st_mtime_ns, stat.st_size))(file.stat()))
        if key not in self._marked:
            content = file.read_bytes()
            self._marked[key] = any(marker in content for marker in self.withheld.markers)
        return self._marked[key]

    def _files(self, *, snapshot=False):
        for file in self._all_files(snapshot=snapshot):
            if not self._withheld(file, snapshot=snapshot):
                yield file

    def _all_files(self, *, snapshot=False):
        for relative, original in self.mounts.items():
            path = self.root / relative if snapshot else original
            if not path.exists() or path.is_symlink():
                continue
            if path.is_file():
                yield path
                continue
            for folder, directories, names in path.walk(follow_symlinks=False):
                directories[:] = [
                    name for name in directories if name not in _EXCLUDED and not (folder / name).is_symlink()
                ]
                for name in names:
                    file = folder / name
                    if name.startswith(".env") or name.endswith((".pem", ".key")):
                        continue
                    if file.is_file() and not file.is_symlink():
                        yield file

    def _copy_path(self, path):
        for relative, original in self.mounts.items():
            if path.is_relative_to(original):
                return self.root / relative / path.relative_to(original)
        raise ValueError(f"registered source is outside material roots: {path}")

    @property
    def sources(self):
        return {
            name: {**entry, "snapshot_path": str(self._copy_path(Path(entry["path"])))}
            for name, entry in self.inspection.sources.items()
        }

    def read_source(self, **kwargs):
        result = self.inspection.read_source(**kwargs)
        if "path" in result:
            result["path"] = str(self._copy_path(Path(result["path"])))
        return result

    def _snapshot(self):
        for original in self._files():
            content = original.read_bytes()
            copy = self._copy_path(original)
            _write(copy, content)
            digest = sha256(content).hexdigest()
            self.originals[original] = digest
            self.protected[copy] = digest
        for source in self.inspection.sources.values():
            original = Path(source["path"])
            if original in self.originals and source["digest"] != self.originals[original]:
                raise ValueError("registered source changed before exploration; inspect again")
        self.active = Artifact.model_validate(self.inspection.facts.get("authored", {"values": {}}))
        self.active_package = write_package(self.root / "active", self.active)
        facts = self.root / "facts.json"
        _write(facts, json.dumps(self.inspection.facts, ensure_ascii=False, indent=2).encode())
        self.protected[facts] = _digest(facts)
        for file in self.active_package.rglob("*"):
            if file.is_file():
                self.protected[file] = _digest(file)
        self.verify()

    def _save(self):
        _write(
            self.root / "snapshot.json",
            json.dumps(
                {
                    "repository": str(self.repository),
                    "source_paths": self.source_paths,
                    "mounts": {name: str(path) for name, path in self.mounts.items()},
                    "originals": {str(path): digest for path, digest in self.originals.items()},
                    "protected": {str(path.relative_to(self.root)): digest for path, digest in self.protected.items()},
                }
            ).encode(),
        )

    def _restore(self):
        saved = json.loads((self.root / "snapshot.json").read_text())
        if saved["repository"] != str(self.repository) or tuple(saved["source_paths"]) != self.source_paths:
            raise ValueError("exploration source scope changed; cannot resume")
        if saved.get("mounts") != {name: str(path) for name, path in self.mounts.items()}:
            raise ValueError("exploration material roots changed; cannot resume")
        self.originals = {Path(path): digest for path, digest in saved["originals"].items()}
        self.protected = {self.root / path: digest for path, digest in saved["protected"].items()}
        self.active = Artifact.model_validate(self.inspection.facts.get("authored", {"values": {}}))
        self.verify()
        self.active_package = write_package(self.root / "active", self.active)

    def _runtime(self, path):
        return not any(path.is_relative_to(self.repository / relative) for relative in _REFERENCE_PATHS)

    def verify(self):
        if {path for path in self._files() if self._runtime(path)} != set(filter(self._runtime, self.originals)):
            raise ValueError("source inventory changed; inspect the current baseline again")
        copied = {self._copy_path(path) for path in self.originals}
        if set(self._files(snapshot=True)) != copied:
            raise ValueError("source snapshot inventory changed; inspect again")
        runtime = [(file, digest) for file, digest in self.originals.items() if self._runtime(file)]
        for file, digest in (*runtime, *self.protected.items()):
            if not file.is_file() or file.is_symlink() or _digest(file) != digest:
                raise ValueError(f"curation input changed; inspect again: {file}")

    def describe(self):
        return {
            "workspace": str(self.root),
            "source_root": str(self.root / "source"),
            "source_paths": [name for name in self.source_paths if (self.root / "source" / name).exists()],
            "material_roots": {name: str(self.root / name) for name in self.mounts},
            "excluded_directories": sorted(_EXCLUDED),
            "symlinks": "not copied",
            "facts_file": str(self.root / "facts.json"),
            "active_package": str(self.active_package),
            "candidate_directory": str(self.root / "candidate"),
            "executor": type(self.executor).__name__,
            "sandboxed": self.executor.is_sandboxed,
            "shell": "exec" if self.executor.is_sandboxed else "not offered: the executor is not an OS sandbox",
            "baseline": self.inspection.declaration.baseline,
        }

    def stage_candidate(self, candidate):
        self.verify()
        directory = self.root / "candidate"
        shutil.rmtree(directory, ignore_errors=True)
        if candidate is None:
            return None
        effective = extend_artifact(self.active, candidate.artifact)
        package = write_package(directory, effective)
        _write(directory / "artifact.json", effective.model_dump_json(indent=2).encode())
        return {"package": str(package), "artifact": str(directory / "artifact.json")}

    async def start_executor(self):
        async with self.start_lock:
            if not self.started:
                self.start_attempted = True
                await self.executor.start()
                self.started = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        try:
            if self.start_attempted:
                await self.executor.stop()
        finally:
            if self.temporary:
                self.temporary.cleanup()
