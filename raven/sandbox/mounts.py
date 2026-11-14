"""Host-to-guest path mapping for the directories mounted into a sandbox."""

from __future__ import annotations

import logging
import re
from pathlib import Path, PurePath

logger = logging.getLogger(__name__)

WORKSPACE_MOUNT = "/workspace"


class MountTable:
    """The workspace mount plus any extra ``[host, guest, "ro"|"rw"]`` volumes."""

    def __init__(self, workspace: Path, extra_volumes: list[list[str]]) -> None:
        self.workspace = Path(workspace)
        self.extra_volumes = [list(entry) for entry in extra_volumes]

    def mounts(self) -> list[tuple[str, str, bool]]:
        """``(host, guest, read_only)`` for every mount, the workspace first."""
        entries = [(str(self.workspace), WORKSPACE_MOUNT, False)]
        entries.extend((host, guest, mode == "ro") for host, guest, mode in self.extra_volumes)
        return entries

    def roots(self) -> list[tuple[PurePath, str]]:
        """Every volume's host root and guest mount, longest host root first.

        A nested mount has to win over its parent: a directory mounted on its
        own must not be translated through the wider root that happens to
        contain it.
        """
        roots: list[tuple[PurePath, str]] = [(self.workspace.resolve(), WORKSPACE_MOUNT)]
        roots.extend((Path(host).resolve(), guest) for host, guest, _mode in self.extra_volumes)
        roots.sort(key=lambda item: len(str(item[0])), reverse=True)
        return roots

    def translate_cwd(self, cwd: str | None) -> str:
        """Map a host path to its guest path, falling back to the workspace mount.

        A one-shot command still has work to do in the wrong directory, so an
        unmapped cwd is logged and replaced rather than refused.
        """
        guest = self._lookup(cwd)
        if guest is None:
            logger.warning("cwd '%s' is outside every mounted volume; falling back to %s", cwd, WORKSPACE_MOUNT)
            return WORKSPACE_MOUNT
        return guest

    def translate_process_cwd(self, cwd: str | None) -> str:
        """``translate_cwd`` for a long-running server, which must be exact.

        A server launched in the wrong directory answers with the wrong files
        for as long as it lives, so an unmapped cwd is refused.
        """
        guest = self._lookup(cwd)
        if guest is None:
            raise ValueError(
                f"the working directory {cwd!r} is not inside any volume mounted into the "
                "sandbox; mount its parent directory or run this server without the sandbox"
            )
        return guest

    def translate_process_path(self, text: str) -> str:
        """Rewrite every mounted host root inside *text* to its guest path.

        A package server's command, its placeholder-expanded arguments and
        its PLUGIN_ROOT/PLUGIN_DATA variables all carry host paths; the
        sandbox only sees the guest paths.
        """
        if not text:
            return text
        for host_root, guest_root in self.roots():
            host = str(host_root)
            windows = "\\" in host
            # The suffix stops at the next occurrence of this root, so re.sub
            # resumes there and translates it too; it must not swallow one.
            suffix = r"(?:[\\/](?:(?!" + re.escape(host) + r")[^:;])*)?"
            text = re.sub(
                re.escape(host) + r"(?=$|[\\/])(?P<suffix>" + suffix + r")",
                lambda match, guest=guest_root, windows=windows: (
                    guest + (match.group("suffix").replace("\\", "/") if windows else match.group("suffix"))
                ),
                text,
            )
        return text

    @staticmethod
    def guest_mount_path(guest_root: str, relative: PurePath) -> str:
        suffix = "/".join(relative.parts)
        return guest_root if not suffix else f"{guest_root}/{suffix}"

    def _lookup(self, cwd: str | None) -> str | None:
        if cwd is None:
            return WORKSPACE_MOUNT
        host_path = Path(cwd).resolve()
        for host_root, guest_root in self.roots():
            try:
                rel = host_path.relative_to(host_root)
            except ValueError:
                continue
            return self.guest_mount_path(guest_root, rel)
        return None
