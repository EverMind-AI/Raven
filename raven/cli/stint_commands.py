"""``raven playbook stint`` subapp: lay a project out for a stint, and work its backlog.

``init`` writes ``.stint/`` -- a guard file per role, the shared prose, and a
link to the project's own specification -- which is what a ``mode: stint``
playbook reads before its first round. The rest is the backlog the roles share:
``task`` moves one card through the state machine, ``ask`` files a question for
a person, and ``confirm`` is the person's word that the stint is the stint.

Nothing here starts a run. A run is a playbook (`raven playbook run <name>`),
and these verbs are what a person uses around it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

console = Console()

stint_app = typer.Typer(
    help="Lay a project out for a stint, and work the backlog its roles share.",
    no_args_is_help=True,
)


task_app = typer.Typer(
    help="The backlog: the one structured thing the three roles share.",
    no_args_is_help=True,
)
stint_app.add_typer(task_app, name="task")

ROLE_ENV = "STINT_ROLE"
ROUND_ENV = "STINT_ROUND"
STATE_ENV = "STINT_STATE_DIR"


def _task_context(project: Path | None, role: str | None) -> tuple[Path, str, int]:
    """Where the backlog is, who is asking, and which round it is.

    Role and round come from the environment the runtime sets for a role turn,
    so a role never has to name itself and cannot get it wrong by accident. A
    person on a terminal is ``human`` unless they say otherwise.
    """
    import os

    from raven.stint.backlog import HUMAN

    workspace = (project or Path.cwd()).expanduser().resolve()
    who = (role or os.environ.get(ROLE_ENV) or HUMAN).strip().lower()
    try:
        index = int(os.environ.get(ROUND_ENV) or 0)
    except ValueError:
        index = 0
    return workspace, who, index


def _task_log(verb: str, role: str, index: int, task_id: int | None, outcome: str) -> None:
    """Every attempt at a transition, accepted or refused, in one file.

    The refusals are the point. A role that kept calling a transition it does
    not own leaves no trace in the backlog -- the backlog only records what
    happened -- and its turn output may be thousands of lines away. This is the
    one place to look when a round's bookkeeping went wrong.
    """
    import os

    state_dir = os.environ.get(STATE_ENV, "").strip()
    if not state_dir:
        return
    line = json.dumps(
        {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "round": index,
            "role": role,
            "verb": verb,
            "task": task_id,
            "outcome": outcome,
        },
        ensure_ascii=False,
    )
    try:
        path = Path(state_dir).expanduser() / "tasks.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return


def _task_change(verb: str, project: Path | None, role: str | None, task_id: int | None = None, **fields):
    from raven.stint import backlog as backlog_mod

    workspace, who, index = _task_context(project, role)
    try:
        # `add` is the verb that files the first task into a project that has
        # none, which is what the missing-backlog message promises; every other
        # verb needs a backlog that is already there.
        backlog = backlog_mod.start(workspace) if verb == "add" else backlog_mod.load(workspace)
        task = backlog_mod.apply(backlog, verb, role=who, task_id=task_id, round_index=index, **fields)
        backlog_mod.save(workspace, backlog)
    except backlog_mod.BacklogError as error:
        _task_log(verb, who, index, task_id, f"refused: {error}")
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    _task_log(verb, who, index, task.id, task.state)
    console.print(f"[green]task {task.id}[/green] {task.state} -- {task.title}")


_PROJECT = typer.Option(None, "--project", "-p", help="The project (default: the working directory)")
_ROLE = typer.Option(None, "--role", help=f"Override ${ROLE_ENV}; a person on a terminal is `human`")


@task_app.command("list")
def task_list(
    project: Path | None = _PROJECT,
    state: str | None = typer.Option(None, "--state", help="Only tasks in this state"),
    ready: bool = typer.Option(False, "--ready", help="Only what can be started: open, unblocked, deps settled"),
    deferred: int | None = typer.Option(None, "--deferred", help="Only tasks deferred at least this many times"),
):
    """What is in the pool."""
    from raven.stint import backlog as backlog_mod

    workspace, _, _ = _task_context(project, None)
    try:
        # A project with no backlog yet has an empty pool, which is an answer and
        # not a failure -- and it is the first thing a Planner asks on a project's
        # first round. Answering it with an error sent one off reading this
        # program's own source to find out what had gone wrong; nothing had.
        backlog = backlog_mod.start(workspace)
    except backlog_mod.BacklogError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    rows = backlog.ready() if ready else list(backlog.tasks)
    if state:
        rows = [task for task in rows if task.state == state]
    if deferred is not None:
        rows = [task for task in rows if task.deferred >= deferred]
    console.print(backlog_mod.render(rows))
    counts = backlog.counts()
    console.print("[dim]" + "  ".join(f"{key} {value}" for key, value in counts.items() if value) + "[/dim]")


@task_app.command("add")
def task_add(
    title: str | None = typer.Argument(None, help="One sentence: what done looks like"),
    title_option: str = typer.Option("", "--title", help="The same, as an option"),
    name: str = typer.Option(
        "", "--name", help="Two to five words a card can carry; defaults to the title's head before its colon"
    ),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
    source: str = typer.Option("", "--source", help="Where it came from: spec:7.4, qa_02, round_03, human"),
    gates: list[str] = typer.Option([], "--gates", help="Acceptance gate this task should move; repeatable"),
    depends_on: list[int] = typer.Option([], "--depends-on", help="Task that must settle first; repeatable"),
    severity: str = typer.Option("", "--severity", help="severe / major / minor, for a QA finding"),
    note: str = typer.Option("", "--note", help="Why the stint did not already have it"),
):
    """Register a task."""
    _task_change(
        "add",
        project,
        role,
        title=(title_option or title or "").strip(),
        name=name,
        source=source,
        gates=gates,
        depends_on=depends_on,
        severity=severity,
        note=note,
    )


@task_app.command("assign")
def task_assign(
    task_id: int,
    to: str = typer.Option(
        "",
        "--to",
        help="Which Developer instance does it (developer-a, developer-b, developer-c). "
        "Leave it out when the round runs one Developer; with several, a task assigned to nobody "
        "is shown to all of them and two of them may then edit one file",
    ),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
):
    """Put a task in this round, and say whose it is. Planner only."""
    _task_change("assign", project, role, task_id, owner=to.strip())


@task_app.command("defer")
def task_defer(
    task_id: int,
    reason: str = typer.Option(..., "--reason", help="Why not this round"),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
):
    """Not this round. Deferring twice means the third round must take it."""
    _task_change("defer", project, role, task_id, reason=reason)


@task_app.command("reject")
def task_reject(
    task_id: int,
    reason: str = typer.Option(..., "--reason", help="Why it will not be done; QA reads this before re-raising"),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
):
    """Decline a finding, on the record."""
    _task_change("reject", project, role, task_id, reason=reason)


@task_app.command("block")
def task_block(
    task_id: int,
    by: str = typer.Option(..., "--by", help="human:<qid>, task:<id>, or external:<what>"),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
):
    """Something is holding this up. A question for a person is `raven playbook stint ask`."""
    from raven.stint import decisions as decisions_mod

    workspace, _, _ = _task_context(project, role)
    known = [question.id for question in decisions_mod.read(workspace)]
    _task_change("block", project, role, task_id, blocker=by, known_questions=known)


@task_app.command("unblock")
def task_unblock(
    task_id: int,
    by: str = typer.Option("", "--by", help="Lift one blocker; omit to lift them all"),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
):
    """The thing that was holding it up is gone."""
    _task_change("unblock", project, role, task_id, blocker=by)


check_app = typer.Typer(
    help="What this project runs for the checks a playbook declares by description.",
    no_args_is_help=True,
)
stint_app.add_typer(check_app, name="check")


@check_app.command("set")
def check_set(
    playbook: str = typer.Argument(..., help="The playbook that declares the check"),
    name: str = typer.Argument(..., help="The check's name, as the playbook declares it"),
    run: str = typer.Option(..., "--run", help="What it runs here, as a shell command"),
    project: Path | None = _PROJECT,
):
    """Answer, for this project, what one declared check runs.

    A playbook that travels says what has to be true (`the source compiles`) and
    not how to find out, because the how is the project's and shell in a file is
    shell on whoever opens it. This is where the project answers, once: written
    to `.stint/checks.json` and read by every round after.
    """
    from raven.stint.verify import remember_check

    workspace, _, _ = _task_context(project, None)
    path = remember_check(workspace, playbook, name, run, found="person")
    console.print(f"[green]{escape(playbook)}-{escape(name)}[/green] runs {escape(run)}")
    console.print(f"[dim]{escape(str(path))}[/dim]")


@check_app.command("list")
def check_list(project: Path | None = _PROJECT):
    """Every check this project has an answer for."""
    import json as _json

    from raven.stint.verify import checks_path

    workspace, _, _ = _task_context(project, None)
    path = checks_path(workspace)
    try:
        rows = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        console.print("[dim]This project has answered no checks.[/dim]")
        return
    for key in sorted(rows):
        row = rows[key] or {}
        console.print(f"[green]{escape(key)}[/green] {escape(str(row.get('run', '')))} [dim]({row.get('from')})[/dim]")


@stint_app.command("ask")
def ask(
    text: str = typer.Argument(..., help="The question, as one paragraph a person can answer"),
    blocks: list[int] = typer.Option([], "--blocks", help="Task that waits on the answer; repeatable"),
    decide: str = typer.Option(
        "", "--decide", help="Your own ruling, standing until a person's replaces it; the run carries on with it"
    ),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
):
    """Put a question to a person: rule on it provisionally, or block the tasks that wait on the answer.

    One move rather than two, because the question's id is minted here: a role
    that blocks first has to invent an id, and a task blocked on an id nobody
    asked is never unblocked.
    """
    from raven.stint import decisions as decisions_mod

    workspace, who, index = _task_context(project, role)
    if not text.strip():
        console.print("[red]a question needs words[/red]")
        raise typer.Exit(code=1)
    question = decisions_mod.ask(workspace, text, round_index=index, blocks=list(blocks), provisional=decide)
    where = f"{decisions_mod.STINT_DIR}/{decisions_mod.DECISIONS_FILE}"
    standing = " -- your ruling stands until a person's replaces it" if question.provisional else ""
    console.print(f"[green]{question.id}[/green] recorded in {where}{standing}")
    for task_id in dict.fromkeys(blocks):
        _task_change("block", project, who, task_id, blocker=f"human:{question.id}", known_questions=[question.id])


@stint_app.command("confirm")
def confirm_command(
    project: Path = typer.Option(..., "--project", "-p", help="The project whose backlog you have read"),
):
    """Confirm the backlog: your word that the stint is the stint.

    A separate move from writing the stint, and a person's alone. Nothing in the
    stint path reads the flag yet -- ``roster.ready`` is where the check lives
    and no stint asks it -- so today this records the word rather than gating on
    it.
    """
    from raven.stint import backlog as backlog_mod

    try:
        backlog = backlog_mod.confirm(Path(project).expanduser().resolve())
    except backlog_mod.BacklogError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]confirmed[/green] {len(backlog.tasks)} task(s); the first round may start")


@task_app.command("implement")
def task_implement(
    task_id: int,
    commit: str = typer.Option(..., "--commit", help="The commit carrying the work"),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
):
    """Submit an attempt for review. Developer only -- it does not mark the task done."""
    _task_change("implement", project, role, task_id, commit=commit)


@task_app.command("verdict")
def task_verdict(
    task_id: int,
    proven: bool = typer.Option(False, "--proven", help="The evidence supports the claim"),
    not_proven: bool = typer.Option(False, "--not-proven", help="It does not"),
    evidence: str = typer.Option("", "--evidence", help="The path a reader can open"),
    reason: str = typer.Option("", "--reason", help="Why the evidence falls short"),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
):
    """Judge an attempt. QA only -- it is the one holding evidence."""
    if proven == not_proven:
        raise typer.BadParameter("give exactly one of --proven or --not-proven")
    _task_change("proven" if proven else "not_proven", project, role, task_id, evidence=evidence, reason=reason)


@task_app.command("reopen")
def task_reopen(
    task_id: int,
    reason: str = typer.Option(..., "--reason", help="What regressed, and where it was seen"),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
):
    """A finished task is not finished any more. QA only."""
    _task_change("reopen", project, role, task_id, reason=reason)


@task_app.command("drop")
def task_drop(
    task_id: int,
    reason: str = typer.Option(..., "--reason", help="Why it is cut"),
    project: Path | None = _PROJECT,
    role: str | None = _ROLE,
):
    """Cut a task from the stint. A person's call."""
    _task_change("drop", project, role, task_id, reason=reason)


