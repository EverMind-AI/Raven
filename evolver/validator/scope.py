"""Agent-scope rules over a package snapshot: folder (F), declarations (D) and code (C).

These layers make no model call and start no agent code in this process. The rules
encode what the shipped agents under ``agents/`` actually use; a rule one of them
fails is a wrong rule (see the calibration test).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from evolver.validator.report import LayerResult, RuleResult
from evolver.validator.snapshot import Snapshot

REQUIRED_FILES = ("subagent.json", "run.py", "config.json")
# ``.env`` is a known agent-folder file (own-key mode); F3, not F2, owns its verdict.
ALLOWED_TOP_LEVEL = frozenset(
    {"subagent.json", "run.py", "config.json", "install.py", ".env.example", ".env", "README.md", "soul.md"}
)
ALLOWED_TOP_DIRS = frozenset({"modes", "skills", "plugins"})
MAX_FILE_BYTES = 1024 * 1024

_ENV_SECRET_KEY = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD)$")
_JSON_SECRET_KEY = re.compile(r"^(apikey|api_key|token|secret|password)$", re.IGNORECASE)
# Shapes of live credentials as vendors issue them; a match anywhere in a text file is a leak.
_KEY_SHAPE = re.compile(
    r"(sk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ghp_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{40,}"
    r"|xox[bpas]-[A-Za-z0-9-]{20,})"
)


def _rule(rule: str, failures: list[RuleResult], ok: str) -> list[RuleResult]:
    return failures or [RuleResult(rule, "pass", ok)]


def _files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        found.extend(base / name for name in filenames)
        found.extend(base / name for name in dirnames if (base / name).is_symlink())
    return sorted(found)


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def json_secret_keys(data: Any, prefix: str = "") -> list[str]:
    """Dotted paths of non-empty string values stored under credential-looking keys."""
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _JSON_SECRET_KEY.match(str(key)) and isinstance(value, str) and value.strip():
                found.append(path)
            else:
                found.extend(json_secret_keys(value, path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(json_secret_keys(item, f"{prefix}[{index}]"))
    return found


def _env_secrets(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if _ENV_SECRET_KEY.search(key) and value.strip().strip("'\""):
            found.append(key)
    return found


def check_folder(snap: Snapshot) -> LayerResult:
    root = snap.root
    results: list[RuleResult] = []

    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    results += _rule(
        "F1",
        [RuleResult("F1", "fail", f"required file missing: {name}", name, f"add {name}") for name in missing],
        "required files present",
    )

    stray = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        allowed = (
            (entry.is_dir() and not entry.is_symlink() and name in ALLOWED_TOP_DIRS)
            or name in ALLOWED_TOP_LEVEL
            or (name.endswith(".md") and not entry.is_dir())
        )
        if not allowed:
            stray.append(RuleResult("F2", "fail", f"not an agent-folder entry: {name}", name, "remove it"))
    results += _rule("F2", stray, "top-level entries allowed")

    secrets = []
    for path in _files(root):
        rel = _rel(path, root)
        if path.is_symlink():
            continue
        if path.name == ".env":
            secrets.append(RuleResult("F3", "fail", "a .env file ships secrets", rel, "remove it; ship .env.example"))
            continue
        if path.name.startswith(".env"):
            for key in _env_secrets(path.read_text(encoding="utf-8", errors="replace")):
                secrets.append(RuleResult("F3", "fail", f"credential value for {key}", rel, "leave the slot empty"))
        elif path.suffix == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            for key in json_secret_keys(data):
                secrets.append(RuleResult("F3", "fail", f"credential value at {key}", rel, "remove the value"))
        if path.stat().st_size <= MAX_FILE_BYTES:
            try:
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if match := _KEY_SHAPE.search(text):
                secrets.append(
                    RuleResult("F3", "fail", f"live-looking key {match.group(0)[:8]}...", rel, "remove the key")
                )
    results += _rule("F3", secrets, "no credentials")

    bounds = []
    resolved_root = root.resolve()
    for path in _files(root):
        rel = _rel(path, root)
        if path.is_symlink():
            target = path.resolve()
            if not target.is_relative_to(resolved_root):
                bounds.append(RuleResult("F4", "fail", f"symlink leaves the folder: {os.readlink(path)}", rel))
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            bounds.append(RuleResult("F4", "fail", f"file over 1 MiB ({path.stat().st_size} bytes)", rel))
    results += _rule("F4", bounds, "files inside the folder and under 1 MiB")

    if snap.build_output:
        results.append(
            RuleResult(
                "F5",
                "advisory",
                f"{len(snap.build_output)} build-output file(s) in the source folder were left out",
                snap.build_output[0],
            )
        )
    return LayerResult("F", results)


def _load_json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, str(exc)


def package_manifests(snap: Snapshot) -> list[tuple[Path, Any]]:
    """Every plugin manifest the package ships, parsed; unparseable ones are left to D4."""
    from raven.plugins.manifest import PluginManifest

    paths = sorted(snap.root.glob("plugins/*/raven-plugin.toml"))
    if snap.engine is not None:
        paths += sorted(snap.engine.glob("*/raven-plugin.toml"))
    parsed = []
    for path in paths:
        try:
            parsed.append((path, PluginManifest.from_toml_path(path)))
        except Exception:  # noqa: BLE001 - D4 reports the cause
            continue
    return parsed


def _manifest_paths(snap: Snapshot) -> list[Path]:
    paths = sorted(snap.root.glob("plugins/*/raven-plugin.toml"))
    if snap.engine is not None:
        paths += sorted(snap.engine.glob("*/raven-plugin.toml"))
    return paths


def _check_subagent(manifest: Any) -> tuple[list[RuleResult], list[RuleResult], list[RuleResult]]:
    from raven.config.schema import ThirdPartyAcpSubagentConfig

    d1: list[RuleResult] = []
    if not isinstance(manifest, dict) or manifest.get("kind") != "acp":
        d1.append(RuleResult("D1", "fail", "kind must be 'acp'", "subagent.json", 'set "kind": "acp"'))
    else:
        try:
            ThirdPartyAcpSubagentConfig.model_validate(manifest)
        except Exception as exc:  # noqa: BLE001 - pydantic's message is the evidence
            d1.append(RuleResult("D1", "fail", f"roster schema rejects it: {exc}", "subagent.json"))

    d2 = []
    for key in ("description", "owns"):
        value = manifest.get(key) if isinstance(manifest, dict) else None
        if not isinstance(value, str) or not value.strip() or "TODO" in value:
            d2.append(RuleResult("D2", "fail", f"{key} is empty or still a TODO", f"subagent.json:{key}"))

    d3 = []
    command = str((manifest or {}).get("command") or "") if isinstance(manifest, dict) else ""
    cwd = str((manifest or {}).get("cwd") or "") if isinstance(manifest, dict) else ""
    if not command.startswith("{PYTHON}") or "{SUBAGENT_DIR}" not in command:
        d3.append(RuleResult("D3", "fail", "command must be '{PYTHON} {SUBAGENT_DIR}/...'", "subagent.json:command"))
    if "{SUBAGENT_DIR}" not in cwd:
        d3.append(RuleResult("D3", "fail", "cwd must name {SUBAGENT_DIR}", "subagent.json:cwd"))
    for token in [*command.split(), *cwd.split()]:
        if token.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", token):
            d3.append(RuleResult("D3", "fail", f"absolute path in the launcher: {token}", "subagent.json"))
    return d1, d2, d3


def _config_d5(path: Path) -> list[RuleResult]:
    import tempfile

    from raven.config.loader import load_config
    from raven.config.raven import load_raven_config

    data, error = _load_json(path)
    if error is not None:
        return [RuleResult("D5", "fail", f"config.json is not valid JSON: {error}", "config.json")]
    with tempfile.TemporaryDirectory(prefix="validate-config-") as scratch:
        copy = Path(scratch) / "config.json"
        copy.write_text(json.dumps(data), encoding="utf-8")
        for loader in (load_config, load_raven_config):
            try:
                loader(copy)
            except Exception as exc:  # noqa: BLE001 - the loader's message is the evidence
                return [RuleResult("D5", "fail", f"{loader.__name__} rejects config.json: {exc}", "config.json")]
    return []


def _config_d6(data: Any) -> list[RuleResult]:
    from raven.config.product_render import _MEMORY_SLICE_INHERITED

    if not isinstance(data, dict):
        return []
    found = [
        RuleResult("D6", "fail", f"credential value at {key}", "config.json", "use the .env slots instead")
        for key in json_secret_keys(data)
    ]
    plugins = data.get("plugins") if isinstance(data.get("plugins"), dict) else {}
    if "dirs" in plugins:
        found.append(RuleResult("D6", "fail", "plugins.dirs is injected by the launcher", "config.json:plugins.dirs"))
    slices = plugins.get("config") if isinstance(plugins.get("config"), dict) else {}
    for distribution, (replaced, _filled) in _MEMORY_SLICE_INHERITED.items():
        own = slices.get(distribution) if isinstance(slices.get(distribution), dict) else {}
        for key in replaced:
            if key in own:
                found.append(
                    RuleResult(
                        "D6",
                        "fail",
                        f"the host sets {distribution}.{key} at render time",
                        f"config.json:plugins.config.{distribution}.{key}",
                    )
                )
    return found


def _memory_ids(folder: Path) -> set[str]:
    ids = set()
    for name in ("config.json", "subagent.json"):
        data, _ = _load_json(folder / name)
        memory = data.get("memory") if isinstance(data, dict) else None
        if isinstance(memory, dict) and isinstance(memory.get("userId"), str):
            ids.add(memory["userId"])
    return ids


def _advisories(snap: Snapshot, manifests: list[tuple[Path, Any]], shipped_root: Path | None) -> list[RuleResult]:
    from raven.agent.subagent.vendored_agents import env_prefix

    prefix = env_prefix(snap.source.name).lower()
    notes = [
        RuleResult("D7", "advisory", f"tool name not derived from the agent name: {tool.name}", str(path.name))
        for path, manifest in manifests
        for tool in manifest.contributes.tools
        if not tool.name.startswith(prefix)
    ]
    if shipped_root is not None and shipped_root.is_dir():
        own = _memory_ids(snap.root)
        for other in sorted(p for p in shipped_root.iterdir() if p.is_dir() and p.name != snap.source.name):
            shared = own & _memory_ids(other)
            if shared:
                notes.append(
                    RuleResult("D7", "advisory", f"memory identity {sorted(shared)} shared with {other.name}", "")
                )
    return notes


def check_declarations(snap: Snapshot, *, shipped_root: Path | None) -> LayerResult:
    from raven.plugins.manifest import PluginManifest

    root = snap.root
    manifest, error = _load_json(root / "subagent.json")
    results: list[RuleResult] = []
    if error is not None:
        results.append(RuleResult("D1", "fail", f"subagent.json is not valid JSON: {error}", "subagent.json"))
        manifest = {}
    d1, d2, d3 = _check_subagent(manifest)
    results += _rule("D1", d1 if error is None else [], "roster row valid")
    results += _rule("D2", d2, "description and owns written")
    results += _rule("D3", d3, "launcher uses placeholders")

    d4 = []
    for path in _manifest_paths(snap):
        try:
            PluginManifest.from_toml_path(path)
        except Exception as exc:  # noqa: BLE001 - the parser's message is the evidence
            d4.append(RuleResult("D4", "fail", f"manifest rejected: {exc}", str(path.relative_to(path.parents[1]))))
    results += _rule("D4", d4, "plugin manifests parse")

    results += _rule("D5", _config_d5(root / "config.json"), "config.json loads")
    config, _ = _load_json(root / "config.json")
    results += _rule("D6", _config_d6(config), "no host-owned keys or credentials in config.json")
    results += _advisories(snap, package_manifests(snap), shipped_root)
    return LayerResult("D", results)


FORBIDDEN_IMPORTS = frozenset({"evolver", "experimental", "benchmarks", "tests"})
ENGINE_PACKAGES = frozenset({"raven_design", "raven_ppt"})
IMPORT_TIMEOUT_S = 60


def _packages_under(parent: Path) -> set[str]:
    return (
        {p.name for p in parent.iterdir() if p.is_dir() and (p / "__init__.py").is_file()} if parent.is_dir() else set()
    )


def _own_packages(snap: Snapshot) -> set[str]:
    own: set[str] = set()
    for plugin in sorted(snap.root.glob("plugins/*")):
        own |= _packages_under(plugin)
    if snap.engine is not None:
        own |= _packages_under(snap.engine)
    return own


def other_plugin_packages(shipped_root: Path | None, own: Snapshot) -> frozenset[str]:
    others = set(ENGINE_PACKAGES)
    if shipped_root is not None and shipped_root.is_dir():
        for plugin in sorted(shipped_root.glob("*/plugins/*")):
            others |= _packages_under(plugin)
    return frozenset(others - _own_packages(own))


def _python_files(snap: Snapshot) -> list[tuple[Path, Path]]:
    roots = [snap.root] + ([snap.engine] if snap.engine is not None else [])
    return [(root, path) for root in roots for path in sorted(root.rglob("*.py")) if not path.is_symlink()]


def _imported_names(tree) -> set[str]:
    import ast

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _factory_modules(snap: Snapshot) -> list[tuple[Path, str]]:
    """(sys.path root, module) for every factory a package manifest names."""
    found: list[tuple[Path, str]] = []
    for path, manifest in package_manifests(snap):
        root = path.parent if path.parent.parent.name == "plugins" else path.parent.parent
        contributes = manifest.contributes
        for group in (
            contributes.tools,
            contributes.hooks,
            contributes.memory_backends,
            contributes.services,
            contributes.tool_gates,
            contributes.session_observers,
            contributes.onboard,
        ):
            for item in group:
                module = item.factory.split(":", 1)[0]
                if (root, module) not in found:
                    found.append((root, module))
    return found


def _module_file(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _snapshot_digests(snap: Snapshot) -> tuple[str, str | None]:
    from evolver.validator.snapshot import tree_digest

    return tree_digest(snap.root), tree_digest(snap.engine) if snap.engine is not None else None


def _import_failures(snap: Snapshot, modules: list[tuple[Path, str]]) -> list[RuleResult]:
    import subprocess
    import tempfile

    from evolver.validator.children import child_env, module_command

    if not modules:
        return []
    before = _snapshot_digests(snap)
    roots = list(dict.fromkeys(str(root) for root, _ in modules))
    args = [arg for root in roots for arg in ("--root", root)] + ["--", *[m for _, m in modules]]
    with tempfile.TemporaryDirectory(prefix="validate-import-") as scratch:
        base = Path(scratch)
        (base / "home").mkdir()
        (base / "raven-home").mkdir()
        env = child_env({**os.environ, "HOME": str(base / "home"), "RAVEN_HOME": str(base / "raven-home")})
        try:
            proc = subprocess.run(
                module_command("evolver.validator._import_probe", *args),
                cwd=base,
                env=env,
                capture_output=True,
                text=True,
                timeout=IMPORT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return [RuleResult("C3", "fail", f"importing the factories took over {IMPORT_TIMEOUT_S}s")]
    failures = []
    if proc.returncode != 0:
        detail = (proc.stdout or proc.stderr).strip()
        failures.append(RuleResult("C3", "fail", f"factory import failed: {detail[-800:]}"))
    if _snapshot_digests(snap) != before:
        failures.append(RuleResult("C3", "fail", "importing the factories changed the package's own files"))
    return failures


def check_code(snap: Snapshot, *, other_packages: frozenset[str]) -> LayerResult:
    import ast

    forbidden = FORBIDDEN_IMPORTS | other_packages
    c1: list[RuleResult] = []
    c2: list[RuleResult] = []
    bad_files: set[Path] = set()
    for root, path in _python_files(snap):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_bytes(), filename=rel)
        except (SyntaxError, ValueError) as exc:
            c1.append(RuleResult("C1", "fail", f"does not compile: {exc}", rel))
            bad_files.add(path)
            continue
        hits = sorted(_imported_names(tree) & forbidden)
        if hits:
            c2.append(RuleResult("C2", "fail", f"imports outside the agent scope: {', '.join(hits)}", rel))
            bad_files.add(path)

    # C3 imports only modules whose own file passed C1 and C2, so one cause fails one rule.
    modules = [(root, m) for root, m in _factory_modules(snap) if _module_file(root, m) not in bad_files]
    results = _rule("C1", c1, "every .py compiles")
    results += _rule("C2", c2, "imports stay inside the agent scope")
    results += _rule("C3", _import_failures(snap, modules), "factory modules import cleanly")
    return LayerResult("C", results)
