#!/usr/bin/env python3
"""
CLI for managing boxlite OCI images and VMs.

Usage:
  python scripts/boxlite_cli.py [--home-dir PATH] <resource> <action> [options]

  python scripts/boxlite_cli.py image pull <image> [-u USERNAME] [-p PASSWORD]
    # Providing -u/-p writes credentials to ~/.docker/config.json for that registry.
  python scripts/boxlite_cli.py image ls
  python scripts/boxlite_cli.py image rm <image> [--force]

  python scripts/boxlite_cli.py vm ls
  python scripts/boxlite_cli.py vm create --image <image> [--name NAME] [--cpus N] [--memory MiB] [--disk GB] [--start]
  python scripts/boxlite_cli.py vm start <id_or_name>
  python scripts/boxlite_cli.py vm stop <id_or_name>
  python scripts/boxlite_cli.py vm rm <id_or_name> [--force]
  python scripts/boxlite_cli.py vm shell <id_or_name> [--shell /bin/sh]

--home-dir overrides the boxlite runtime home (DB, images, layers). It also
overrides the BOXLITE_HOME env var. Default: ~/.boxlite.

(File renamed from tools/boxlite.py in the scripts/ + tools/ reorg —
the previous `_sys.path` shadowing-defense hack at the top of this
file is no longer needed because the script no longer shares its name
with the ``boxlite`` package.)
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

# ── Storage paths ──────────────────────────────────────────────────────────────

# Resolved boxlite runtime home. Set once by main() from the --home-dir CLI flag,
# the BOXLITE_HOME env var, or the boxlite default (~/.boxlite). All DB / image
# helpers and Boxlite() runtime constructors read it from here.
_HOME: Path = Path(os.environ.get("BOXLITE_HOME", Path.home() / ".boxlite"))


def _set_home(path: Path) -> None:
    global _HOME
    _HOME = path


def _home() -> Path:
    return _HOME


def _db_path() -> Path:
    return _home() / "db" / "boxlite.db"


def _images_dir() -> Path:
    return _home() / "images"


_runtime_cache: dict[str, object] = {}


def _runtime():
    """Return a boxlite runtime rooted at the active home dir.

    Memoised per home_dir because boxlite's Rust core takes a process-wide
    filesystem lock per home_dir that is only released when the ``Boxlite``
    instance is dropped — calling ``Boxlite()`` twice in one process panics
    with "Another BoxliteRuntime is already using directory: …". Today every
    subcommand only calls _runtime() once, but caching here keeps that
    invariant from being a footgun for future commands.
    """
    import boxlite

    home = str(_home())
    rt = _runtime_cache.get(home)
    if rt is None:
        rt = boxlite.Boxlite(boxlite.Options(home_dir=home))
        _runtime_cache[home] = rt
    return rt


# ── Image name helpers ─────────────────────────────────────────────────────────


def _canonical(image: str) -> str:
    """Normalize a short image name to its canonical registry form.

    docker.io convention: unqualified names (no dot/colon in the first component
    before the first slash) are expanded to docker.io/library/<name>.
    Names with one slash component (e.g. "myorg/app:tag") get docker.io/ prepended.
    Names that already start with a known registry domain are returned unchanged.
    """
    name_part = image.split(":")[0]
    first_component = name_part.split("/")[0]
    # Already has a registry domain (contains a dot or is "localhost")
    if "." in first_component or first_component == "localhost":
        return image
    # Has an org but no registry → docker.io
    if "/" in name_part:
        return f"docker.io/{image}"
    # Plain name like "ubuntu:22.04" → docker.io/library
    return f"docker.io/library/{image}"


def _short(reference: str) -> str:
    """Strip the docker.io/library/ prefix for display."""
    if reference.startswith("docker.io/library/"):
        return reference[len("docker.io/library/") :]
    if reference.startswith("docker.io/"):
        return reference[len("docker.io/") :]
    return reference


def _match_reference(reference: str, user_input: str) -> bool:
    canon = _canonical(user_input)
    return reference == canon or reference == user_input or _short(reference) == user_input


# ── Registry credentials ───────────────────────────────────────────────────────


def _registry_auth_key(image: str) -> str:
    """Return the key used in ~/.docker/config.json auths for the image's registry.

    Docker Hub uses the legacy endpoint "https://index.docker.io/v1/" regardless
    of whether the image was specified as "ubuntu:22.04", "docker.io/library/...",
    or any other Docker Hub form. All other registries use their bare hostname
    (and port, if present).
    """
    name_part = image.split(":")[0]
    first = name_part.split("/")[0]
    if first in ("docker.io", "index.docker.io") or ("." not in first and ":" not in first and first != "localhost"):
        return "https://index.docker.io/v1/"
    return first  # e.g. "ghcr.io", "registry.example.com:5000"


def _store_registry_credentials(registry_key: str, username: str, password: str) -> None:
    """Write base64-encoded credentials into ~/.docker/config.json.

    This is the standard credential store that OCI-compliant clients (including
    boxlite's Rust image puller) read when authenticating to a registry.
    Existing entries for other registries are preserved.
    """
    import base64

    config_path = Path.home() / ".docker" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        config = {}
    auth_b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
    config.setdefault("auths", {})[registry_key] = {"auth": auth_b64}
    config_path.write_text(json.dumps(config, indent=2) + "\n")


# ── SQLite helpers ─────────────────────────────────────────────────────────────


def _open_db() -> sqlite3.Connection:
    db = _db_path()
    if not db.exists():
        print(f"Error: boxlite database not found at {db}", file=sys.stderr)
        print("Is boxlite installed? Run: pip install raven[sandbox]", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(str(db))


def _image_rows() -> list[dict]:
    with _open_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT reference, manifest_digest, config_digest, layers, cached_at, complete "
            "FROM image_index ORDER BY cached_at"
        ).fetchall()
    return [dict(r) for r in rows]


def _boxes_using_image(canonical_ref: str) -> list[dict]:
    """Return all boxes (any state) whose config references the given canonical image."""
    with _open_db() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT bc.id, bc.name, bs.status, bc.json FROM box_config bc JOIN box_state bs ON bc.id = bs.id"
        ).fetchall()
    short = _short(canonical_ref)
    result = []
    for row in rows:
        try:
            cfg = json.loads(row["json"])
        except (json.JSONDecodeError, TypeError):
            continue
        # box_config stores the image as options.rootfs.Image (short or canonical form)
        stored = cfg.get("options", {}).get("rootfs", {}).get("Image", "")
        if stored == canonical_ref or stored == short or _canonical(stored) == canonical_ref:
            result.append(dict(row))
    return result


def _delete_image_from_db(canonical_ref: str) -> int:
    with _open_db() as conn:
        cur = conn.execute("DELETE FROM image_index WHERE reference = ?", (canonical_ref,))
        conn.commit()
        return cur.rowcount


# ── Orphaned file GC ───────────────────────────────────────────────────────────


def _gc_image_files(removed_row: dict) -> None:
    """Remove files for the deleted image that are no longer referenced by any remaining image."""
    remaining = _image_rows()

    # Collect all digests still referenced (manifest + config + layers)
    still_needed: set[str] = set()
    for row in remaining:
        still_needed.add(row["manifest_digest"].removeprefix("sha256:"))
        still_needed.add(row["config_digest"].removeprefix("sha256:"))
        for layer in json.loads(row["layers"]):
            still_needed.add(layer.removeprefix("sha256:"))

    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    images = _images_dir()
    manifest_hex = removed_row["manifest_digest"].removeprefix("sha256:")
    if manifest_hex not in still_needed:
        _safe_unlink(images / "manifests" / f"sha256-{manifest_hex}.json")

    config_hex = removed_row["config_digest"].removeprefix("sha256:")
    if config_hex not in still_needed:
        _safe_unlink(images / "configs" / f"sha256-{config_hex}.json")

    for layer in json.loads(removed_row["layers"]):
        layer_hex = layer.removeprefix("sha256:")
        if layer_hex not in still_needed:
            _safe_unlink(images / "layers" / f"sha256-{layer_hex}.tar.gz")


# ── Image size ─────────────────────────────────────────────────────────────────


def _layer_sizes_bytes(row: dict) -> int:
    """Sum compressed layer sizes on disk; fall back to 0 if files are missing."""
    total = 0
    layers_dir = _images_dir() / "layers"
    for layer in json.loads(row["layers"]):
        hex_ = layer.removeprefix("sha256:")
        p = layers_dir / f"sha256-{hex_}.tar.gz"
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


# ── Image subcommands ──────────────────────────────────────────────────────────


async def image_pull(
    image: str,
    username: str | None = None,
    password: str | None = None,
) -> None:
    # Credential resolution: CLI args > env vars
    username = username or os.environ.get("BOXLITE_REGISTRY_USERNAME")
    password = password or os.environ.get("BOXLITE_REGISTRY_PASSWORD")
    if bool(username) != bool(password):
        print("Error: --username and --password must be provided together.", file=sys.stderr)
        sys.exit(1)
    if username and password:
        key = _registry_auth_key(image)
        _store_registry_credentials(key, username, password)
        print(f"Credentials stored for {key}")

    import boxlite

    print(f"Pulling {image} ...")
    # SimpleBox without runtime= falls back to Boxlite.default() (~/.boxlite),
    # which would silently ignore --home-dir. Bind it to the active runtime
    # so the pull lands in the home dir the user asked for.
    async with boxlite.SimpleBox(
        image=image,
        cpus=1,
        memory_mib=256,
        runtime=_runtime(),
    ) as box:
        result = await box.exec("sh", "-c", "echo ok")
        if result.exit_code != 0:
            print(f"Pull check failed (exit {result.exit_code}): {result.stderr}", file=sys.stderr)
            sys.exit(1)
    print(f"Pulled {_canonical(image)}")


def image_ls() -> None:
    rows = _image_rows()
    if not rows:
        print("No images cached.")
        return

    display = [
        (_short(r["reference"]), r["manifest_digest"][:19], r["cached_at"][:19], _layer_sizes_bytes(r)) for r in rows
    ]
    ref_w = max(len(d[0]) for d in display)
    ref_w = max(ref_w, 5)

    print(f"{'IMAGE':<{ref_w}}  {'MANIFEST':<19}  {'CACHED AT':<19}  SIZE")
    print("-" * (ref_w + 2 + 19 + 2 + 19 + 2 + 12))
    for ref, digest, cached_at, size in display:
        size_str = f"{size / 1_048_576:.1f} MiB" if size else "?"
        print(f"{ref:<{ref_w}}  {digest:<19}  {cached_at:<19}  {size_str}")


def image_rm(image: str, force: bool) -> None:
    rows = _image_rows()
    matched = [r for r in rows if _match_reference(r["reference"], image)]
    if not matched:
        print(f"Error: '{image}' not found in local cache.", file=sys.stderr)
        image_ls()
        sys.exit(1)

    for row in matched:
        canonical = row["reference"]
        using = _boxes_using_image(canonical)
        if using and not force:
            ids = ", ".join((b["name"] or b["id"][:12]) for b in using)
            print(
                f"Error: '{_short(canonical)}' is referenced by {len(using)} VM(s): {ids}\n"
                "Remove those VMs first, or pass --force to remove anyway.",
                file=sys.stderr,
            )
            sys.exit(1)

        deleted = _delete_image_from_db(canonical)
        if deleted:
            _gc_image_files(row)
            print(f"Removed {_short(canonical)}")
        else:
            print(f"Warning: no DB row deleted for {_short(canonical)}", file=sys.stderr)


# ── VM subcommands ─────────────────────────────────────────────────────────────


def _state_str(state: object) -> str:
    # BoxStateInfo has a .status str field ("running", "stopped", "created", ...)
    status = getattr(state, "status", None)
    if status:
        return status.capitalize()
    return str(state).split(".")[-1]


async def vm_ls() -> None:
    rt = _runtime()
    boxes = await rt.list_info()
    if not boxes:
        print("No VMs.")
        return

    name_w = max((len(b.name or "") for b in boxes), default=0)
    name_w = max(name_w, 4)
    img_w = max(len(_short(b.image)) for b in boxes)
    img_w = max(img_w, 5)

    print(f"{'ID':<26}  {'NAME':<{name_w}}  {'STATE':<10}  {'IMAGE':<{img_w}}  CPUS  MEM(MiB)")
    print("-" * (26 + 2 + name_w + 2 + 10 + 2 + img_w + 2 + 4 + 2 + 8))
    for b in boxes:
        print(
            f"{b.id:<26}  {b.name or '':<{name_w}}  {_state_str(b.state):<10}  "
            f"{_short(b.image):<{img_w}}  {b.cpus:<4}  {b.memory_mib}"
        )


async def vm_create(
    image: str,
    name: str | None,
    cpus: int,
    memory: int,
    disk: int | None,
    start: bool,
) -> None:
    import boxlite

    rt = _runtime()
    opts = boxlite.BoxOptions(
        image=image,
        cpus=cpus,
        memory_mib=memory,
        disk_size_gb=disk,
        auto_remove=False,
        detach=True,
    )
    create_kwargs: dict = {}
    if name:
        create_kwargs["name"] = name
    box = await rt.create(opts, **create_kwargs)
    label = f" ({name})" if name else ""
    if start:
        await box.start()
        print(f"Created and started VM {box.id}{label}")
    else:
        print(f"Created VM {box.id}{label}  (use 'vm start {box.id}' to boot)")


async def vm_start(id_or_name: str) -> None:
    rt = _runtime()
    box = await rt.get(id_or_name)
    await box.start()
    print(f"Started {id_or_name}")


async def vm_stop(id_or_name: str) -> None:
    rt = _runtime()
    box = await rt.get(id_or_name)
    await box.stop()
    print(f"Stopped {id_or_name}")


async def vm_rm(id_or_name: str, force: bool) -> None:
    rt = _runtime()
    if force:
        # Try to stop first; ignore errors (box may already be stopped)
        try:
            box = await rt.get(id_or_name)
            await box.stop()
        except Exception:
            pass
    await rt.remove(id_or_name)
    print(f"Removed {id_or_name}")


async def vm_shell(id_or_name: str, shell: str) -> None:
    import os
    import shutil
    import signal as _signal
    import termios
    import tty as _tty

    if not sys.stdin.isatty():
        print("Error: vm shell requires an interactive terminal.", file=sys.stderr)
        sys.exit(1)

    rt = _runtime()
    box = await rt.get(id_or_name)

    def _term_size() -> tuple[int, int]:
        s = shutil.get_terminal_size()
        return s.lines, s.columns

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    # Default to 1 so a setup-time failure (box.exec, resize_tty, …) still
    # propagates a non-zero exit even though `result` was never bound.
    exit_code = 1

    try:
        _tty.setraw(fd)

        rows, cols = _term_size()
        execution = await box.exec(shell, [], tty=True)
        await execution.resize_tty(rows, cols)

        if hasattr(_signal, "SIGWINCH"):

            def _on_resize(*_):
                r, c = _term_size()
                asyncio.run_coroutine_threadsafe(execution.resize_tty(r, c), loop)

            _signal.signal(_signal.SIGWINCH, _on_resize)

        stdin_writer = execution.stdin()
        stdin_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def _on_stdin_readable() -> None:
            try:
                data = os.read(fd, 1024)
                loop.call_soon_threadsafe(stdin_queue.put_nowait, data if data else None)
            except OSError:
                loop.call_soon_threadsafe(stdin_queue.put_nowait, None)

        loop.add_reader(fd, _on_stdin_readable)

        async def _stdin_fwd() -> None:
            try:
                while not stop.is_set():
                    try:
                        data = await asyncio.wait_for(stdin_queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue
                    if data is None:
                        break
                    await stdin_writer.send_input(data)
            except Exception:
                pass

        async def _stdout_fwd() -> None:
            try:
                async for chunk in execution.stdout():
                    raw = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else chunk
                    sys.stdout.buffer.write(raw)
                    sys.stdout.buffer.flush()
            except Exception:
                pass
            finally:
                stop.set()

        async def _stderr_fwd() -> None:
            try:
                async for chunk in execution.stderr():
                    if chunk:
                        raw = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else chunk
                        sys.stdout.buffer.write(raw)
                        sys.stdout.buffer.flush()
            except Exception:
                pass

        await asyncio.gather(_stdin_fwd(), _stdout_fwd(), _stderr_fwd(), return_exceptions=True)
        result = await execution.wait()
        exit_code = result.exit_code

    finally:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        if hasattr(_signal, "SIGWINCH"):
            _signal.signal(_signal.SIGWINCH, _signal.SIG_DFL)
        sys.stdout.write("\r\n")
        sys.stdout.flush()

    sys.exit(exit_code)


# ── CLI wiring ─────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="boxlite",
        description="Manage boxlite OCI images and VMs.",
    )
    p.add_argument(
        "--home-dir",
        default=None,
        metavar="PATH",
        help="Boxlite runtime home dir (DB, images, layers). "
        "Overrides BOXLITE_HOME and the default ~/.boxlite. "
        "Use raven's data dir to inspect raven-managed VMs, e.g. "
        "$(uv run python -c 'from raven.config.paths import get_sandbox_dir; print(get_sandbox_dir(\"boxlite\"))').",
    )
    sub = p.add_subparsers(dest="resource", required=True)

    # ── image ──
    img = sub.add_parser("image", help="OCI image management")
    img_sub = img.add_subparsers(dest="action", required=True)

    pull_p = img_sub.add_parser("pull", help="Pull an OCI image into the local cache")
    pull_p.add_argument("image", help="Image reference, e.g. ubuntu:22.04")
    pull_p.add_argument(
        "-u",
        "--username",
        default=None,
        help="Registry username (or set BOXLITE_REGISTRY_USERNAME). Credentials are saved to ~/.docker/config.json.",
    )
    pull_p.add_argument(
        "-p",
        "--password",
        default=None,
        help="Registry password / token (or set BOXLITE_REGISTRY_PASSWORD). "
        "Credentials are saved to ~/.docker/config.json.",
    )

    img_sub.add_parser("ls", help="List cached OCI images")

    rm_p = img_sub.add_parser("rm", help="Remove a cached OCI image")
    rm_p.add_argument("image", help="Image reference to remove")
    rm_p.add_argument("--force", action="store_true", help="Remove even if VMs reference it")

    # ── vm ──
    vm = sub.add_parser("vm", help="VM management")
    vm_sub = vm.add_subparsers(dest="action", required=True)

    vm_sub.add_parser("ls", help="List all VMs")

    create_p = vm_sub.add_parser("create", help="Create a VM")
    create_p.add_argument("--image", required=True, help="OCI image for the VM")
    create_p.add_argument("--name", default=None, help="Optional name for the VM")
    create_p.add_argument("--cpus", type=int, default=2, help="vCPU count (default: 2)")
    create_p.add_argument("--memory", type=int, default=2048, metavar="MiB", help="RAM in MiB (default: 2048)")
    create_p.add_argument(
        "--disk", type=int, default=None, metavar="GB", help="Persistent disk in GB (default: ephemeral)"
    )
    create_p.add_argument("--start", action="store_true", help="Boot the VM immediately after creation")

    start_p = vm_sub.add_parser("start", help="Start a stopped VM")
    start_p.add_argument("id_or_name", help="VM ID or name")

    stop_p = vm_sub.add_parser("stop", help="Stop a running VM")
    stop_p.add_argument("id_or_name", help="VM ID or name")

    vmrm_p = vm_sub.add_parser("rm", help="Remove a VM")
    vmrm_p.add_argument("id_or_name", help="VM ID or name")
    vmrm_p.add_argument("--force", action="store_true", help="Stop the VM first if it is running")

    shell_p = vm_sub.add_parser("shell", help="Open an interactive PTY shell in a running VM")
    shell_p.add_argument("id_or_name", help="VM ID or name")
    shell_p.add_argument("--shell", default="/bin/sh", dest="shell_cmd", help="Shell binary to run (default: /bin/sh)")

    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        import boxlite as _  # noqa: F401
    except ImportError:
        print("Error: boxlite is not installed. Run: pip install raven[sandbox]", file=sys.stderr)
        sys.exit(1)

    if args.home_dir is not None:
        _set_home(Path(args.home_dir).expanduser())

    async def _run() -> None:
        if args.resource == "image":
            if args.action == "pull":
                await image_pull(args.image, args.username, args.password)
            elif args.action == "ls":
                image_ls()
            elif args.action == "rm":
                image_rm(args.image, args.force)

        elif args.resource == "vm":
            if args.action == "ls":
                await vm_ls()
            elif args.action == "create":
                await vm_create(args.image, args.name, args.cpus, args.memory, args.disk, args.start)
            elif args.action == "start":
                await vm_start(args.id_or_name)
            elif args.action == "stop":
                await vm_stop(args.id_or_name)
            elif args.action == "rm":
                await vm_rm(args.id_or_name, args.force)
            elif args.action == "shell":
                await vm_shell(args.id_or_name, args.shell_cmd)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
