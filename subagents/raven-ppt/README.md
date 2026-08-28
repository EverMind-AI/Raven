# Raven-PPT

Raven-PPT is the host Raven's deck-building worker. The folder contains a full
Raven checkout plus the launcher and manifest needed to run it as one CLI
subagent. It reads the source documents the caller names, plans an outline,
writes each page as a build script against measured text budgets, renders every
page, reads the rendered pages back, and publishes a `.pptx` only once the deck
clears its own fact and layout gates.

## Capability boundary

Delegate a request whose deliverable is a slide deck: a report turned into a
talk, a research summary, a launch narrative, a technical explainer, a review
built on the caller's own material.

The host supplies the objective, the source documents, the page count and the
output filename. Raven-PPT owns the outline, the page-by-page geometry, the
figures it fetches, the render loop and the gates. It does not spawn another
agent or send messages to external channels.

## How a deck is made

The route is a sequence of tools rather than one prompt:

1. `ppt_prepare` reads the named material and records what the deck may claim.
2. `ppt_template` binds a template, which decides the palette and the faces.
3. `ppt_outline` records one claim per page, what carries it, the figures it
   places and what it says. It does not decide a page's shape: whether the
   content becomes a table, a card row or a chart is settled where the page is
   drawn, with the content in front of the author.
4. `ppt_build` runs the build script, renders the pages and measures them.
   Findings come back per page; a blocking one refuses publication.
5. `ppt_review` renders each page and asks a second reader, with no build
   context, what is wrong with how it looks. The first delivered build runs it
   once by itself, because the measured gates are blind to a picture's size and
   placement and to whitespace beside a shape rather than under it.

The skill and its reference documents live in
`Raven-PPT/raven/memory_engine/skills/ppt-script-authoring/`, and are written
into each job's build directory so the author can open them while drawing.

## Job state

Each run gets its own directory under the host's subagent session root, holding
the deck's sources, its outline, the build script, the rendered pages and the
published file. Nothing reclaims them: at roughly a megabyte and a half a deck,
a few hundred runs is a few gigabytes, so delete the ones you are done with.

## Credentials

Copy `.env.example` to `.env` only when this worker needs credentials of its
own. With `PPT_API_KEY` blank, each launch copies the host Raven's provider
block, routing and selected model, which is the normal setup. `PPT_MODEL` and
`PPT_API_BASE` are read only on the own-key branch: applied on top of an
inherited block they would aim the host's gateway at a model it may not serve.
With no key here and none to inherit, the run exits naming both places one can
go.

Serper and Jina keys fall back to the host tool configuration the same way.
Rendered configs contain live keys, are created below the state root with mode
600, and are deleted after the run. They never belong in this checkout.

Note that the web tools build their HTTP clients with `trust_env=False`, so a
proxy in the environment does not reach them. Set `tools.web.proxy` in
`config.json` instead; it feeds `web_search`, `web_fetch` and `ppt_fetch`.

## Build and register

From the host repository:

```bash
cd subagents/raven-ppt/Raven-PPT
uv sync --extra ppt
.venv/bin/python -c "import PIL, pptx, fitz"   # the extra's three hard imports
soffice --version                              # the render gate's dependency
cd ..
python3 install.py --dry-run
python3 install.py
```

Restart a running host Raven after registration so it reloads the roster.

## Manual worker smoke test

Run from the workspace whose files the worker should read and write. State the
absolute path of every source document in the task text -- that is the only way
material reaches it. Material is optional: name none and the deck is authored
from the model's knowledge, with the prompt making it present what it cannot
verify as a guess.

```bash
python3 /path/to/subagents/raven-ppt/run.py \
    --job my-deck \
    --task "Build a 16-page deck from /abs/path/report.pdf" \
    --verbose
```

`--job` makes a run resumable: the same job name continues the same deck and
workspace. Manual execution proves the inner runtime. A full integration test
must start the host Raven, confirm `Raven-PPT` appears in its subagent roster,
and make the host dispatch the request through `spawn`.

## A licence note worth reading before publishing

The `ppt` extra depends on **PyMuPDF, which is AGPL-3.0** -- the only such
dependency in this tree, and one no other subagent here carries.
`Raven-PPT/NOTICES.md` documents it: PyMuPDF is imported for reading source
PDFs, not vendored, not modified, and no PyMuPDF source is redistributed. AGPL
obligations attach to distributing the library or offering it over a network,
not to importing it, so an internal deployment carries none while a public
service built on this should read the AGPL or take a commercial licence.
