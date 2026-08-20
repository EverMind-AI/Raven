# Ravenx-PPT

Hand it your source material and a `.pptx` template; it hands back a deck.

[中文说明](README.zh-CN.md)

Ravenx-PPT is a deck-authoring build of [Raven](https://github.com/EverMind-AI/Raven).
Give it a paper, a report, a set of documents or a URL — plus, optionally, a
PowerPoint template of your own — and it reads the material, decides what the deck
argues page by page, writes the deck, looks at every page it made, and fixes what it
finds before handing the file over.

> Pre-alpha. Interfaces and configuration may change.

## What comes out

A real `.pptx`. Every page is shapes and text that open in PowerPoint or Keynote and
can be edited there — nothing is a flattened screenshot of a page, and no chart is a
picture of a chart. Figures and tables lifted out of your source PDFs are placed as
figures; tables are rebuilt as tables.

The model writes a python-pptx program rather than filling in slots, so the layout of
a page is chosen for what that page has to say. What keeps that from going wrong is
measurement: after every build the engine renders each page, measures the file and the
render, and hands back what it found — type below the floor, words overlapping words,
copy off the canvas, a figure hidden under a card, a page citing a number the sources
never state. Most of it goes back for another pass; some of it refuses to publish.

## Requirements

| | Why |
| --- | --- |
| Python 3.13 | |
| [uv](https://docs.astral.sh/uv/) | dependency management (or use pip) |
| **LibreOffice** | renders each page so the model and the gates can see it. Without it the deck still builds, but nothing can look at it |
| CJK fonts | needed for Chinese, Japanese or Korean decks — without them the render shows tofu boxes and every visual check is meaningless. On Debian/Ubuntu: `apt install fonts-noto-cjk` |
| poppler-utils | optional fallback for page rasterisation and word positions when pypdfium2 is unavailable |
| An LLM API key | OpenRouter, Anthropic, OpenAI, or any provider Raven supports |

## Install

```bash
git clone git@github.com:Tchen-data/Ravenx-PPT.git
cd Ravenx-PPT
uv sync --extra ppt
```

With pip instead: `pip install -e ".[ppt]"`.

Check the toolchain found LibreOffice:

```bash
uv run raven doctor
```

## Configure

`config.example.json` is a working OpenRouter configuration. Copy it and put your key
in:

```bash
mkdir -p ~/.raven
cp config.example.json ~/.raven/config.json
$EDITOR ~/.raven/config.json
```

```json
{
  "agents": {
    "defaults": {
      "provider": "openrouter",
      "model": "anthropic/claude-opus-5",
      "maxToolIterations": 120
    }
  },
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-REPLACE_WITH_YOUR_OWN_KEY",
      "apiBase": "https://openrouter.ai/api/v1"
    }
  },
  "tools": { "ppt": { "enabled": true } }
}
```

Only four settings, and everything else falls through to the code's own defaults.
`maxToolIterations` is the one raised from its default of 40: making a deck of a dozen
pages and reviewing every one of them took 95 to 120 tool calls in the runs this was
built against, so the default stops a real deck halfway through.

The deck work needs a capable model. It writes and repeatedly edits a several-hundred
line program while reading page renders, which is the hardest thing in the toolchain.

`raven onboard` walks through provider setup interactively if you would rather not
edit the file.

## Run

```bash
uv run raven tui
```

Then say what you want, in the language you want it in:

> Turn `papers/tarvis.pdf` into a 12-page Chinese deck for an internal technical
> review, using the template at `~/templates/house.pptx`.

Attach material by path. Anything you point it at — a PDF, a directory of documents,
a URL — is read into the project first.

For a single non-interactive turn: `uv run raven agent -m "..."`.

## Where things land

Under the workspace (`~/.raven/workspace` by default):

```
ppt_projects/<name>/
  sources/     every document the deck stands on
  ingest/      extracted text, figures and tables
  build/       build.py — the program that draws the deck — and the deck it built
  review/      page renders
  state/       the brief and the outline
exports/<name>/deck.pptx    what gets delivered
```

`build/build.py` is worth knowing about: it is the whole deck as code. Edit it and
rebuild and you get your edit; ask for a change in the conversation and this is what
gets rewritten.

## Using your own template

Point it at a `.pptx` and the deck is built inside that template's master, theme,
layouts and canvas.

Its cover, contents, section divider and closing page are **cloned** — those are the
pages a reader recognises a house style by, and most templates build them out of
gradients, custom geometry and fills that no library can reproduce. Content pages are
**composed**, inside the style measured off the template: its title row, its type
ladder, its palette, its safe area. A template's own example content — stock photos,
lorem text, the vendor's watermark — is reported if any of it survives into the deck.

## What refuses to publish

Most measurements come back as warnings and go into the next pass. Seven refuse
outright, because no rearrangement fixes them:

| | |
| --- | --- |
| `fact`, `citation` | a number or a figure reference the sources do not support |
| `page_budget`, `language` | not the deck that was agreed |
| `band` | a filled colour bar carrying nothing — the loudest tell of a generated deck |
| `house_style` | the deck's theme is not the template's |
| `unmapped_page` | a page the outline never planned |

Plus, per page: text painted over text, a shape covering another shape's content,
copy clipped by its own box, and an escape sequence printed as characters.

## Configuration reference

Everything below is optional; the value shown is what the code uses when you say
nothing.

| Key | Default | |
| --- | --- | --- |
| `tools.ppt.enabled` | `true` | set false to get the general-purpose agent back |
| `tools.ppt.profile` | `script_author` | the only implemented route |
| `tools.ppt.renderDpi` | `144` | what a page render is rasterised at before a model sees it |
| `tools.ppt.renderConcurrency` | `2` | concurrent LibreOffice conversions |
| `tools.ppt.deckName` | `deck.pptx` | the delivered filename |
| `tools.ppt.designer.enabled` | `false` | a second pass that rearranges the finished deck. Off by default: measured over four runs it removed copy it was told to preserve and added decoration the gates then refused |
| `tools.ppt.designer.model` | `""` | empty means the main model |
| `agents.defaults.workspace` | `~/.raven/workspace` | |

## Licence and attribution

Apache-2.0. Built on [Raven](https://github.com/EverMind-AI/Raven) by EverMind AI,
which is itself built on [nanobot](https://github.com/HKUDS/nanobot) and
[hermes-agent](https://github.com/NousResearch/hermes-agent). Third-party notices are
in [`NOTICES.md`](NOTICES.md).

One dependency is not permissively licensed: **PyMuPDF is AGPL-3.0**. It reads the
source PDFs — their text with layout, and the figures and tables inside their pages —
and nothing else here can do that. It is imported, not modified and not
redistributed. AGPL obligations attach to distributing the library or offering it over
a network, not to importing it, so a local or internal deployment carries none; a
public service built on this should read the AGPL or take Artifex's commercial
licence. Rasterisation and word positions deliberately use pdfium (Apache-2.0)
instead, which keeps the AGPL surface to reading source documents.
