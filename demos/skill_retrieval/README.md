# skill_retrieval — agent finds the skill, follows it, generates an image

Showcases an Raven agent doing a real end-to-end run:

  1. User gives a prompt.
  2. Agent sees the local skills directory in its system prompt.
  3. Agent picks ``image-gen`` and ``read_file``s its SKILL.md.
  4. Agent writes a Python script following the SKILL.md "Quick recipe".
  5. Agent's ``exec`` tool runs the script — POSTing to OpenRouter's
     Nano Banana endpoint and writing a PNG to disk.

No hand-rolled glue, no fake harness — same code path as a real user
typing a prompt into ``raven agent``.

## What's in this directory

```
demos/skill_retrieval/
├── README.md              ← you are here
├── run_agent_demo.sh      ← one-line driver: feeds prompt to ``raven agent``
├── skills/
│   └── image-gen/
│       └── SKILL.md       ← agent-facing how-to for Nano Banana on OpenRouter
└── example_output.png     ← image from a previous run (committed as proof)
```

## Run it

```bash
# default prompt: watercolor fox in autumn leaves
bash demos/skill_retrieval/run_agent_demo.sh

# custom prompt:
bash demos/skill_retrieval/run_agent_demo.sh "render a cyberpunk skyline at night"
```

Requires an OpenRouter API key:

  - either ``OPENROUTER_API_KEY`` in env,
  - or written to ``raven/key.env`` (one line, no quotes).

The script auto-injects the key into a temp config and points the agent
at this directory as its workspace, so the demo doesn't touch your
``~/.raven/config.json``.

## What you should see

The agent's intermediate "↳" lines show its reasoning + tool calls.
A successful run looks roughly like::

    ↳ I'll use the image-gen skill to generate that watercolor painting
      for you. Let me first read the skill documentation to understand
      how to use it.
    ↳ Now I'll create a Python script to generate the watercolor
      painting of a fox in autumn leaves using the image-gen skill...

    🦞 Raven
    Perfect! I've successfully generated a watercolor painting of a fox
    sitting in autumn leaves and saved it to ``./fox.png``. The image is
    about 2.4 MB and features soft watercolor styling with warm autumn
    colors.

The agent picks the output filename from your prompt, so a custom
prompt usually writes a differently-named PNG. ``example_output.png``
in this repo is the result of one default-prompt run — sample output,
not a fixed contract.

## What this proves

  - **SKILL.md is the agent's source of truth** — no special "image-gen"
    code path in Raven, just a markdown file the LLM is told to read
    and follow.
  - **Real OpenRouter call works through the agent's exec tool** — the
    Python script the LLM writes hits ``google/gemini-2.5-flash-image``
    and decodes the base64 PNG.
  - **Cost is bounded and visible** — Nano Banana v2.5 prices around
    $0.04 per 1024×1024 image; the SKILL.md documents this so the agent
    can budget when looping.

## Troubleshooting

- **403 "not available in your region"**: the proxy in ``run_agent_demo.sh``
  defaults to a worker-internal address. From elsewhere, override
  ``HTTPS_PROXY`` to a proxy that exits via a non-blocked region.
- **No PNG written**: check the agent's stdout — if the LLM forgot
  ``"modalities": ["image", "text"]`` the response is text-only. The
  SKILL.md "Troubleshooting" section calls this out, so the agent
  usually self-corrects.
- **Empty output / hung run**: increase the proxy timeout, or unset
  ``HTTPS_PROXY`` if you're already on a non-CN network.
