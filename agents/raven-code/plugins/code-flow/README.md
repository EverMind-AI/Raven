# Raven-Code Python diagnostics

When the replacement tool face is enabled, Python writes and edits return
syntax and name-binding diagnostics alongside the normal result. The name
checks cover undefined names (`F821`), undefined exports (`F822`), and local
variables used before assignment (`F823`). A direct edit compares with the
previous content to suppress existing name findings. A syntax error still
present after an edit remains visible, including after a rewrite that left
the file unchanged. A baseline that cannot be parsed, decoded or analyzed is
unknown, so findings are not claimed to be newly introduced. Diagnostics never undo an
edit, automatically fix code, or change the command's exit status.
These checks run only through the enabled code-flow replacement tools. Loading
the plugin or installing Pyflakes does not modify the host's built-in tools;
other agents keep their existing behavior unless explicitly configured to use
this tool face.

The switches live under `plugins.config["code-flow"].tools`:

| Switch | Default | Effect |
| --- | --- | --- |
| `pythonSyntaxNote` | `true` | Master switch for Python diagnostics. |
| `pythonLint` | `true` | Add name-binding checks to syntax diagnostics. |
| `pythonUnusedImports` | `false` | Add advisory `F401` notices. Imports can export names or have side effects; the notice does not recommend unconditional removal. |
| `execPythonDiagnostics` | `true` | Check changed Python files after completed local foreground commands when a checkpoint supplies visibility rules. |

The checks parse source without importing or executing it. They honor inline
`# noqa` / `# noqa: F821` and file-level `# ruff: noqa` / `# flake8: noqa`
comments for name/import checks, read as Ruff reads them: a file-level
directive counts only on a line of its own, and one trailing code suppresses
nothing; a marker counts only when a colon and codes follow it or, with no
colon, whitespace, another comment or the end of the comment does, so a URL
fragment such as `#noqa-section` or a colon with no codes is text. Unlike
Ruff, which takes the first directive in a comment, every directive in it
counts. They use
the host's Python parser and builtins, not the project's complete Ruff/Flake8
configuration or target interpreter;
dynamic bindings and version differences can need human or agent judgment.
Syntax feedback names the parser version. Passing diagnostics do not prove
that the patch is correct or replace project tests.

Name checks skip source over 256 KiB, the syntax check source over 2 MiB, and
a check skipped for size says so in one line. Source is decoded as Python
decodes it, by its coding declaration or as UTF-8; bytes that do not decode
are reported as the syntax error Python raises for them, at the line where
decoding stops. A changed file that cannot be read reports diagnostics
unavailable. Each file reports at most eight findings.
Post-command checks consider at most eight visible changed Python files inside the
command's working directory, with a 6,000-character diagnostic budget plus
omission notices. The changed files come from the host's own write record for
the command, which lists regular files and skips symlinks; a command whose
directory the host could not list has no record and gets no post-command
checks. Post-command checks require a checkpoint covering the working
directory. With no checkpoint or no visibility verdict, they do not read
changed source files. Direct `write_file` / `edit_file` diagnostics remain
available. Noninteractive runs using the default
`runtime.checkpoint.policy = "interactive"` need an explicit `"always"` policy
to enable checkpoints and post-command checks. Visibility rules apply before the
eight-file limit and withhold ignored files. Visibility queries use batches of
up to 256 files and stop once eight eligible files are found. The host's
visibility callback resolves each queried path synchronously; yielding between
batches bounds that work, but one blocked host callback cannot be preempted by
an asyncio timeout.

Up to four file checks run concurrently, with feedback kept in input order.
Each check analyzes one file; it does not resolve imports across the project.
Python's GIL means concurrency does not promise a CPU speedup. Each file check
has a one-second wait limit, and post-command diagnostics have a three-second
total wait limit, separate from the command timeout.

All diagnostic reads and checks share a private four-worker limit per
process, independent of the host's default executor. There is no background
work queue: when all workers are occupied, new diagnostic work is skipped or
reported unavailable. A timeout or cancellation releases the caller but cannot
interrupt an operating system read or Python check already running. Such work
retains its worker slot until it finishes, preventing an accumulation of
abandoned workers; daemon workers do not delay process shutdown. A file check
that fails, including on source nested too deeply for the parser or checker,
reports diagnostics unavailable; post-command diagnostics that fail or overrun
their wait as a whole are dropped. Either way the operation's result is kept.

For existing files changed by a command, the original source is not
available: feedback explicitly says findings may predate the command.
Background, remote, sandboxed commands, symlinks, and writes outside the watched
directory are not covered. Concurrent writers can also appear in the host's write
record; the check does not establish which process made a change.
