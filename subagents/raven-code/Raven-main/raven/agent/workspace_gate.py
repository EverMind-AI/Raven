"""First-write gate: allocate the workspace before the first mutating tool call.

Run as a host raven's Raven-Code sub-agent, this raven inherits the HOST's
working directory. Reading it is always safe; writing it is not: the user may
have uncommitted work in it, and another Raven-Code instance may already be
writing to it. So the first tool call that could modify the workspace (or its
git state) passes through this gate, which atomically decides what this
instance is allowed to write to:

- nobody owns the primary checkout and it is clean -> this instance binds it;
- this instance already bound something -> reuse the binding;
- the checkout is dirty, or another Raven-Code instance owns it -> the user is
  asked (worktree / read-only / retry later).

How the user is asked depends on the transport:

- **In-turn (ACP, or any transport that wired a QuestionBroker):** the gate's
  ``ask`` hook raises the question through the runtime's own ask_user broker -
  over ACP that becomes an ``elicitation/create`` (or a marked synthetic
  permission request) to the client - and the SAME turn continues with the
  answer: approval creates the worktree, rebinds the session's working
  directory through the ``rebind`` hook, and the model is told to re-issue its
  change there.
- **Between turns (plain CLI, no broker):** the question is persisted to the
  allocation record and the turn is stopped; the launcher (run.py in the
  integration repo) relays the question as the reply and parses the next turn
  of the same conversation as the answer. A deliberately marked fallback, not
  an imitation of the native path.

Read-only tools never trigger allocation, so an instance that only analyzes
code allocates nothing.

The gate is ARMED ONLY by the launcher's environment. Two layouts:

- ``RAVEN_WORKSPACE_ALLOC_DIR`` + ``RAVEN_WORKSPACE_ALLOC_INSTANCE``: one
  process serves one conversation (the CLI launcher); the allocation record is
  ``<DIR>/allocation.json``.
- ``RAVEN_WORKSPACE_ALLOC_BASE``: one process serves many sessions (the ACP
  server multiplexes them on one connection); each session's record is
  ``<BASE>/allocations/<session>.json`` and the identity is the session key
  the ``cid`` hook reports.

``RAVEN_WORKSPACE_ALLOC_REPOS`` names the repo-level records (locks, owners,
worktrees) in both layouts. Without the env - evals, plain ``raven agent``,
every other use of this build - ``build_workspace_gate`` returns None and
nothing here runs.

Scope is Raven-Code's own concurrency: the owner records name Raven-Code
instances. Other sub-agents and the host itself are not tracked, not blocked.

Atomicity: check-and-bind runs under an exclusive flock on
``<repos>/<repo-id>/alloc.lock``, held only for the allocation itself
(sub-second) - never across a human answer and never for the coding task. The
kernel releases it if the process dies; the durable facts are files written
atomically (tmp + rename), and re-running the allocation converges on them.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from loguru import logger

ALLOC_DIR_ENV = "RAVEN_WORKSPACE_ALLOC_DIR"
INSTANCE_ENV = "RAVEN_WORKSPACE_ALLOC_INSTANCE"
ALLOC_BASE_ENV = "RAVEN_WORKSPACE_ALLOC_BASE"
REPOS_ENV = "RAVEN_WORKSPACE_ALLOC_REPOS"

ALLOCATION_FILE = "allocation.json"

# The three answers the question offers. Written out here once; the launcher's
# between-turns parser and the in-turn parser both match against these.
CHOICE_WORKTREE = "worktree"
CHOICE_READONLY = "readonly"
CHOICE_RETRY = "retry_later"

# Tools that mutate the workspace by name. Everything not listed here and not
# `exec` is read-only by construction (read_file, grep, find, list_dir,
# todowrite, web_*, ask_user - none of them touch the checkout).
WRITE_TOOLS = frozenset({"write_file", "edit_file", "exec_write", "apply_patch"})

# Commands whose every segment is one of these run without allocation. The
# asymmetry is deliberate: misclassifying a read as a write costs one early
# (invisible) allocation or one unnecessary question; misclassifying a write
# as a read defeats the gate. So the list is narrow and anything unparsed is
# treated as a write.
_READONLY_CMDS = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "stat",
        "file",
        "du",
        "df",
        "pwd",
        "echo",
        "printf",
        "env",
        "which",
        "whereis",
        "type",
        "uname",
        "date",
        "grep",
        "rg",
        "egrep",
        "fgrep",
        "find",
        "fd",
        "tree",
        "realpath",
        "readlink",
        "basename",
        "dirname",
        "sort",
        "uniq",
        "cut",
        "tr",
        "diff",
        "cmp",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "cksum",
        "true",
        ":",
        "test",
        "[",
    }
)
_READONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "show",
        "diff",
        "rev-parse",
        "ls-files",
        "ls-tree",
        "blame",
        "shortlog",
        "describe",
        "grep",
        "cat-file",
        "remote",
    }
)
_SHELL_SPLIT_RE = re.compile(r"&&|\|\||;|\||\n")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# Writing escape hatches of otherwise-read-only heads, by option SEMANTICS
# rather than by literal strings: a short option engages as its own token, in
# a getopt cluster, or with an attached value ("-oout.txt", "-xrm"); a long
# option engages bare (value separated) or with "=value" attached. Anything a
# cluster COULD mean engages the conservative branch - "-no" may be
# "-n -o out"; proving it harmless is the classifier's job, not the reader's.
_WRITING_OPTIONS: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {
    # sort -o/--output writes the result to a file.
    "sort": (frozenset("o"), ("--output",)),
    # fd -x/--exec and -X/--exec-batch run an arbitrary command per result.
    "fd": (frozenset("xX"), ("--exec", "--exec-batch")),
}


def _writes_via_options(head: str, args: list[str]) -> bool:
    spec = _WRITING_OPTIONS.get(head)
    if spec is None:
        return False
    letters, longs = spec
    for arg in args:
        if arg == "--":
            break  # everything after is positional; options cannot follow
        if arg.startswith("--"):
            if any(arg == flag or arg.startswith(flag + "=") for flag in longs):
                return True
        elif arg.startswith("-") and len(arg) > 1:
            # A short-option token: any writing letter anywhere in it counts,
            # because clusters and attached values are indistinguishable
            # without the full option table ("-no" may mean "-n -o ...").
            if any(ch in letters for ch in arg[1:]):
                return True
    return False


# Git global options that appear BEFORE the subcommand. Flags take no value;
# valued options consume the next word or carry it attached ("-Cpath",
# "-cname=v", "--git-dir=path").
_GIT_GLOBAL_FLAGS = frozenset(
    {
        "--no-pager",
        "--paginate",
        "-p",
        "-P",
        "--bare",
        "--no-replace-objects",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-optional-locks",
        "--no-lazy-fetch",
        "--no-advice",
    }
)
_GIT_GLOBAL_VALUED = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path")
_GIT_GLOBAL_VALUED_LONG = tuple(opt for opt in _GIT_GLOBAL_VALUED if opt.startswith("--"))


def _git_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    """Split git's leading global options off; returns (subcommand, its args).

    ``(None, [])`` when there is no subcommand or a global option is not
    recognized - the caller fails closed on that, because a misread global
    option can smuggle its value in as the "subcommand" (``git -C status``
    runs nothing of the sort).
    """
    i = 0
    n = len(args)
    while i < n:
        word = args[i]
        if not word.startswith("-"):
            return word, args[i + 1 :]
        if word in _GIT_GLOBAL_FLAGS:
            i += 1
            continue
        if word in _GIT_GLOBAL_VALUED:
            if i + 1 >= n:
                return None, []  # a dangling value slot is not a real invocation
            i += 2
            continue
        if any(word.startswith(opt + "=") for opt in _GIT_GLOBAL_VALUED_LONG):
            i += 1
            continue
        if (word.startswith("-C") or word.startswith("-c")) and len(word) > 2 and not word.startswith("--"):
            i += 1  # attached value form: -Cpath / -cname=value
            continue
        return None, []  # unknown global option: fail closed
    return None, []


def _git_output_flag(args: list[str]) -> bool:
    """True when a normally-read-only git subcommand is told to write a file
    (``diff``/``show``/``log`` accept ``--output[=]<file>`` and ``-o<file>``)."""
    for arg in args:
        if arg == "--":
            break
        if arg == "--output" or arg.startswith("--output="):
            return True
        if arg.startswith("-o") and not arg.startswith("--"):
            return True
    return False


def _is_readonly_command(command: str) -> bool:
    """True only when every segment of ``command`` is provably read-only.

    Conservative by principle: what cannot be PROVEN read-only is a write.
    Readers with an escape hatch get that hatch closed by option semantics
    (see ``_WRITING_OPTIONS``): ``find``'s action predicates, ``fd``'s exec
    options, and the output options that turn ``sort``, ``git diff`` or
    ``git show`` into file writers - attached, separated and clustered forms
    alike. ``env`` is a wrapper, not a command: whatever it wraps is what
    gets classified. Git's leading global options are parsed off before the
    subcommand is judged, so ``git -C <path> status`` stays read-only while
    an unrecognized global option fails closed.
    """
    if not command.strip():
        return True
    # Command substitution or redirection can hide a write inside a segment
    # whose head looks read-only.
    if any(tok in command for tok in ("$(", "`", ">", "<<")):
        return False
    for segment in _SHELL_SPLIT_RE.split(command):
        try:
            words = shlex.split(segment, posix=True) if segment.strip() else []
        except ValueError:
            # Unparseable shell (e.g. the trailing `\` the splitter leaves of
            # `find -exec ... \;`): cannot be proven read-only, so it is not.
            return False
        # Strip leading VAR=... assignments and no-op wrappers. `env` counts:
        # `env touch x` runs touch, and `env` alone prints (empty rest below).
        while words and (_ENV_ASSIGN_RE.match(words[0]) or words[0] in ("command", "builtin", "env")):
            words = words[1:]
        if not words:
            continue
        head, args = words[0], words[1:]
        if head == "git":
            sub, sub_args = _git_subcommand(args)
            if sub is None or sub not in _READONLY_GIT_SUBCOMMANDS:
                return False
            if sub == "remote" and sub_args and sub_args[0] not in ("-v", "show"):
                return False
            if _git_output_flag(sub_args):
                return False
            continue
        if head == "find":
            # `find`'s own `-o` is the OR operator; its writing action
            # predicates are the hatch, and they are always separate tokens.
            if any(
                a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls") or a.startswith("-fprint") for a in args
            ):
                return False
            continue
        if head not in _READONLY_CMDS:
            return False
        if _writes_via_options(head, args):
            return False
    return True


def parse_choice(text: str, options: list[str]) -> str | None:
    """Map an answer onto one offered option, or None when unclear.

    Deliberately keyword-based rather than clever: an unrecognized answer
    re-asks instead of guessing, because the wrong guess either blocks a task
    the user approved or writes into a tree they wanted protected.
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    # STRICT: the WHOLE answer must be one accepted token - a choice id, its
    # canonical number, or one unambiguous Chinese option word. Substring
    # matching is how "no worktree" / "不要创建 worktree" used to parse as a
    # worktree AUTHORIZATION; an unparseable answer stays read-only and the
    # next write asks again, which costs a question instead of a wrong write.
    t = t.strip("。．.!！?？'\"“”‘’ \t\r\n").rstrip(".)）、，,")
    canonical = {
        "1": CHOICE_WORKTREE,
        "2": CHOICE_READONLY,
        "3": CHOICE_RETRY,
        CHOICE_WORKTREE: CHOICE_WORKTREE,
        CHOICE_READONLY: CHOICE_READONLY,
        CHOICE_RETRY: CHOICE_RETRY,
        "只读": CHOICE_READONLY,
        "稍后": CHOICE_RETRY,
    }
    choice = canonical.get(t)
    return choice if choice is not None and choice in options else None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class WorkspaceGateError(RuntimeError):
    """A gate record could not be read or trusted.

    Raised instead of silently answering ``{}``: a corrupt allocation record
    treated as "no allocation" would send a worktree-bound session back through
    check-and-bind, where a stale ``primary.json`` (or a clean tree) binds it to
    the PRIMARY checkout - the exact misdirected write the gate exists to
    prevent. Mutating calls fail closed on this; read-only calls proceed.
    """


