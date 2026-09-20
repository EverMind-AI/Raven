"""Shell execution tool.

Authorization lives at the registry's door (``raven.permissions``): whether a
command may run, must ask, or is refused is decided before this tool is
dispatched. What stays here is the tool's own integrity boundary -- the
operator's allowlist and the workspace fence -- and the execution itself.
"""

import fnmatch
import os
import re
import shlex
from pathlib import Path
from typing import Any

from raven.agent import workdir
from raven.contracts.tool import STOP_RETRY_INSTRUCTION, Continuation, Tool, ToolOutput, ToolResult
from raven.permissions.shell_policy import (
    _MAX_EMBEDDED_SHELL_DEPTH,
    _command_segments,
    _embedded_shell_command,
    _unwrap_command_wrappers,
    executable_text,
)
from raven.sandbox import DirectExecutor, SandboxExecutor


class _UnmodelledExpansionError(Exception):
    """A parameter expansion the fence cannot resolve to the text the shell runs.

    The fence's promise is that it reads what will run. Where it cannot, the
    honest answer is a refusal rather than a scan of text the shell will
    replace: an expansion it does not model can hand the command any path at
    all, and every spelling that reached this point before was a silent bypass.
    Command substitution is not this case -- it holds an arbitrary program,
    which no textual guard can resolve, and it is a declared limit of the fence
    rather than a gap in it.
    """

    def __init__(self, construct: str) -> None:
        super().__init__(construct)
        self.construct = construct