@stint_app.command("init")
def init_command(
    project: Path = typer.Option(..., "--project", "-p", help="The project to set up"),
    spec: Path | None = typer.Option(None, "--spec", help="The specification to link (default: the likeliest one)"),
    enforce_read: str = typer.Option(
        "soft", "--read", help="soft: the prompt says which files are the role's. hard: its tools cannot reach others"
    ),
    enforce_write: str = typer.Option(
        "hard", "--write", help="hard: a write outside the role's paths is undone. soft: it is only recorded"
    ),
    force: bool = typer.Option(False, "--force", help="Rewrite a file that is already there"),
):
    """Lay out `.stint/` in a project: the guard files, the shared prose, the spec link."""
    from raven.stint import roster as roster_mod
    from raven.stint.bootstrap import InitError, init

    for label, value in (("--read", enforce_read), ("--write", enforce_write)):
        if value not in (roster_mod.SOFT, roster_mod.HARD):
            raise typer.BadParameter(f"{label} is {roster_mod.SOFT} or {roster_mod.HARD}, not {value!r}")
    try:
        result = init(project, spec=spec, enforce_read=enforce_read, enforce_write=enforce_write, force=force)
    except InitError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from None

    console.print(f"[green]+[/green] {Path(project).expanduser().resolve()}")
    for name in result.created:
        console.print(f"  wrote {name}")
    for name in result.kept:
        console.print(f"  [dim]kept {name} (use --force to replace)[/dim]")
    for note in result.notes:
        console.print(f"  [yellow]![/yellow] {note}")
    console.print(
        "\nNext: read the guard files and the facts section of "
        f"[bold]{roster_mod.roster_dir(project) / 'HUMAN_DECISIONS.md'}[/bold], "
        "then write the backlog and confirm it."
    )
