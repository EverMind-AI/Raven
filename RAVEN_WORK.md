# Raven Work

Raven Work is a minimal version of **raven-code** (Raven v0.2.4) for unattended professional office tasks. This
branch (`feat/raven_work`) is where to get it, and it is not meant to be merged into `main`.

It keeps the whole Raven repository, and the evaluation harness starts raven-code on its own with
`agents/raven-code/run.py`, one container per task, without the main agent.

```
v0.2.4 (3632e60)
└─ 4cc6bcb  office prompts, context and tool descriptions; MCP fix for one-shot runs
   └─ c121a7e  tool-result images stay in place; per-turn compaction cap lifted (two env switches)
      └─ 64a3a03  web_fetch lets the proxy resolve hostnames; web tool errors carry text
```

The commit messages describe each change.

## Tools

| Tool | Default |
|---|---|
| read_file, write_file, edit_file, list_dir, grep, glob, exec, todo | on |
| web_search (Serper, needs `SERPER_API_KEY`) | on |
| web_fetch (Jina Reader, `JINA_API_KEY`; direct fetch as fallback) | on |
| deliver_files | off; turned on per benchmark |
| ask_user, use_skill, read_skill, find_skill, ops_connection_add | off |
| tool_call | removed from the request's `tools` array by the harness |

There are no office-specific tools. The OpenAI GDPval paper and Artificial Analysis's Stirrup harness (GDPval-AA)
give the agent only code execution, pre-installed libraries, web search and fetch, and image viewing. Raven Work
follows that setup: LibreOffice, python-docx / python-pptx, openpyxl, pandas, PyMuPDF and the rest are installed in
the image, and the model uses them through `exec`.

Submission follows Stirrup where a benchmark uses `deliver_files`. The tool only registers the files (a summary plus
absolute paths), and the harness collects the listed files after the run. Stirrup's `finish(reason, paths)` checks
that the files exist, and its session exit saves them.

## Running it

The evaluation harness writes a Raven config and starts the launcher once per task:

```
python agents/raven-code/run.py --config CONFIG --prompt-file TASK.txt --workspace /workspace \
    --session TASK_ID --timeout SECONDS
```

Settings:

- **Container environment**: `RAVEN_IMAGE_IN_TOOL_RESULT=1` and `RAVEN_UNLIMITED_COMPACTION=1`. Both are off by
  default.
- **Context**:
  - `contextWindowTokens` is 262144;
  - compaction is on, with `triggerRatio` 0.7 and `reservedTokens` equal to `max_tokens`;
  - the memory segment is dropped and the memory backend is none;
  - skillForge, session titles, and the everos-memory / ppt-engine / design-engine plugins are off.
- **Sampling**:
  - temperature 1.0, top_p 0.95, top_k 20, min_p 0;
  - presence_penalty 1.5, repetition_penalty 1.0;
  - max_tokens 65536;
  - `chat_template_kwargs: {enable_thinking: true, preserve_thinking: true}`.

  A benchmark may override these (see below).
- **Web**: Serper search and Jina fetch through an HTTP(S) proxy (`tools.web.proxy`). With a proxy set, the proxy
  resolves hostnames.
- **Environment**: one container per task, running as non-root `appuser`, with 2 CPUs, 8 GB memory and 512 pids.
  The image adds LibreOffice, Noto CJK fonts, python-docx/pptx, openpyxl, xlsxwriter, pandas, PyMuPDF, pdfplumber,
  tesseract, ImageMagick, ffmpeg, cadquery and common CLI tools (the full list is in `agents/raven-code/TOOLS.md`).

The model endpoint must show the model a picture sent inside a `role=tool` message, and must honour
`preserve_thinking` and `reasoning_content`.

## How each benchmark uses it

What a benchmark adds lives in the evaluation framework's compatibility layer, not in Raven.

| | GDPval | JobBench | Workspace-Bench-Lite |
|---|---|---|---|
| Tools | The 10 default tools + deliver_files (11) | The 10 default tools | 8: no web_search or web_fetch |
| Network | Open; web tools go through the proxy | Open; web tools and `exec` go through the proxy | None. The model endpoint is still reachable |
| Task message | GDPval-AA task template: reference file paths, the step budget, the deliver_files requirement, then the task | The official JobBench OpenCode prompt, with paths `/workspace/task_folder` and `/workspace/deliverable_files` | The upstream Workspace-Bench prompt wrapper for `/workspace/workdir`, `model_output/` and a final path list |
| Submission | The model calls `deliver_files` with a summary and absolute paths; after the run the harness copies the listed files out | Every file left in `/workspace/deliverable_files`. There is no submit action | Upstream collection: `model_output/`, the final path list, and files left elsewhere in the workspace |
| Budget | 250 steps, no wall clock | 7200 s per task, no step limit | 7200 s per task, no step limit; no per-task memory limit |
| Remaining-step notices | From 20 steps left, every step, in Stirrup's wording. Kept in the conversation | None | None |
| Continue message | `Please continue the task` when the model stops with text but has not delivered. Replaced by the notice once notices run | None | None |
| Sampling | Default | temperature 1.0, top_p 0.95, top_k 20, max_tokens 65536, `preserve_thinking`. presence / frequency penalty, min_p and repetition penalty are not sent; thinking is the provider default (on) | Default |

**GDPval.** The task template adds:

> You have up to {max_steps} steps for this task; a step is one assistant message with its tool calls. You cannot
> interact with the user during the task: make reasonable assumptions when needed and record them in your summary.
>
> In order to complete the task you must use the `deliver_files` tool to submit your work. If you do not use the
> `deliver_files` tool you will fail this task!

The notice (sent as a user message from 20 steps left) and the last-step notice read:

> You have {remaining} turns remaining to complete the task. Please continue. Remember you will need a separate turn
> to call a deliver_files tool.
>
> This is the last turn. Please finish the task by calling a deliver_files tool.

**JobBench.** The prompt is the official one ("Only save the final deliverables to the output directory specified
below. Do not save any intermediate or temporary files."), and only the paths differ. The official judge reads
every file in the output directory, converting each to text (up to 200,000 characters per file). The official
OpenCode and Claude Code runners submit the same way.

**Step budget.** `agent.md` has a short "Step budget" section in the system prompt for every benchmark. It explains
what a remaining-step notice means. Only GDPval sends one.

## Known limitations

- The core changes are new defaults, not switches. On this branch they also apply to the main Raven agent.
- The `RAVEN_IMAGE_IN_TOOL_RESULT` switch disables a request rewrite in LiteLLM 1.105.0 (the image's version). Other
  LiteLLM versions may differ.
- With a proxy, a public hostname that resolves to an address inside the proxy's own network is not blocked in the
  container.