class ExecTool(Tool):
    """Tool to execute shell commands."""

    # Backstop above the 600s internal exec cap (``_MAX_TIMEOUT``); the
    # executor's own timeout fires first, this only catches a wedged executor.
    timeout_seconds = 660.0

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        executor: SandboxExecutor | None = None,
        extra_allowed_dirs: tuple[Path, ...] = (),
        *,
        follow_binding: bool = True,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.extra_allowed_dirs = extra_allowed_dirs
        # A sub-agent run is a background asyncio task that can outlive the turn
        # that spawned it, since SubagentManager.spawn captures the workspace at
        # spawn time; its ExecTool must resolve cwd from the directory captured
        # for that run, never from the ambient binding, or it disagrees with its
        # own fs tools about which directory it is in (the same follow_binding
        # convention as the filesystem tools). The main loop's ExecTool keeps
        # following the live binding as normal.
        self.follow_binding = follow_binding
        self.path_append = path_append
        self._executor: SandboxExecutor = executor if executor is not None else DirectExecutor()

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        # Two descriptions, because the second sentence is a lie for an owner
        # who registered no machines. Read per call rather than fixed at
        # construction -- the registry can be written mid-session, and the loop
        # rebuilds the tool schema every turn.
        base = "Execute a shell command and return its output. Use with caution."
        from raven.agent.tools.machine_exec import machines_registered

        if not machines_registered():
            return base
        return (
            f"{base} "
            "Runs on THIS computer unless you name a machine: pass 'machine' with a "
            "connection id from the owner's registry to run it there instead. A machine "
            "of the owner's is where their code and their cases live, and work that "
            "outlives the call belongs to a job runner, so this refuses to detach there."
        )

    @property
    def truncation_hint(self) -> str:
        # "Send it in smaller pieces" is meaningless for a command: half a
        # command is not a command. What splits here is the work, not the
        # argument.
        return "Shorten the command, or split the work across several runs."

    @property
    def incomplete_hint(self) -> str:
        # Phrased as the consequent of a condition; see Tool.incomplete_hint.
        return "shorten the command, or split the work across several runs."

    @property
    def parameters(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds for a command on THIS computer. Increase for long-running "
                        "commands like compilation or installation (default 60, max 600). Not read with "
                        "'machine': a look on a registered machine is capped at 60s whatever this says, and "
                        "anything longer there is a job for the on-call agent's ops_submit."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Run detached on THIS computer instead of holding the turn: for long "
                        "mechanical local work (a download, an rsync, packaging). Returns a task id "
                        "and a log path at once; 'timeout' is not read on this lane, the task runs "
                        "until it finishes or the session ends. Read the log with a later exec. Not "
                        "combinable with 'machine': work on a registered machine is ops_submit's job."
                    ),
                },
            },
            "required": ["command"],
        }
        from raven.agent.tools.machine_exec import machines_registered

        if machines_registered():
            schema["properties"]["machine"] = {
                "type": "string",
                "description": "Where to run it: a connection id from the owner's machine "
                "registry, exactly as listed (an unknown id answers with the list). "
                "Leave out to run on THIS computer.",
            }
        return schema

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        machine: str = "",
        run_in_background: bool = False,
        **kwargs: Any,
    ) -> str | ToolResult:
        # Someone else's machine is a different place with different rules: the
        # guards below are about this computer (a deny-list for accidents, an
        # operator's workspace boundary), while there the rule is that work
        # outliving the call belongs on a ledger. Branching before them keeps
        # each set where it means something.
        #
        # Imported here and nowhere else in the execute path: a caller who
        # names no machine must not be able to be broken by the ops layer -- a
        # malformed connection registry has to leave the plain shell working.
        if machine and run_in_background:
            # Background work ON a registered machine is the definition of an
            # ops_submit job; allowing it here would re-legalise the accounting
            # bypass this boundary exists for (a main agent once drove 35 jobs
            # over raw ssh nohup, past the occupancy gate, dedup and the GPU
            # ledger). The two parameters never combine.
            return (
                "Error: 'machine' and 'run_in_background' do not combine. Background work on a "
                "registered machine is a job: submit it with ops_submit (budget, dedup, ledger). "
                "run_in_background is for long mechanical work on THIS computer. Nothing was run."
            )
        if machine:
            from raven.agent.tools.machine_exec import run_on_machine

            return await run_on_machine(command, connection=machine, cwd=working_dir)

        bound = str(workdir.current() or "") if self.follow_binding else ""
        cwd = working_dir or bound or self.working_dir or os.getcwd()

        if not self._executor.is_sandboxed:
            guard_error = self._guard_command(command, cwd)
            if guard_error:
                return self._boundary_error(guard_error)
        elif self.restrict_to_workspace:
            # Sandboxed: the microVM provides real isolation, but the workspace
            # fence is an operator-set boundary and still holds.
            workspace_error = self._check_workspace_restriction(command, cwd)
            if workspace_error:
                return self._boundary_error(workspace_error)

        if run_in_background:
            # After the guards on purpose: the background lane changes where the
            # output goes and who holds the turn, never what a command is
            # allowed to do. The permission gate has already ruled on this call
            # at the registry door, whichever lane it takes.
            if self._executor.is_sandboxed:
                # The detached child is a host process; starting one from a
                # sandboxed session would run the command OUTSIDE the sandbox.
                return (
                    "Error: run_in_background is not available in a sandboxed session -- the "
                    "detached process would run outside the sandbox. Run it synchronously, or "
                    "split the work. Nothing was run."
                )
            from raven.agent.tools import background_exec
            from raven.sandbox.direct_executor import _baseline_env

            # The same environment hygiene as the synchronous path: the child
            # gets the executor's baseline allowlist, never the full host
            # environment, so a detached download cannot read credentials the
            # capped path already withholds.
            bg_env = _baseline_env()
            if self.path_append:
                bg_env["PATH"] = bg_env.get("PATH", "") + os.pathsep + self.path_append
            try:
                task = background_exec.start(command, cwd=cwd, env=bg_env)
            except OSError as exc:
                return f"Error starting background command: {exc}"
            return background_exec.start_note(task)

        # Use `is None` check — `timeout or default` would treat timeout=0 as falsy.
        effective_timeout = min(self.timeout if timeout is None else timeout, self._MAX_TIMEOUT)

        env: dict[str, str] | None = None
        if self.path_append:
            if self._executor.is_sandboxed:
                # Inject path inside the VM via command wrapper; never pass os.environ
                # to a sandboxed executor — it would leak host credentials into the VM.
                command = f'export PATH="$PATH:{shlex.quote(self.path_append)}" && {command}'
            else:
                # Pass ONLY the PATH override. Copying os.environ here would hand
                # the full host environment to DirectExecutor and defeat its
                # baseline-allowlist hygiene; the executor supplies the rest.
                base_path = os.environ.get("PATH", "")
                env = {"PATH": base_path + os.pathsep + self.path_append}

        try:
            result = await self._executor.exec(command, cwd=cwd, timeout=effective_timeout, env=env)
        except Exception as e:
            return f"Error executing command: {str(e)}"
        text = result.as_text(self._MAX_OUTPUT)
        # The exit code is the verdict a config change, a security call or a
        # syntax error share, and the text a failing command produced is not
        # token-safe to classify from -- so the caller gets it structurally.
        return ToolOutput(text, ok=result.exit_code == 0)

    @classmethod
    def _boundary_error(cls, message: str) -> ToolResult:
        """A fence refusal: this call and its siblings do not run, the turn does."""
        return ToolResult(
            model_text=message + STOP_RETRY_INSTRUCTION,
            retryable=False,
            blocks_call=True,
            continuation=Continuation.CONTINUE,
            ok=False,
        )

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """The tool's own boundary: the operator's allowlist and the workspace fence.

        Reads the executable view (comments and quoted text stripped) so a
        command cannot be talked onto the allowlist by its own comment, and a
        path named in a comment is not scanned as a path the command touches.
        The deny list is not here: refusing dangerous commands is the
        permission gate's job, decided before dispatch.
        """
        cmd = executable_text(command).strip()
        lower = cmd.lower()

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        workspace_error = self._check_workspace_restriction(command, cwd)
        if workspace_error:
            return workspace_error

        return None

    # The null-device family is how a shell mutes or feeds a stream, not an
    # escape from the workspace: `2>/dev/null` names a path only to the
    # guard's regex, and blocking it turns every quiet read-only probe into a
    # terminal safety refusal. Matched on the written form, before resolve(),
    # which on macOS follows /dev/stdout into /dev/fd and out of this set.
    _DEVICE_FILES = frozenset(
        {
            "/dev/null",
            "/dev/stdin",
            "/dev/stdout",
            "/dev/stderr",
            "/dev/tty",
            "/dev/zero",
            "/dev/urandom",
            "/dev/random",
        }
    )

    def _check_workspace_restriction(self, command: str, cwd: str, *, _depth: int = 0) -> str | None:
        """Check only the workspace boundary constraints (no allow-list).

        Reads the executable view rather than trusting the caller to strip: the
        traversal and absolute-path scans have no reason to see text the shell
        discards, and there are two call sites -- the guard and the sandboxed
        path -- so doing it here is what keeps them from diverging. Reading the
        raw text refused ``ls -la  # see ../notes for why`` as path traversal.

        The contract, because the fence is a reader of shell and a reader can
        always meet syntax it does not know:

        **A parameter expansion is resolved faithfully or the command is
        refused.** The set of spellings is finite, so the residue is closed:
        there is no further form that quietly passes. Refusing is visible and
        arguable; the failure it replaces was silent, and silence is what let
        four spellings through at once.

        **Command substitution is a declared limit, not a gap.** It holds an
        arbitrary program, so no textual guard can resolve it, and refusing it
        would refuse ``echo "built at $(date)"`` along with everything else.
        Paths written literally inside one are still scanned; a path the
        substitution computes is beyond this fence, as a symlink inside the
        workspace is, and the sandbox executor is the boundary for both.
        """
        if not self.restrict_to_workspace:
            return None
        command = executable_text(command)

        cmd = command.strip()
        if "..\\" in cmd or "../" in cmd:
            return "Error: Command blocked by safety guard (path traversal detected)"

        cwd_path = Path(cwd).resolve()
        roots = [cwd_path, *(Path(d).resolve() for d in self.extra_allowed_dirs)]
        env = self._child_env(cwd_path)
        try:
            readable = self._as_the_shell_reads_it(cmd, env)
        except _UnmodelledExpansionError as unresolved:
            return f"Error: Command blocked by safety guard (unsupported shell expansion: {unresolved.construct})"

        try:
            segments = list(_command_segments(readable))
        except ValueError:
            # Quoting the lexer cannot close. The permission gate reads the
            # same text through the same lexer and refuses it there, so
            # answering here would only duplicate that refusal.
            segments = []

        # A `cd` inside a subshell does not outlive it, and the splitter has
        # already dropped the bracket that said so. Carrying the directory
        # forward past one would read a walk back as returning to the
        # workspace while the shell sits a level above it, so a bracket
        # anywhere puts every `cd` back on the starting directory. Crude on
        # purpose: a literal bracket in an argument only costs strictness.
        carry_forward = "(" not in readable and "{" not in readable
        if self._steps_outside(segments, cwd_path, roots, env, carry_forward=carry_forward):
            return "Error: Command blocked by safety guard (directory change outside working dir)"

        for raw in self._extract_absolute_paths(readable):
            try:
                if raw in self._DEVICE_FILES:
                    continue
                p = Path(raw.strip()).expanduser().resolve()
            except Exception:
                continue
            if not p.is_absolute():
                continue
            if any(root == p or root in p.parents for root in roots):
                continue
            return "Error: Command blocked by safety guard (path outside working dir)"

        # A nested shell's payload is a program, not an argument: the quoting
        # that stopped this pass expanding it is what hands it to that shell
        # intact, and the names in it expand there. Scanning it with the same
        # rules is the only reading that matches what runs. Depth-capped with
        # the policy's own bound, which is what decides the same question for
        # the deny list.
        if _depth < _MAX_EMBEDDED_SHELL_DEPTH:
            for segment in segments:
                # Unwrapped first, the way the policy reads a segment before
                # asking what it runs. Reusing the payload helper without the
                # unwrap that precedes it there reused half the agreement:
                # `env sh -c '...'` put the program back out of view.
                inner = _embedded_shell_command(_unwrap_command_wrappers(segment))
                if not inner:
                    continue
                nested = self._check_workspace_restriction(inner, cwd, _depth=_depth + 1)
                if nested:
                    return nested

        return None

    # `$NAME` and `${NAME}`, the two spellings that carry a path. `$(`, `$$`
    # and `$?` are deliberately not names, so they fall through untouched.
    _VARIABLE = re.compile(r"\$(?:\{([A-Za-z_]\w*)([^}]*)\}|([A-Za-z_]\w*))")

    # The brace bodies that offer a second word the shell may substitute
    # instead of the value (`:-`, `:=`, `:?`, `:+` and their colon-less forms).
    _BRACE_WORD_OPERATOR = re.compile(r"^:?[-=?+]")

    # Suffix and prefix removal. These shorten a path, which walks it UP and
    # out of the workspace, so reading them as decoration beside an in-root
    # value is what let `${PWD%/*}/outside.txt` open the parent directory.
    _BRACE_TRIM = re.compile(r"^(##?|%%?)(.*)$", re.DOTALL)

    # How cmd.exe spells a variable, and ``DirectExecutor`` runs the platform
    # shell, so on Windows this is the ordinary spelling of an outside path.
    # Substituted only for a name the child is actually given, which is cmd's
    # own rule for an undefined one.
    _WINDOWS_VARIABLE = re.compile(r"%([A-Za-z_]\w*)%")

    # Whether the shell that will run the command reads that spelling at all.
    # `%VAR%` is cmd.exe syntax and means nothing to `sh`, which prints it --
    # and HOME, PWD, USER and TMPDIR are on the executor's allowlist on POSIX
    # too, so reading it there refused `echo '%HOME%/notes'`, which names no
    # path. A class attribute rather than a call to `os.name`, so the Windows
    # branch can be driven from a POSIX host.
    _WINDOWS_SHELL = os.name == "nt"

    # A `${` the name pattern does not accept. `${#NAME}` counts characters, so
    # whatever it yields is a number and cannot name a path; anything else here
    # is indirection, an array or a form not modelled, and gets refused.
    _BRACE_OTHER = re.compile(r"\$\{(#[A-Za-z_]\w*|[^}]*)\}")

    @staticmethod
    def _child_env(cwd: Path) -> dict[str, str]:
        """The environment the command will be run with.

        ``PWD`` is overridden because a shell sets it from the directory it is
        started in: this command's ``$PWD`` is the workspace, whatever this
        process inherited.
        """
        from raven.sandbox.direct_executor import _baseline_env

        env = _baseline_env()
        env["PWD"] = str(cwd)
        return env

    @classmethod
    def _steps_outside(
        cls,
        segments: list[list[str]],
        cwd: Path,
        roots: list[Path],
        env: dict[str, str],
        *,
        carry_forward: bool,
    ) -> bool:
        """Whether the command leaves the workspace before doing its work.

        Reaching out and stepping out are the same escape, but only the first
        leaves a path for :meth:`_extract_absolute_paths` to find. A ``cd``
        whose destination is spelled out is already refused there, because
        ``/etc`` is a path like any other. What is left are the destinations
        the pattern cannot take: ``/`` has nothing after it, ``..`` is not
        absolute, and a ``cd`` with no argument names ``$HOME`` by saying
        nothing -- and after any of them, every relative path in the rest of
        the command resolves somewhere the operator did not allow.

        Each `cd` starts from where the last one landed, so `cd subdir; cd ..`
        ends where it began rather than being read as leaving. ``carry_forward``
        turns that off where the structure cannot be seen; see the caller.
        """
        here = cwd
        for segment in segments:
            # A subshell or brace group needs no unwrapping here: the splitter
            # treats `(`, `)` and a standalone `{` as operators, so `(cd /; x)`
            # arrives as its own segment with the bracket already gone. A
            # wrapper does, though -- `command cd /` names the same builtin.
            tokens = _unwrap_command_wrappers(segment)
            if not tokens or tokens[0] != "cd":
                continue
            arguments = list(tokens[1:])
            while arguments and arguments[0] in ("-L", "-P"):
                arguments.pop(0)
            if arguments and arguments[0] == "--":
                # The option terminator is not a destination. Past it every
                # word is an operand, so a `-` there names a directory rather
                # than $OLDPWD, and nothing there means what a bare `cd` means.
                arguments.pop(0)
            elif arguments and arguments[0] == "-":
                # ``$OLDPWD`` is a directory some earlier ``cd`` already passed.
                continue
            target = arguments[0] if arguments else env.get("HOME", "")
            if not target:
                continue
            try:
                destination = (here / Path(target).expanduser()).resolve()
            except Exception:
                return True
            if not any(root == destination or root in destination.parents for root in roots):
                return True
            if carry_forward:
                here = destination
        return False

    @staticmethod
    def _windows_value(env: dict[str, str], name: str) -> str | None:
        """``env``'s value for ``name`` the way cmd.exe finds it, or ``None``.

        cmd.exe resolves an environment name without regard to case, so an
        exact-key lookup leaves `%UserProfile%` standing and the path scan then
        sees no drive prefix. ``None`` means the child has no such name, which
        cmd answers by leaving the text as written.
        """
        if name in env:
            return env[name]
        folded = name.casefold()
        return next((value for key, value in env.items() if key.casefold() == folded), None)

    @classmethod
    def _resolve_brace(cls, value: str, body: str, construct: str, env: dict[str, str]) -> str:
        """The value a ``${...}`` yields, or a refusal when that cannot be known.

        Two bodies are resolved. A word operator offers a second word the shell
        may substitute instead of the value, and which one it picks depends on a
        value this cannot read -- so both are emitted, the operator becoming a
        space so the word it introduces starts on a boundary the scan can take.
        A trim shortens the value, which is the case that matters: it walks a
        path UP, so reading it as decoration beside an in-root value is what let
        ``${PWD%/*}/outside.txt`` open the parent.

        Every other body is refused. Substitution, case folding and indirection
        can each hand the command a path this cannot compute, and allowing them
        would make the fence's promise depend on which spellings happen to be
        implemented -- which is the defect, not a smaller version of it.
        """
        if not body:
            return value
        # The body is inserted after this walk has passed the position it lands
        # in, so nothing would read it again. The shell does read it, which is
        # why it goes back through the same pass here rather than out as text:
        # `${UNSET:-$HOME/secret}` is that path, and leaving it unread was the
        # one spelling the resolved-or-refused rule claimed and did not cover.
        if cls._BRACE_WORD_OPERATOR.match(body):
            word = cls._as_the_shell_reads_it(cls._BRACE_WORD_OPERATOR.sub("", body), env)
            return f"{value} {word}"
        trim = cls._BRACE_TRIM.match(body)
        if trim is not None:
            pattern = cls._as_the_shell_reads_it(trim.group(2), env)
            return cls._trim(value, trim.group(1), pattern)
        raise _UnmodelledExpansionError(construct)

    @staticmethod
    def _trim(value: str, operator: str, pattern: str) -> str:
        """``value`` with the matching prefix or suffix removed, as the shell does.

        A doubled operator takes the longest match and a single one the
        shortest, which is why each direction is walked from its own end. No
        match leaves the value alone, exactly as the shell leaves it.
        """
        from_end = operator.startswith("%")
        longest = len(operator) == 2
        if from_end:
            cuts = range(0, len(value) + 1) if longest else range(len(value), -1, -1)
            for cut in cuts:
                if fnmatch.fnmatchcase(value[cut:], pattern):
                    return value[:cut]
            return value
        cuts = range(len(value), -1, -1) if longest else range(0, len(value) + 1)
        for cut in cuts:
            if fnmatch.fnmatchcase(value[:cut], pattern):
                return value[cut:]
        return value

    @classmethod
    def _as_the_shell_reads_it(cls, command: str, env: dict[str, str]) -> str:
        """``command`` with its parameters expanded, as the child will expand them.

        The scan below reads text, so it can only refuse a path it can see, and
        a name is where a path hides: ``$HOME/.ssh/id_rsa`` put a letter in
        front of the ``/`` that :meth:`_extract_absolute_paths` looks for, and
        the whole command went through as naming no path at all. Expanding
        first is what puts the path back in view -- expanding afterwards, which
        is what this used to do, only ever saw candidates the scan had already
        passed.

        Three rules, each taken from the shell rather than invented:

        - **An unknown name is nothing.** ``$NOPE/etc/shadow`` *is*
          ``/etc/shadow``, and it is the case with no literal spelling to fall
          back on.
        - **The child's environment decides.** ``DirectExecutor`` hands over an
          allowlisted baseline, so a name outside that list arrives unset
          however this process reads it; ``os.environ`` would answer for a
          variable the command will never have.
        - **Single quotes expand nothing.** ``echo 'keys go in $HOME/.ssh'`` is
          a sentence, and refusing it would teach the operator to turn the
          fence off.

        A sandboxed executor runs somewhere with its own environment, which
        nothing here can read. The values are a host-side approximation there;
        the rule that carries is the first one, which needs no value to hold.
        """
        out: list[str] = []
        quote = ""
        index = 0
        while index < len(command):
            char = command[index]
            if char == "%" and cls._WINDOWS_SHELL:
                # Ahead of the quote branches on purpose: cmd.exe has no
                # quoting that suppresses this, so a run it would expand must
                # not be hidden here by POSIX quoting rules.
                windows_match = cls._WINDOWS_VARIABLE.match(command, index)
                if windows_match is not None:
                    value = cls._windows_value(env, windows_match.group(1))
                    if value is not None:
                        out.append(value)
                        index = windows_match.end()
                        continue
            if quote == "'":
                out.append(char)
                if char == "'":
                    quote = ""
                index += 1
                continue
            if char == "\\" and index + 1 < len(command):
                escaped = command[index + 1]
                # An escaped `$` is a literal `$` that this level does not
                # expand -- but the character still travels on, and a nested
                # shell handed it does expand it. Keeping the backslash here
                # hid `sh -c "sh -c 'cat \\$HOME/x'"` from the nested scan,
                # because the payload it was given no longer looked like a
                # name. Every other escape keeps both characters: only these
                # two decide whether something expands.
                out.append(escaped if escaped in "$`" else char + escaped)
                index += 2
                continue
            if quote == '"' and char == '"':
                quote = ""
                out.append(char)
                index += 1
                continue
            if not quote and char in "'\"":
                quote = char
                out.append(char)
                index += 1
                continue
            if char == "$":
                name_match = cls._VARIABLE.match(command, index)
                if name_match is not None:
                    value = env.get(name_match.group(1) or name_match.group(3), "")
                    out.append(cls._resolve_brace(value, name_match.group(2) or "", name_match.group(0), env))
                    index = name_match.end()
                    continue
                if command.startswith("${", index):
                    other = cls._BRACE_OTHER.match(command, index)
                    if other is None:
                        # Nesting runs past the first `}`, which this does not
                        # parse. The contract answers that with a refusal.
                        raise _UnmodelledExpansionError(command[index : index + 32])
                    if not other.group(1).startswith("#"):
                        raise _UnmodelledExpansionError(other.group(0))
                    # A character count. Whatever it yields is a number, and a
                    # number cannot name a path, so it needs no value here.
                    out.append("0")
                    index = other.end()
                    continue
            out.append(char)
            index += 1
        return "".join(out)

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        # The boundary class must cover every character a path can be glued
        # to, not just whitespace: --file=/etc/passwd, </etc/passwd,
        # cmd;/bin/x, $(/usr/bin/id) and `/bin/x` all name a path with no
        # space before it, and a boundary the class misses is a fence bypass.
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)
        posix_paths = re.findall(r"(?:^|[\s|>'\"=<;(`])(/[^\s\"'>;|<]+)", command)
        home_paths = re.findall(r"(?:^|[\s|>'\"=<;(`])(~[^\s\"'>;|<]*)", command)
        return win_paths + posix_paths + home_paths
