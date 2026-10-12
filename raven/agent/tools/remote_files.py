"""A file call whose path is on a registered machine, answered by that machine's Raven node.

``read_file``, ``list_dir``, ``find``, ``grep``, ``write_file`` and
``edit_file`` take a remote path (:mod:`raven.ops.remote_path`) as readily as
a local one. A remote path never touches this computer's disk: it is placed
on its machine -- absolute, against the login home the node reported --
fenced to the directory the permission check allowed, and sent to the
machine's node (:mod:`raven.node.client`), which runs the very tool the call
named there. Only the answer comes back, and every path in it is shown the
way scp writes it, with the registry's user, address and port (C28).

A read remembers the file's digest; a write that replaces the whole file
then tells the node what it expects to replace, and a file changed in
between is left alone, with a sentence that says to read it again.

Every failure is one sentence the tool returns: a path that names no
registered machine, a machine without this Raven's node, one that does not
answer.
"""

from __future__ import annotations

import re
from typing import Any

from raven.contracts.tool import FileChange, ToolResult
from raven.ops import remote_path
from raven.ops.remote_path import RemotePath, RemotePathError

#: Which node method answers which tool (``raven.node.protocol``; spelled out
#: here because the node imports these tools, and they must not import it back).
_METHODS = {
    "read_file": "fs/read",
    "list_dir": "fs/list",
    "find": "fs/find",
    "grep": "fs/grep",
    "write_file": "fs/write",
    "edit_file": "fs/edit",
}


async def answer(tool: str, path: Any, params: dict[str, Any]) -> str | ToolResult | None:
    """``tool``'s answer for ``path`` on its machine, or ``None`` when ``path`` is local.

    ``params`` are the call's other arguments, passed to the node as they are.
    """
    if not isinstance(path, str):
        return None
    try:
        where = remote_path.target(path)
    except RemotePathError as exc:
        return f"Error: {exc}"
    if where is None:
        return None
    pool = remote_path.nodes()
    if pool is None:
        return f"Error: {path} is on another machine, and this process does not read registered machines' files"
    try:
        home = await pool.home(where.machine)
        place = where.absolute(home)
        roots = [remote_path.fence(tool, where, place, home)]
        sent = {**params, "path": place, "roots": roots}
        if (
            tool == "write_file"
            and params.get("mode") != "append"
            and (read := remote_path.last_read(where.machine, place))
        ):
            # What this process last read there, so a file changed since is
            # not written over unseen (``fs/write``'s ``expect``).
            sent["expect"] = read
        got = await pool.call(where.machine, _METHODS[tool], sent)
    except remote_path.NodeCallError as exc:
        if exc.reason == "changed":
            remote_path.forget_read(where.machine, place)
            return (
                f"Error: {where.display(place)} on machine {remote_path._label(where._row)} changed after it was "
                "read; nothing was written. Read it again before writing."
            )
        return f"Error: {exc}"
    if tool == "read_file" and isinstance(got.get("sha256"), str):
        remote_path.remember_read(where.machine, place, got["sha256"])
    elif tool in ("write_file", "edit_file"):
        remote_path.forget_read(where.machine, place)
    return _shown(got, where, [place, *roots])


def _shown(got: dict[str, Any], where: RemotePath, paths: list[str]) -> str | ToolResult:
    """The node's answer with each path it was given shown as scp writes it.

    One pass, longest path first: a shown path contains the plain one, and a
    second pass over it would show it twice.
    """
    swaps = sorted({p for p in paths if p.startswith("/")}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(p) for p in swaps)) if swaps else None

    def show(text: Any) -> str:
        out = str(text or "")
        return pattern.sub(lambda m: where.display(m.group(0)), out) if pattern else out

    text = show(got.get("text"))
    if "blocks" not in got:
        return text
    blocks = []
    for block in got.get("blocks") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            block = {**block, "text": show(block.get("text"))}
        blocks.append(block)
    display = got.get("display")
    change = got.get("file_change")
    return ToolResult(
        model_text=text,
        display_text=show(display) if display is not None else None,
        ok=bool(got.get("ok", True)),
        blocks=blocks,
        # A write's account of what it changed, for a surface with a diff
        # view of its own: the file's whole content both ways, under the
        # path as scp writes it.
        diff=show(got["diff"]) if isinstance(got.get("diff"), str) else None,
        file_change=FileChange(
            path=show(change.get("path")), after=str(change.get("after") or ""), before=change.get("before")
        )
        if isinstance(change, dict)
        else None,
    )


__all__ = ["answer"]
