# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — Safety Limits

- Commands have a configurable timeout (default 60s, max 600s by default).
  Requests above the ceiling are clamped, not rejected — for genuinely longer
  work use `background: true`
- Catastrophic commands are blocked (`rm -rf /`, mkfs, format, raw writes to a
  block device, shutdown — matched only when actually invoked, not as flags or
  arguments). Ordinary destructive work such as `rm -f build/*.o` is allowed
- Output is truncated at 30,000 characters, keeping the head and the tail. The
  full output is saved to a file named in the truncation marker — grep it or
  page through it with `read_file`
- `restrictToWorkspace` config can limit file access to the workspace

## exec sessions — persistent state and stdin

Each plain `exec` call is a separate process: `cd`, `export` and background jobs
do not survive to the next call. Pass `session: "<name>"` to run inside a
persistent shell instead, then:

- `exec_write` sends input to it — passwords and other prompts, REPL/debugger
  input, or the ETX control character (0x03) to send Ctrl-C
- `exec_read` returns whatever it has printed since your last read

Sessions are capped (8 at a time) and all close when the agent exits; pass
`close: true` to `exec_read` to release one early. Anything that has to still be
running afterwards belongs in a background job, not a session.

## background jobs — work that outlives the run

Use `background: true` for a server or a long build. The process is detached: it
has no terminal, its output goes to a log file, and it keeps running after the
call returns, after you stop working on the task, and after the run ends. This is
also the way around the per-command timeout ceiling.

`background: true` always creates a detached job — combining it with `session`
just names the job (the process does NOT live in that shell, and has no stdin;
if you need to type into a long-running program, use a session without
`background` and drive it with `exec_write`).

- `job_status` lists jobs, or with a name returns the log written since your last
  check
- `job_wait` blocks until a job finishes — for a build you now need the result of,
  never for a server you meant to leave running
- `job_cancel` stops one (SIGTERM, then SIGKILL)

Because jobs survive, cancel the ones that were only scaffolding. Leave any
service the task asked you to have running — something will check it after you are
done, and stopping it on the way out fails the task.

## read_file / write_file — truncation and paging

- `read_file` returns 2000 lines per call by default. A large file is NOT fully
  read in one call — page through it with `offset`/`limit` until the output no
  longer reports more lines remaining
- Lines longer than 2000 chars are cut with an explicit `(line truncated ...)`
  marker; use `exec` with `cut -c` or `grep -o` when the tail of a long line
  matters
- Absence of a truncation marker means you saw the complete requested range

## cron — Scheduled Reminders

- Please refer to cron skill for usage.
