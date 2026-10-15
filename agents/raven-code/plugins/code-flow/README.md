# Raven-Code Python diagnostics

When the replacement tool face is enabled, Python writes and edits return
syntax and name-binding diagnostics alongside the normal result. The name
checks cover undefined names (`F821`), undefined exports (`F822`), and local
variables used before assignment (`F823`). A direct edit compares with the
previous content to suppress existing findings. Diagnostics never undo an edit,
automatically fix code, or change the command's exit status.

The switches live under `plugins.config["code-flow"].tools`:

| Switch | Default | Effect |
| --- | --- | --- |
| `pythonSyntaxNote` | `true` | Master switch for Python diagnostics. |
| `pythonLint` | `true` | Add name-binding checks to syntax diagnostics. |
| `pythonUnusedImports` | `false` | Add advisory `F401` notices. Imports can export names or have side effects; the notice does not recommend unconditional removal. |
| `execPythonDiagnostics` | `true` | Check changed Python files after completed local foreground commands. |

The checks parse source without importing or executing it. They honor inline
`# noqa` / `# noqa: F821` and file-level `# ruff: noqa` / `# flake8: noqa`
comments for name/import checks. They use the host's Python parser and builtins,
not the project's complete Ruff/Flake8 configuration or target interpreter;
dynamic bindings and version differences can need human or agent judgment.
Syntax feedback names the parser version. Passing diagnostics do not prove
that the patch is correct or replace project tests.

Checks skip source over 256 KiB and report at most eight findings per file.
Post-command checks consider at most eight changed Python files inside the
command's working directory, with a 6,000-character diagnostic budget plus
omission notices. Existing host write records are reused; otherwise a bounded
metadata scan detects changes. Scans stop at 20,000 files or a 250 ms deadline;
an incomplete scan yields no comparison. Each file check has a one-second wait
limit, and post-command diagnostics have a three-second total wait limit.
Timeouts release the caller but cannot interrupt an operating system file read
already running in a worker thread. A failed check preserves the operation's
result. Where the host supplies checkpoint visibility rules, diagnostics also
honor them and do not read withheld files.

For existing files changed by a command, the original source is not always
available: feedback explicitly says findings may predate the command.
Background, remote, sandboxed commands, symlinks, and writes outside the watched
directory are not covered. Concurrent writers can also appear in a metadata
comparison; the check does not establish which process made a change.
