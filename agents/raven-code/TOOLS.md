# Tool Usage Notes

Tool signatures are provided automatically via function calling.
Only use tools and parameters exposed in the current function schemas.
The active tool descriptions and result messages take precedence over the
notes below.

## exec — Safety Limits

- A command times out after 600s by default; `timeout` raises it up to the
  1200s ceiling. A larger `timeout` is clamped to the ceiling and the result
  says so. For work that needs longer than the ceiling use
  `run_in_background: true`
- A command that hits its time limit is killed, but the output it had produced
  by then is returned with a note saying it is partial — read it as partial
- Catastrophic commands are blocked (`rm -rf /`, mkfs, format, raw writes to a
  block device, shutdown — matched only when actually invoked, not as flags or
  arguments). Ordinary destructive work such as deleting files you created is
  allowed
- Output is truncated at 30,000 characters, keeping the head and the tail.
  When that happens the COMPLETE output is saved to a file and the result
  names the path — `grep` or `read_file` that file instead of re-running
  blind. Many programs print their summary at the end, so check the tail first
  and the saved file when the middle matters
- Each `exec` call is a separate process: `cd` and `export` do not survive to
  the next call. Chain them in one command (`cd dir && python build.py`) when
  they matter
- `run_in_background: true` detaches the process: its output goes to a log
  file named in the result, and it keeps running after the call returns. Use it
  for work longer than the timeout ceiling; poll its log with `read_file`. Stop
  background processes you no longer need before you finish

## read_file / write_file / edit_file — paging, reading before editing

- `read_file` returns 2000 lines per call by default. A large file is NOT fully
  read in one call — page through it with `offset`/`limit` until the output no
  longer reports more lines remaining
- Lines longer than 2000 chars are cut with an explicit `(line truncated ...)`
  marker; use `exec` with `cut -c` or `grep -o` when the tail of a long line
  matters
- Absence of a truncation marker means you saw the complete requested range
- `read_file` on an image file returns the picture itself, downscaled if needed
- `edit_file` accepts only a file you have read, or written in full with
  `write_file`, earlier in this session. Read the file yourself, then edit the
  text you actually saw. A file changed externally must be read again.
  `old_string` is the file's content, never the `N| ` line-number prefix
  `read_file` prints
- `write_file` with `mode: overwrite` supplies the complete new content and
  permits subsequent edits. `mode: append` adds only a tail: on an existing
  file it preserves a current read record but cannot establish one for unseen
  or externally changed content. Appending to a new file supplies its complete
  content and permits edits
- When `old_string` matches several places, pass `replace_all: true` to change
  every one, or `occurrence: <n>` to change only the nth non-overlapping exact
  match, counted from 1 in file order. `occurrence` normalizes CRLF to LF but
  does not use fuzzy whitespace matching; do not combine it with `replace_all`
- If a write or edit reports a Python syntax error, fix it before moving on

## grep / glob — search, truncation

- When available, `glob` finds files by pathname pattern (`*.xlsx`,
  `data/**/*.csv`), newest first; otherwise use find. `grep` searches content. Use them instead of `find`/`grep` through
  `exec` — their results are capped, with truncation notices
- Neither tool spills a complete result to a file. `grep` cuts its output at
  30,000 characters and says so in a trailing note; a result marked PARTIAL is
  not the full set — use `output_mode='count'` for exact totals, or narrow the
  pattern or path. `glob` returns at most `limit` entries; a notice that names
  the limit means the result was cut there — raise `limit` or narrow the
  pattern when you need the rest. Brace alternatives such as `*.{csv,xlsx}`
  share one limit after deduplication and recency sorting. If any alternative
  was truncated, the merged result is marked PARTIAL too. A PARTIAL notice that
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
- If the checklist drops out of your context, the system restores it in a
  `<system-reminder>`; when unsure, `read` it


# Office environment

You are running in an isolated Linux container as the non-root user `appuser`
(UID 1000).

The system Python is available as `python` and `python3`; the agent runtime has
its own interpreter. Use system Python for file-processing scripts. These tools
are already installed; check before assuming something is missing.

- Spreadsheets and data: openpyxl, xlsxwriter, xlrd, pandas, numpy, scipy and
  matplotlib. openpyxl writes formulas but does not calculate their cached
  results; recalculate with LibreOffice when computed values are needed.
- Documents and slides: python-docx, python-pptx, reportlab, weasyprint and
  pandoc. LibreOffice converts Office formats to PDF:
  `soffice --headless --convert-to pdf --outdir DIR FILE`.
- PDFs: PyMuPDF, pdfplumber, `pdftotext`, `pdftoppm`; `tesseract` for OCR
  (verify important OCR-derived facts).
- Images, charts and media: Pillow, matplotlib, OpenCV, ImageMagick (`convert`),
  ffmpeg/ffprobe, moviepy, av, librosa, soundfile, pedalboard, pyloudnorm and srt.
- CAD: cadquery.
- Other command-line tools: unzip, zip, 7z, file, jq, wget, curl, git, node and npm.
  Fonts include Noto CJK for Chinese, Japanese and Korean text.
