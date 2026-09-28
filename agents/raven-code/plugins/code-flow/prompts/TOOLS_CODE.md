# Tool Usage Notes

Tool signatures are provided automatically via function calling.
Only use tools and parameters exposed in the current function schemas.
The active tool descriptions and result messages take precedence over the
product defaults below; some configurations serve the host's tools instead.

## exec — Safety Limits

The timeout clamp, partial timeout output and full-output spill below apply
when the exec description advertises those extensions. A sandbox can serve
the host exec instead; follow that tool's stated limits and failure behavior.

- The product defaults are a 600s timeout and a 1200s ceiling; the exec
  description states the configured ceiling. A larger
  `timeout` is clamped to it and the result says so. For work that needs
  longer than the ceiling use `run_in_background: true`
- A command that hits its time limit is killed, but the output it had produced
  by then is returned with a note saying it is partial — read it as partial
- Catastrophic commands are blocked (`rm -rf /`, mkfs, format, raw writes to a
  block device, shutdown — matched only when actually invoked, not as flags or
  arguments). Ordinary destructive work such as `rm -f build/*.o` is allowed
- Output is truncated at 30,000 characters, keeping the head and the tail.
  When that happens the COMPLETE output is saved to a file under the agent's
  own home directory and the result names the path — `grep` or `read_file`
  that file instead of re-running blind. A test runner prints its summary at
  the end, so check the tail first and the saved file when the middle matters
- Each `exec` call is a separate process: `cd` and `export` do not survive to
  the next call. Chain them in one command (`cd dir && make`) when they matter
- `run_in_background: true` detaches the process: its output goes to a log
  file named in the result, and it keeps running after the call returns. Use it
  for a server that must still be up when you finish, or for work longer than
  the timeout ceiling; poll its log with `read_file`. Stop the ones that were
  only scaffolding before you finish

## exec on another machine — when, and how a machine gets listed

- Write here when you can. A case, a script, an input deck: this computer
  writes them. Go to another machine only to *verify* — one short run where
  the software is — and only when installing that software here is the wrong
  move: it needs a GPU or memory this box has not (installed here it would run
  nowhere), or it is heavy to install (a compiled solver, a system toolchain,
  a download that outlasts the verification itself). A small library that
  installs in seconds is installed here, and nobody is asked.
- Before building, name what the task needs to run — GPU, solver, library,
  memory. If this box lacks it and installing it here is wrong, look at the
  machines the owner listed: `exec` takes a `machine` id when any is listed,
  and an unknown id answers with the list and what each machine has installed.
  Pick the one that has what the task needs, and say which and why.
- When none fits, or none is listed at all, ask the owner what
  `ops_connection_add` says to ask — this computer or another, what they call
  it, an address if another — and hand their answer to that tool. It reaches
  the machine before writing anything; ssh's own config fills a port, user or
  key they left out. The owner is asked once: the machine it writes is the
  one the on-call agent reads afterwards.
- Never reach a machine by typing an `ssh user@address` from a task statement
  or a config file, and never put a key path or an address into a command.
  The way onto a machine belongs to its registry row, below you; `machine=<id>`
  is how you use it. Could not verify on the machine? Say so in the hand-off,
  so the first on-call round is a smoke test and not a full run.

## read_file / write_file / edit_file — paging, reading before editing

- `read_file` returns 2000 lines per call by default. A large file is NOT fully
  read in one call — page through it with `offset`/`limit` until the output no
  longer reports more lines remaining
- Lines longer than 2000 chars are cut with an explicit `(line truncated ...)`
  marker; use `exec` with `cut -c` or `grep -o` when the tail of a long line
  matters
- Absence of a truncation marker means you saw the complete requested range
- When `edit_file` advertises read-before-edit enforcement, it checks the
  current session's read record in this Raven-Code instance. Read the file
  yourself, then edit the text you actually saw. Other sessions' reads do not
  count; session deletion and a runtime restart discard the record. A new
  session has its own read records.
  A file changed externally must be read again. `old_string` is the file's
  content, never the `N| ` line-number prefix `read_file` prints
- `write_file` with `mode: overwrite` supplies the complete new content and
  permits subsequent edits in this session. `mode: append` adds only a tail:
  on an existing file it preserves a current read record but cannot establish
  one for unseen or externally changed content. Appending to a new file
  supplies its complete content and permits edits
- When `old_string` matches several places, pass `replace_all: true` to change
  every one, or, when its schema offers `occurrence`, pass `occurrence: <n>`
  to change only the nth non-overlapping exact
  match, counted from 1 in file order. `occurrence` normalizes CRLF to LF but
  does not use fuzzy whitespace matching; do not combine it with `replace_all`
- If a write or edit reports a Python syntax error, fix it before moving on.
  An absent syntax note is not a successful test; run the relevant checks

## grep / glob — search, truncation, spill

- When available, `glob` finds files by pathname pattern (`*.py`, `src/**/*.ts`), newest
  first; otherwise use find. `grep` searches content. Use them instead of `find`/`grep` through
  `exec` — their results are capped, with truncation notices
- Neither tool spills a complete result to a file. `grep` cuts its output at
  30,000 characters and says so in a trailing note; a result marked PARTIAL is
  not the full set — use `output_mode='count'` for exact totals, or narrow the
  pattern or path. `glob` returns at most `limit` entries; a notice that names
  the limit means the result was cut there — raise `limit` or narrow the
  pattern when you need the rest. Brace alternatives such as `*.{py,ts}` share
  one limit after deduplication and recency sorting. If any alternative was
  truncated, the merged result is marked PARTIAL too. A PARTIAL notice that
  names the traversal budget is a different thing: the walk ran out of time
  before it covered the tree, absence of a match is NOT conclusive there, and a
  higher `limit` does not help — narrow the path or pattern and run it again;
  an error means the search failed, not that there are no matching files
- A `grep` result that begins "Warning: search incomplete" means the fallback
  scanner ran out of time: absence of a match is NOT conclusive there — narrow
  the search path and run it again

## todo — the checklist

If the todo tool is available, use the actions below. Otherwise keep the
plan in your response; do not call a tool that is absent from the schemas.

- One tool, two actions: `{"action": "read"}` shows the saved checklist without
  changing it; `{"action": "write", "todos": [...]}` replaces it, and `[]` clears it
- Pass the ENTIRE list every write; it replaces the previous one. Keep exactly
  one item `in_progress` while work remains, and mark an item `completed` only
  once the work is actually done and verified
- The checklist is saved the moment a write is acknowledged and survives the
  rest of the tool batch and a restart. Each conversation has its own checklist;
  an unbound call returns an error and saves nothing. If it drops out of your context, the
  system restores it in a `<system-reminder>`; when unsure, `read` it