def _read_json(path: Path) -> dict:
    """Parse one gate record. Absent file -> ``{}``; unreadable/corrupt -> raise.

    The distinction is the safety property: "no record" is a normal state the
    state machine handles, while "a record exists but cannot be trusted" must
    stop a write rather than default it to the most permissive branch.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise WorkspaceGateError(f"unreadable record {path.name}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise WorkspaceGateError(f"corrupt record {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        # `[]`, a bare string, a number, `null` - valid JSON, but not a record.
        # Answering `{}` would read as "no allocation" and re-run check-and-bind
        # over a binding whose real shape is unknown.
        raise WorkspaceGateError(f"corrupt record {path.name}: root is {type(data).__name__}, not an object")
    return data


def _safe_segment(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in value.strip())
    return (cleaned.strip(".") or "unnamed")[:120]


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git_binary = shutil.which("git") or "git"
    return subprocess.run(
        [git_binary, "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        timeout=15,
    )


def repo_identity(workspace: Path) -> tuple[str, dict | None]:
    """(repo-id, repo facts) - facts is None outside a usable git repository.

    The id hashes the git *common* dir, so every worktree of one repository
    maps to one id (and one lock, one owner). A directory that is not a git
    repo, or a repo with an unborn HEAD (no commit to base a worktree on),
    gets a path-derived id and ``None``: allocation still serializes on it,
    but the worktree offer is off the table.
    """
    try:
        common = _git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode != 0:  # git < 2.31: no --path-format; answer may be relative
            common = _git(workspace, "rev-parse", "--git-common-dir")
        head = _git(workspace, "rev-parse", "HEAD")
        root = _git(workspace, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired) as exc:
        # git itself failing to RUN is not "this is not a repository" - treating
        # it as one would bind the primary checkout on a blind guess. The gate's
        # caller fails a mutating call closed on this.
        raise WorkspaceGateError(f"git could not run: {exc}") from exc
    if common.returncode != 0 or head.returncode != 0 or root.returncode != 0:
        key = str(workspace.resolve())
        return "path-" + hashlib.sha256(key.encode()).hexdigest()[:16], None
    common_raw = Path(common.stdout.strip())
    common_dir = str(common_raw.resolve() if common_raw.is_absolute() else (workspace / common_raw).resolve())
    return (
        "git-" + hashlib.sha256(common_dir.encode()).hexdigest()[:16],
        {
            "root": root.stdout.strip(),
            "commonDir": common_dir,
            "baseCommit": head.stdout.strip(),
        },
    )


def workspace_dirty(workspace: Path) -> int:
    """Count of uncommitted entries (tracked, staged and untracked alike).

    Only called against a directory ``repo_identity`` already validated, so a
    failure here is infrastructure, not "not a repo" - and answering 0 to it
    would wave a write into a checkout whose dirtiness is unknown. Raise, and
    let the mutating caller fail closed.
    """
    try:
        out = _git(workspace, "status", "--porcelain")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceGateError(f"git status failed: {exc}") from exc
    if out.returncode != 0:
        raise WorkspaceGateError(f"git status failed: {(out.stderr or out.stdout or '').strip()[:200]}")
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def create_worktree(repo: dict, repos_root: Path, repo_id: str, instance: str) -> tuple[Path, str] | str:
    """Create ``instance``'s worktree; returns (path, branch) or an error string.

    Runs under the same repo-level flock the allocation uses, so a creation
    cannot interleave with another instance's check-and-bind. The branch is
    unique per instance; a leftover branch from a crashed attempt is skipped
    with a numeric suffix rather than reused - its state is unknown. The
    worktree directory lives under the state root, never inside the target
    repository's own tree.
    """
    root = Path(repo.get("root", ""))
    base = repo.get("baseCommit", "")
    if not (root.is_dir() and base):
        return "工作目录不是可用的 Git 仓库，无法创建 worktree。"
    rdir = repos_root / repo_id
    rdir.mkdir(parents=True, exist_ok=True)
    wt = rdir / "worktrees" / _safe_segment(instance)
    wt.parent.mkdir(parents=True, exist_ok=True)
    with open(rdir / "alloc.lock", "w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            _git(root, "worktree", "prune")  # clear any crashed half-creation
            last = ""
            for suffix in ("", "-2", "-3"):
                branch = f"raven-code/{_safe_segment(instance)}{suffix}"
                proc = _git(root, "worktree", "add", "-b", branch, str(wt), base)
                if proc.returncode == 0:
                    return wt, branch
                last = (proc.stderr or proc.stdout or "").strip()
                if "already exists" not in last:
                    break
            return f"git worktree add 失败：{last[-300:]}"
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _question_dirty(n: int, sha: str, git_ok: bool) -> str:
    if git_ok:
        return (
            f"检测到当前工作目录存在未提交改动（{n} 处）。为了不影响你进行中的工作，"
            f"本次 Raven-Code 任务可以在独立的 Git worktree 中进行：新 worktree 基于当前 "
            f"HEAD（{sha[:10]}），**不包含尚未提交的修改**。请回复：\n"
            "1. worktree —— 创建独立 worktree，在副本上完成任务\n"
            "2. 只读 —— 本任务不修改任何文件，只做只读分析\n"
            "3. 稍后 —— 等你提交/处理完现有改动后，再在本会话里继续\n"
            "（回复数字或关键词均可；本会话只会问这一次）"
        )
    return (
        "当前工作目录不是可用的 Git 仓库，无法创建隔离副本，而目录内容可能是你未保存的工作。请回复：\n"
        "2. 只读 —— 本任务不修改任何文件\n"
        "3. 稍后 —— 你整理好目录后再继续"
    )


def _question_occupied(owner: str, sha: str, git_ok: bool) -> str:
    if git_ok:
        return (
            f"当前工作目录已由另一个 Raven-Code 实例（{owner}）持有写权限，两个实例同时修改"
            f"同一目录会互相覆盖。本实例可以改在独立的 Git worktree 中工作：基于当前 HEAD"
            f"（{sha[:10]}），**不包含主目录中未提交的内容**。请回复：\n"
            "1. worktree —— 创建独立 worktree，在副本上完成任务\n"
            "2. 只读 —— 本任务不修改任何文件\n"
            "3. 稍后 —— 等另一个任务结束后再继续\n"
            "（回复数字或关键词均可；本会话只会问这一次）"
        )
    return (
        f"当前工作目录已由另一个 Raven-Code 实例（{owner}）持有写权限，且该目录不是 Git 仓库，"
        "无法创建隔离副本。请回复：\n2. 只读 —— 本任务不修改任何文件\n3. 稍后 —— 等另一个任务结束后再继续"
    )


_BLOCK_PENDING = (
    "WRITE BLOCKED: modifying this workspace needs the user's authorization first "
    "(reason: {reason}). The question has been recorded and will reach the user when "
    "this turn ends. Do not attempt any further modification. Briefly summarize what "
    "you planned to change and state that you are waiting for workspace authorization."
)
_BLOCK_DECLINED = (
    "WRITE BLOCKED: the user declined write access for this session. Work strictly "
    "read-only; do not attempt any modification."
)
_BLOCK_NO_ANSWER = (
    "WRITE BLOCKED: the user did not answer the workspace-authorization question "
    "(reason: {reason}). Staying read-only for now; you may finish what is possible "
    "read-only and tell the user the change is waiting on their authorization."
)
_BLOCK_ESCAPE = (
    "WRITE BLOCKED: this session is bound to the worktree {worktree}. Operating on the "
    "primary checkout ({root}) or overriding git's target repository is not allowed. "
    "Work inside the worktree only."
)
_BLOCK_GATE_ERROR = (
    "WRITE BLOCKED: workspace allocation failed before this write could be authorized "
    "(error: {error}). The write was NOT executed and no files were changed. This is "
    "recoverable: once the workspace state is fixed, retry the same operation."
)
_AUTHORIZED_NOTE = (
    "Workspace authorization granted: a dedicated git worktree was created at {worktree} "
    "(branch {branch}, based on commit {base}) and this session now works there. "
    "Uncommitted files from the primary checkout are deliberately absent - do not copy "
    ".env or other local files from it, and do not operate on the primary checkout. "
    "Re-issue your change now; relative paths resolve inside the worktree."
)


class WorkspaceGate:
    """Async callable installed on the ToolRegistry: returns None to allow a
    call, or a terminal string the model receives instead of the tool result.

    Three optional hooks, wired by the agent loop when the transport provides
    them (see ``AgentLoop``'s gate wiring):

    - ``ask(prompt, choices, default) -> str | None``: raise the question
      through the runtime's QuestionBroker and return the user's answer;
      ``None`` when no channel exists (then the between-turns fallback runs).
    - ``rebind(path)``: repoint the running session's working directory.
    - ``cid() -> str``: the current turn's conversation/session key, used as
      the instance identity in the multiplexed (ACP) layout.
    """

    def __init__(
        self,
        workspace: Path,
        repos_root: Path,
        *,
        instance: str = "",
        alloc_dir: Path | None = None,
        alloc_base: Path | None = None,
    ):
        self.workspace = Path(workspace)
        self.repos_root = Path(repos_root)
        self._env_instance = instance
        self._alloc_dir = Path(alloc_dir) if alloc_dir else None
        self._alloc_base = Path(alloc_base) if alloc_base else None
        self.ask: Callable[[str, list[str], str], Awaitable[str | None]] | None = None
        self.rebind: Callable[[Path], None] | None = None
        self.cid: Callable[[], str] | None = None
        # Per-identity cached verdicts: an identity that allocated once is not
        # re-checked on every call, and a terminal refusal repeats itself.
        self._allowed: dict[str, dict] = {}
        self._verdicts: dict[str, str] = {}

    # -- identity and record locations ------------------------------------

    def _identity(self) -> str:
        if self._alloc_dir is not None:
            return self._env_instance or self._alloc_dir.name
        key = self.cid() if self.cid is not None else ""
        return "acp-" + _safe_segment(key or "unkeyed")

    def _allocation_path(self, identity: str) -> Path:
        if self._alloc_dir is not None:
            return self._alloc_dir / ALLOCATION_FILE
        if self._alloc_base is None:
            raise RuntimeError("workspace gate allocation base is not configured")
        return self._alloc_base / "allocations" / f"{_safe_segment(identity)}.json"

    def _current_workspace(self) -> Path:
        try:
            from raven.agent import workdir

            bound = workdir.current()
            if bound is not None:
                return Path(bound)
        except Exception:  # pragma: no cover - workdir must never break the gate
            pass
        return self.workspace

    # -- classification --------------------------------------------------

    def _mutates(self, name: str, params: dict) -> bool:
        if name in WRITE_TOOLS:
            return True
        if name == "exec":
            return not _is_readonly_command(str((params or {}).get("command", "")))
        return False

    # -- escape guard (worktree mode) -------------------------------------

    def _escape_blocked(self, name: str, params: dict, alloc: dict) -> str | None:
        if alloc.get("mode") != "worktree":
            return None
        root = (alloc.get("repo") or {}).get("root") or ""
        wt = alloc.get("boundWorkdir") or ""
        if not root:
            return None
        try:
            root_path = Path(root).resolve()
            worktree_path = Path(wt).resolve()
        except OSError:
            return _BLOCK_ESCAPE.format(worktree=wt, root=root)

        def outside_worktree_in_primary(path: Path) -> bool:
            try:
                resolved = path.resolve()
            except OSError:
                return True
            in_primary = resolved == root_path or root_path in resolved.parents
            in_worktree = resolved == worktree_path or worktree_path in resolved.parents
            return in_primary and not in_worktree

        if name in ("write_file", "edit_file"):
            raw = str((params or {}).get("file_path") or (params or {}).get("path") or "")
            if raw:
                p = Path(raw)
                candidate = p if p.is_absolute() else worktree_path / p
                if outside_worktree_in_primary(candidate):
                    return _BLOCK_ESCAPE.format(worktree=wt, root=root)
        elif name in ("exec", "exec_write"):
            cmd = str((params or {}).get("command" if name == "exec" else "input", ""))
            explicit_cwd = str((params or {}).get("working_dir") or "")
            session_cwd = str((params or {}).get("_raven_session_workdir") or "")
            mutating = not _is_readonly_command(cmd)
            if explicit_cwd and outside_worktree_in_primary(Path(explicit_cwd)) and mutating:
                return _BLOCK_ESCAPE.format(worktree=wt, root=root)
            if session_cwd and outside_worktree_in_primary(Path(session_cwd)) and mutating:
                return _BLOCK_ESCAPE.format(worktree=wt, root=root)
            primary_refs = {str(root_path), str(Path(root).expanduser())}
            traversal_ref = "../" in cmd or "..\\" in cmd
            direct_primary_ref = any(
                re.search(
                    r"(?<![\w./-])" + re.escape(primary) + r"(?=$|[/\s'\";|&)])",
                    cmd,
                )
                for primary in primary_refs
            )
            suspicious = (
                traversal_ref
                or direct_primary_ref
                or "GIT_DIR=" in cmd
                or "GIT_WORK_TREE=" in cmd
                or "--git-dir" in cmd
                or "--work-tree" in cmd
                or re.search(r"\bcd\s+" + re.escape(root) + r"(\s|/|$|['\"])", cmd)
            )
            if suspicious and mutating:
                return _BLOCK_ESCAPE.format(worktree=wt, root=root)
        return None

    # -- allocation --------------------------------------------------------

    def _decide_under_lock(self, identity: str, workspace: Path) -> tuple[str, dict]:
        """One pass of check-and-bind. Returns (outcome, detail).

        Outcomes: ``allowed`` (bound, detail=allocation), ``ask_dirty`` /
        ``ask_occupied`` (detail carries question facts), ``declined``.
        """
        alloc_path = self._allocation_path(identity)
        alloc = _read_json(alloc_path)
        if alloc.get("state") == "working":
            return "allowed", alloc
        if alloc.get("state") == "readonly_locked":
            return "declined", alloc

        repo_id, repo = repo_identity(workspace)
        repo_dir = self.repos_root / repo_id
        repo_dir.mkdir(parents=True, exist_ok=True)

        with open(repo_dir / "alloc.lock", "w", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                alloc = _read_json(alloc_path)
                if alloc.get("state") == "working":
                    return "allowed", alloc

                owner = _read_json(repo_dir / "primary.json").get("instance")
                now = int(time.time() * 1000)
                base = {
                    "version": 1,
                    "instance": identity,
                    "requestedWorkdir": str(workspace.resolve()),
                    "repoId": repo_id,
                    "repo": repo,
                }
                if owner in (None, "", identity):
                    rebinding = owner == identity
                    dirty = 0 if (rebinding or repo is None) else workspace_dirty(workspace)
                    if repo is not None and dirty and not rebinding:
                        return "ask_dirty", {**base, "dirty": dirty}
                    _write_json(repo_dir / "primary.json", {"instance": identity, "boundAtMs": now})
                    record = {
                        **base,
                        "state": "working",
                        "mode": "primary",
                        "boundWorkdir": str(workspace.resolve()),
                        "decision": {"by": "auto", "atMs": now},
                    }
                    _write_json(alloc_path, record)
                    logger.info("workspace gate: {} bound primary checkout ({})", identity, workspace)
                    return "allowed", record
                return "ask_occupied", {**base, "owner": owner}
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

    def _bind_worktree(self, identity: str, detail: dict, decided_by: str) -> tuple[str, dict] | str:
        """Create and record this identity's worktree; error string on failure."""
        repo = detail.get("repo") or {}
        created = create_worktree(repo, self.repos_root, detail["repoId"], identity)
        if isinstance(created, str):
            return created
        wt, branch = created
        record = {
            **{k: detail[k] for k in ("version", "instance", "requestedWorkdir", "repoId", "repo")},
            "state": "working",
            "mode": "worktree",
            "boundWorkdir": str(wt),
            "branch": branch,
            "decision": {"by": decided_by, "choice": CHOICE_WORKTREE, "atMs": int(time.time() * 1000)},
        }
        _write_json(self._allocation_path(identity), record)
        logger.info("workspace gate: {} bound worktree {} ({})", identity, wt, branch)
        return "bound", record

    def _lock_readonly(self, identity: str, detail: dict, decided_by: str) -> None:
        record = {
            **{k: detail.get(k) for k in ("version", "instance", "requestedWorkdir", "repoId", "repo")},
            "state": "readonly_locked",
            "decision": {"by": decided_by, "choice": CHOICE_READONLY, "atMs": int(time.time() * 1000)},
        }
        _write_json(self._allocation_path(identity), record)

    def _write_pending(self, identity: str, detail: dict, reason: str, question: str, options: list[str]) -> None:
        record = {
            **{k: detail.get(k) for k in ("version", "instance", "requestedWorkdir", "repoId", "repo")},
            "state": "awaiting_user",
            "mode": None,
            "pending": {
                "reason": reason,
                "question": question,
                "options": options,
                "askedAtMs": int(time.time() * 1000),
            },
        }
        _write_json(self._allocation_path(identity), record)

    def _question_for(self, outcome: str, detail: dict) -> tuple[str, str, list[str]]:
        git_ok = detail.get("repo") is not None
        sha = (detail.get("repo") or {}).get("baseCommit", "")
        if outcome == "ask_dirty":
            reason = "uncommitted changes in the workspace"
            question = _question_dirty(int(detail.get("dirty", 0)), sha, git_ok)
        else:
            owner = str(detail.get("owner", ""))
            reason = f"the workspace is held by instance {owner}"
            question = _question_occupied(owner, sha, git_ok)
        options = [CHOICE_WORKTREE, CHOICE_READONLY, CHOICE_RETRY] if git_ok else [CHOICE_READONLY, CHOICE_RETRY]
        return reason, question, options

    async def _allocate(self, identity: str) -> str | None:
        """Bind or refuse. Returns None on allow, else the block text."""
        workspace = self._current_workspace()
        alloc_path = self._allocation_path(identity)
        pre = _read_json(alloc_path)
        if pre.get("state") == "awaiting_user" and self.ask is None:
            # Between-turns fallback: the launcher answers before running a
            # task, so reaching this means the pending question is still open.
            return _BLOCK_PENDING.format(reason=pre.get("pending", {}).get("reason", "pending"))

        outcome, detail = self._decide_under_lock(identity, workspace)
        if outcome == "allowed":
            self._allowed[identity] = detail
            self._maybe_rebind(detail, workspace)
            return None
        if outcome == "declined":
            return _BLOCK_DECLINED

        reason, question, options = self._question_for(outcome, detail)

        if self.ask is None:
            # No live channel: persist and stop the turn (the launcher relays).
            self._write_pending(identity, detail, reason, question, options)
            logger.info("workspace gate: {} awaiting user ({})", identity, reason)
            return _BLOCK_PENDING.format(reason=reason)

        # In-turn: ask through the runtime's broker and act on the answer now.
        # The allocation lock is NOT held across this await - a human answer
        # takes arbitrarily long, and the lock only guards check-and-bind.
        answer = await self.ask(question, options, "")
        if answer is None:
            self._write_pending(identity, detail, reason, question, options)
            return _BLOCK_PENDING.format(reason=reason)
        choice = parse_choice(answer, options)
        logger.info("workspace gate: {} answered {!r} -> {}", identity, answer[:80], choice)
        if choice == CHOICE_WORKTREE:
            bound = self._bind_worktree(identity, detail, decided_by="user")
            if isinstance(bound, str):
                return _BLOCK_NO_ANSWER.format(reason=f"worktree creation failed: {bound}")
            _, record = bound
            self._allowed[identity] = record
            self._maybe_rebind(record, workspace)
            return _AUTHORIZED_NOTE.format(
                worktree=record["boundWorkdir"],
                branch=record["branch"],
                base=(record.get("repo") or {}).get("baseCommit", "")[:12],
            )
        if choice == CHOICE_READONLY:
            self._lock_readonly(identity, detail, decided_by="user")
            return _BLOCK_DECLINED
        # retry_later, empty (broker timeout/cancel) or unparseable: stay
        # read-only for this turn without locking the session - the next write
        # attempt asks again.
        return _BLOCK_NO_ANSWER.format(reason=reason)

    def _maybe_rebind(self, alloc: dict, workspace: Path) -> None:
        bound = alloc.get("boundWorkdir")
        if not bound or self.rebind is None:
            return
        try:
            if Path(bound).resolve() != workspace.resolve():
                self.rebind(Path(bound))
        except OSError:  # pragma: no cover
            pass

    # -- entry point -------------------------------------------------------

    async def __call__(self, name: str, params: dict) -> str | None:
        # Classified first and outside the main try: the answer decides which
        # failure mode is safe. An unclassifiable call counts as mutating -
        # the same asymmetry the command classifier applies.
        try:
            mutating = self._mutates(name, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("workspace gate: classification failed, treating {} as a write: {}", name, exc)
            mutating = True
        try:
            identity = self._identity()
            allowed = self._allowed.get(identity)
            if allowed is None:
                # A process serving a resumed conversation converges on the
                # persisted record without re-deciding.
                persisted = _read_json(self._allocation_path(identity))
                if persisted.get("state") == "working":
                    allowed = self._allowed[identity] = persisted
            if allowed is not None:
                self._maybe_rebind(allowed, self._current_workspace())
                return self._escape_blocked(name, params, allowed)
            if not mutating:
                return None
            if identity in self._verdicts:
                return self._verdicts[identity]
            blocked = await self._allocate(identity)
            if blocked is None:
                allowed = self._allowed.get(identity) or _read_json(self._allocation_path(identity))
                return self._escape_blocked(name, params, allowed)
            if blocked.startswith("WRITE BLOCKED: the user declined"):
                self._verdicts[identity] = blocked
            return blocked
        except Exception as exc:
            # Never crash a turn - but never let a broken allocation path fall
            # through to the workspace either. Reads keep working (they cannot
            # misdirect a write); a mutating call fails CLOSED with a
            # recoverable error the model can relay, and is not cached, so a
            # later retry re-runs the whole allocation.
            if mutating:
                logger.warning("workspace gate error (blocking write {}): {}", name, exc)
                detail = str(exc)[:200] or exc.__class__.__name__
                return _BLOCK_GATE_ERROR.format(error=detail)
            logger.warning("workspace gate error (allowing read-only {}): {}", name, exc)
            return None


def _armed_base() -> tuple[Path, Path] | None:
    """(alloc base, repos root) in the multiplexed layout, or None when unarmed."""
    repos = os.environ.get(REPOS_ENV, "").strip()
    base = os.environ.get(ALLOC_BASE_ENV, "").strip()
    if not (repos and base):
        return None
    return Path(base), Path(repos)


def _identity_for_session(session_key: str) -> str:
    return "acp-" + _safe_segment(session_key or "unkeyed")


def _worktree_is_clean(wt: Path, base_commit: str) -> bool:
    """No worktree content and no commits past the base: nothing to lose."""
    status = _git(wt, "status", "--porcelain", "--untracked-files=all", "--ignored=matching")
    if status.returncode != 0 or any(line.strip() for line in status.stdout.splitlines()):
        return False
    ahead = _git(wt, "rev-list", "--count", f"{base_commit}..HEAD")
    return ahead.returncode == 0 and ahead.stdout.strip() == "0"


def release_for_session(session_key: str) -> dict | None:
    """Release what the gate holds for one deleted session. Never deletes work.

    The other half of the lifecycle: the host's ``instance.forget`` sends
    ``session/delete``, and the conversation being gone is what frees the
    workspace for the next Raven-Code instance. Two cases:

    - **primary**: the owner record is removed (under the same allocation
      lock), so the next instance binds the checkout instead of being sent to
      a worktree over a ghost.
    - **worktree**: removed ONLY when it is provably empty-handed (clean tree,
      zero commits past base). A worktree with any content stays on disk with
      its allocation record - deleting a conversation must not delete work;
      integration and cleanup of real changes belong to the caller.

    Unarmed environments return None and touch nothing. Returns a summary dict
    for the caller's log line otherwise.
    """
    armed = _armed_base()
    if armed is None:
        return None
    alloc_base, repos_root = armed
    identity = _identity_for_session(session_key)
    alloc_path = alloc_base / "allocations" / f"{_safe_segment(identity)}.json"
    try:
        alloc = _read_json(alloc_path)
    except WorkspaceGateError as exc:
        # An unreadable record must not free anything: whatever it bound stays
        # on disk (release never deletes work), and the summary says why.
        logger.warning("workspace release for {}: {}", identity, exc)
        return {"identity": identity, "error": str(exc), "kept": True}
    if not alloc:
        return None
    summary: dict = {"identity": identity, "mode": alloc.get("mode")}
    repo_id = alloc.get("repoId") or ""
    repo_dir = repos_root / repo_id
    try:
        if alloc.get("mode") == "primary" and repo_id:
            with open(repo_dir / "alloc.lock", "w", encoding="utf-8") as lock_fh:
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
                try:
                    owner = _read_json(repo_dir / "primary.json").get("instance")
                    if owner == identity:
                        (repo_dir / "primary.json").unlink(missing_ok=True)
                        summary["primaryReleased"] = True
                finally:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
            alloc_path.unlink(missing_ok=True)
            summary["allocationRemoved"] = True
        elif alloc.get("mode") == "worktree":
            wt = Path(alloc.get("boundWorkdir") or "")
            repo = alloc.get("repo") or {}
            root = Path(repo.get("root") or "")
            base_commit = repo.get("baseCommit") or ""
            if wt.is_dir() and root.is_dir() and base_commit and _worktree_is_clean(wt, base_commit):
                _git(root, "worktree", "remove", "--force", str(wt))
                branch = alloc.get("branch") or ""
                if branch:
                    _git(root, "branch", "-D", branch)
                _git(root, "worktree", "prune")
                alloc_path.unlink(missing_ok=True)
                summary["worktreeRemoved"] = True
                summary["allocationRemoved"] = True
            else:
                # Work present (or unverifiable): keep everything, mark the record.
                alloc["state"] = "released_kept_worktree"
                alloc["releasedAtMs"] = int(time.time() * 1000)
                _write_json(alloc_path, alloc)
                summary["worktreeKept"] = True
        else:
            alloc_path.unlink(missing_ok=True)
            summary["allocationRemoved"] = True
    except (OSError, WorkspaceGateError) as exc:  # release is best-effort; never fail the delete over it
        logger.warning("workspace release for {} failed: {}", identity, exc)
        summary["error"] = str(exc)
    return summary


# The machine-readable manifest block. Opening and closing sentinels so a
# consumer can cut the JSON out of a reply that also carries model prose; the
# authoritative block is the LAST one in the message (a model could type the
# opening line itself, but never after the runtime's own trailer).
MANIFEST_SENTINEL_OPEN = "--- raven-code integration manifest v1 ---"
MANIFEST_SENTINEL_CLOSE = "--- end raven-code integration manifest ---"

_TESTS_NOTE = "Test execution is not independently tracked by the runtime; see the agent report."


def _manifest_skeleton(session_key: str, kind: str) -> dict:
    """Every manifest starts here; one shape for both modes and for failures."""
    return {
        "manifestVersion": 1,
        "sessionId": session_key,
        "workspaceKind": kind,
        "repositoryRoot": "",
        "workdir": "",
        "branch": "",
        "baseCommit": "",
        "head": "",
        "commitsPastBase": {"count": 0, "items": []},
        "workingTree": {"clean": False, "uncommittedCount": 0, "entries": []},
        "diffStat": "",
        "tests": {"verifiedByRuntime": False, "commands": [], "results": [], "note": _TESTS_NOTE},
        "readyForIntegration": False,
        "status": "unknown",
        "blockers": [],
    }


def manifest_data_for_session(session_key: str) -> dict | None:
    """The manifest data object for one session, or None when nothing to report.

    None means the session never allocated a writable workspace (read-only,
    still pending, released, or the gate is unarmed) - a session that wrote
    code always has a working allocation, so every writing turn reports.

    Every field is machine-read: Git facts come live from the EFFECTIVE
    workdir (primary checkout or worktree - never a state directory),
    identity/paths from the allocation record, and nothing from model prose.
    A failure to read the facts degrades to ``status: unknown`` with an
    explicit blocker - never to a fabricated ready.
    """
    armed = _armed_base()
    if armed is None:
        return None
    alloc_base, _ = armed
    identity = _identity_for_session(session_key)
    try:
        alloc = _read_json(alloc_base / "allocations" / f"{_safe_segment(identity)}.json")
    except WorkspaceGateError as exc:
        data = _manifest_skeleton(session_key, "unknown")
        data["blockers"] = [f"allocation record unreadable: {exc}"]
        return data
    mode = alloc.get("mode")
    if alloc.get("state") != "working" or mode not in ("primary", "worktree"):
        return None

    repo = alloc.get("repo") or {}
    workdir = Path(alloc.get("boundWorkdir") or "")
    base = repo.get("baseCommit", "")
    data = _manifest_skeleton(session_key, str(mode))
    data["repositoryRoot"] = repo.get("root", "")
    data["workdir"] = str(workdir)
    data["baseCommit"] = base

    try:
        head = _git(workdir, "rev-parse", "HEAD")
        branch = _git(workdir, "rev-parse", "--abbrev-ref", "HEAD")
        status = _git(workdir, "status", "--porcelain")
        commits = _git(workdir, "log", "--format=%H%x1f%s", f"{base}..HEAD") if base else None
        stat = _git(workdir, "diff", "--stat", base) if base else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        data["branch"] = alloc.get("branch", "")
        data["blockers"] = [f"git state could not be read: {exc}"]
        return data

    # readyForIntegration may only rest on a COMPLETE picture: every git
    # query the manifest carries has to have answered. A branch or diff-stat
    # failure with a clean status still means "we do not know what this tree
    # is", and an integration agent acting on a half-known manifest is the
    # exact misread this field exists to prevent.
    failures = []
    if not base:
        failures.append("base commit")
    if head.returncode != 0:
        failures.append("head")
    if branch.returncode != 0 or not branch.stdout.strip():
        failures.append("branch")
    if status.returncode != 0:
        failures.append("status")
    if commits is None or commits.returncode != 0:
        failures.append("commits past base")
    if stat is None or stat.returncode != 0:
        failures.append("diff stat")
    facts_known = not failures
    if branch.returncode == 0 and branch.stdout.strip():
        data["branch"] = branch.stdout.strip()
    else:
        data["branch"] = alloc.get("branch", "")
    if head.returncode == 0:
        data["head"] = head.stdout.strip()

    entries = [line for line in status.stdout.splitlines() if line.strip()] if status.returncode == 0 else []
    items = []
    if commits is not None and commits.returncode == 0:
        for line in commits.stdout.splitlines():
            if not line.strip():
                continue
            sha, _, subject = line.partition("\x1f")
            items.append({"sha": sha.strip(), "subject": subject})
    data["commitsPastBase"] = {"count": len(items), "items": items}
    data["workingTree"] = {
        "clean": facts_known and not entries,
        "uncommittedCount": len(entries),
        "entries": entries,
    }
    if stat is not None and stat.returncode == 0 and stat.stdout.strip():
        data["diffStat"] = stat.stdout.strip().splitlines()[-1].strip()
    elif facts_known:
        data["diffStat"] = "no diff vs base"

    if not facts_known:
        data["status"] = "unknown"
        data["blockers"] = [f"git state could not be read: {', '.join(failures)}"]
    elif entries:
        data["status"] = "needs_commit"
        data["blockers"] = ["uncommitted changes must be committed"]
    elif not items:
        data["status"] = "no_changes"
        data["blockers"] = ["no commits past base"]
    else:
        data["status"] = "ready_for_integration"
        data["readyForIntegration"] = True
    return data


def render_manifest(data: dict) -> str:
    """The wire form: sentinel, one line of standard JSON, closing sentinel.

    ``json.dumps`` does the escaping - quotes, backslashes and control
    characters in branch names, paths and commit subjects stay valid JSON, and
    the payload stays on one physical line.
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return "\n".join(["", MANIFEST_SENTINEL_OPEN, payload, MANIFEST_SENTINEL_CLOSE])


def manifest_for_session(session_key: str) -> str | None:
    """The rendered manifest v1 block for one session, or None.

    Emitted by the ACP server at the end of every prompt of a session holding
    a writable allocation (primary or worktree), so the caller receives the
    integration facts machine-composed rather than trusted to the model's
    prose. Rendering and data share one code path (``manifest_data_for_session``)
    so a text consumer and a JSON consumer can never drift apart.
    """
    data = manifest_data_for_session(session_key)
    if data is None:
        return None
    return render_manifest(data)


def build_workspace_gate(workspace: Path) -> WorkspaceGate | None:
    """The gate, or None when the launcher did not arm it (evals, plain CLI)."""
    repos = os.environ.get(REPOS_ENV, "").strip()
    alloc_dir = os.environ.get(ALLOC_DIR_ENV, "").strip()
    instance = os.environ.get(INSTANCE_ENV, "").strip()
    alloc_base = os.environ.get(ALLOC_BASE_ENV, "").strip()
    if not repos:
        return None
    if alloc_dir and instance:
        return WorkspaceGate(Path(workspace), Path(repos), instance=instance, alloc_dir=Path(alloc_dir))
    if alloc_base:
        return WorkspaceGate(Path(workspace), Path(repos), alloc_base=Path(alloc_base))
    return None
