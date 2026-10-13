"""A file call whose path is on a registered machine, answered by that machine's Raven node.

``read_file``, ``list_dir``, ``find``, ``grep``, ``write_file`` and
``edit_file`` take a remote path (:mod:`raven.ops.remote_path`) as readily as
a local one. A remote path never touches this computer's disk: it is placed
on its machine -- absolute, against the login home the node reported --
fenced to the directory the permission check allowed, and sent to the
machine's node (:mod:`raven.node.client`), which runs the very tool the call
named there. Only the answer comes back, and every path in it is shown the
way scp writes it, with the registry's user, address and port.

A read remembers the file's digest for the agent that read it -- the
conversation, and the sub-agent run inside it; a write by that agent that
replaces the whole file then tells the node what it expects to replace, and
a file changed in between is left alone, with a sentence that says to read
it again. Another agent's read does not vouch for it, and an agent that
never read the file replaces it, as a write here does.

Every failure is one sentence the tool returns: a path that names no
registered machine, a machine without this Raven's node, one that does not
answer. The file's own lines -- a read's numbered lines, a search's matches,
a diff's body -- come back as they are: only what the tool says about paths
is shown as addresses, so the model edits against the bytes the file holds.
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
    reader = _reader()
    try:
        home = await pool.home(where.machine)
        place = where.absolute(home)
        roots = [remote_path.fence(tool, where, place, home)]
        sent = {**params, "path": place, "roots": roots}
        if (
            tool == "write_file"
            and params.get("mode") != "append"
            and (read := remote_path.last_read(where.machine, place, reader=reader))
        ):
            # What this agent last read there, so a file changed since is
            # not written over unseen (``fs/write``'s ``expect``).
            sent["expect"] = read
        got = await pool.call(where.machine, _METHODS[tool], sent)
    except remote_path.NodeCallError as exc:
        if exc.reason == "changed":
            remote_path.forget_read(where.machine, place, reader=reader)
            return (
                f"Error: {where.display(place)} on machine {remote_path._label(where._row)} changed after it was "
                "read; nothing was written. Read it again before writing."
            )
        return f"Error: {exc}"
    if tool == "read_file" and isinstance(got.get("sha256"), str):
        remote_path.remember_read(where.machine, place, got["sha256"], reader=reader)
    elif tool in ("write_file", "edit_file"):
        remote_path.forget_read(where.machine, place, reader=reader)
    return _shown(got, where, [place, *roots], tool)


def _reader() -> str:
    """Who is reading: the conversation, and the sub-agent run inside it, whose later write expects what it read.

    The permission turn names the conversation; a delegated run inside one is
    told apart by its activity's uid, since the sub-agents of a turn read and
    write on their own. Empty outside both, where every call is one reader.
    """
    # Imported here, both: a machine's node imports the file tools and runs
    # them with only its own requirements importable, and neither the
    # permission turn nor the sub-agent package is among them. This runs on
    # the host alone, once a path has named a machine.
    from raven.agent.subagent import activity
    from raven.permissions.turn import current_turn

    run = activity.current()
    return "\x00".join(part for part in (current_turn().session_key, run.uid if run is not None else "") if part)


#: The lines of a tool's answer that are the file's own bytes, which no path
#: display may rewrite: a read's numbered lines, a search's matches and
#: context (``path:line:text`` and ``path-line-text``). What the tool says
#: about paths -- its errors, its headers and footers -- is shown as
#: addresses; the content stays what the model will edit against.
_CONTENT = {
    "read_file": re.compile(r"^\d+\| "),
    "grep": re.compile(r"^.+?[:-]\d+[:-]"),
}
_DIFF_HEADER = ("--- ", "+++ ")


def _shown(got: dict[str, Any], where: RemotePath, paths: list[str], tool: str) -> str | ToolResult:
    """The node's answer with each path it was given shown as scp writes it, outside the file's own lines.

    One pass, longest path first: a shown path contains the plain one, and a
    second pass over it would show it twice. A diff's content lines are left
    alone too; only its two header lines name the file.
    """
    swaps = sorted({p for p in paths if p.startswith("/")}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(p) for p in swaps)) if swaps else None
    content = _CONTENT.get(tool)

    def show(text: Any) -> str:
        out = str(text or "")
        return pattern.sub(lambda m: where.display(m.group(0)), out) if pattern else out

    def shown(text: Any) -> str:
        """``text`` with its paths shown, line by line, the content lines untouched."""
        if content is None:
            return show(text)
        return "\n".join(line if content.match(line) else show(line) for line in str(text or "").split("\n"))

    def shown_diff(diff: str) -> str:
        return "\n".join(show(line) if line.startswith(_DIFF_HEADER) else line for line in diff.split("\n"))

    text = shown(got.get("text"))
    if "blocks" not in got:
        return text
    blocks = []
    for block in got.get("blocks") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            block = {**block, "text": shown(block.get("text"))}
        blocks.append(block)
    display = got.get("display")
    change = got.get("file_change")
    return ToolResult(
        model_text=text,
        display_text=shown(display) if display is not None else None,
        ok=bool(got.get("ok", True)),
        blocks=blocks,
        # A write's account of what it changed, for a surface with a diff
        # view of its own: the file's whole content both ways, under the
        # path as scp writes it.
        diff=shown_diff(got["diff"]) if isinstance(got.get("diff"), str) else None,
        file_change=FileChange(
            path=show(change.get("path")), after=str(change.get("after") or ""), before=change.get("before")
        )
        if isinstance(change, dict)
        else None,
    )


__all__ = ["answer"]
