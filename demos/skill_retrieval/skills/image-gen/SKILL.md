---
name: image-gen
description: Generate images via Nano Banana (Gemini 2.5/3.1 Flash Image) on OpenRouter. Use when the user asks to draw, illustrate, render or generate any kind of picture/diagram/scene.
metadata: '{"raven": {"requires": {"env": ["OPENROUTER_API_KEY"]}}}'
---

# image-gen — Nano Banana via OpenRouter

Generates one or more images from a text prompt (and optionally one or more
input images) by calling Google's **Nano Banana** family on OpenRouter:

  - `google/gemini-2.5-flash-image`            — original (Nano Banana)
  - `google/gemini-3.1-flash-image-preview`    — latest (Nano Banana 2)

OpenRouter speaks the OpenAI-compatible chat-completions API for these
models, with two extras:

  1. The request must include `"modalities": ["image", "text"]` so the
     server knows to return image bytes, not just a description.
  2. The response carries images in a top-level ``message.images`` array
     (NOT in ``content`` — that field still holds optional commentary text).

## Quick recipe

```python
import os, base64, json, urllib.request

KEY = os.environ["OPENROUTER_API_KEY"]
MODEL = "google/gemini-2.5-flash-image"   # or 3.1 for "Nano Banana 2"

def generate_image(prompt: str, out_path: str = "out.png") -> str:
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
        }).encode(),
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    msg = data["choices"][0]["message"]
    # ``message.images[i].image_url.url`` is a data URI:
    #   "data:image/png;base64,<base64-bytes>"
    url = msg["images"][0]["image_url"]["url"]
    b64 = url.split(",", 1)[1]
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(b64))

    text_note = msg.get("content") or ""
    return f"Wrote {out_path} ({len(b64)//1024} KB). Model said: {text_note[:200]!r}"
```

## Request shape (full)

```json
{
  "model": "google/gemini-2.5-flash-image",
  "messages": [
    {"role": "user", "content": "A red circle on white background"}
  ],
  "modalities": ["image", "text"]
}
```

For **image input + image output** (edit / vary / extend), use the standard
multipart `content` form OpenAI clients accept. The image part can be either
a remote URL or a base64 data URI:

```python
{
  "role": "user",
  "content": [
    {"type": "text", "text": "Make this watercolor style"},
    {"type": "image_url",
     "image_url": {"url": "data:image/png;base64,iVBORw0KGgo..."}}
  ]
}
```

## Response shape

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "optional text commentary",
      "images": [
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/png;base64,<bytes>"
          }
        }
      ]
    }
  }],
  "usage": {
    "prompt_tokens": 8,
    "completion_tokens": 1295,
    "total_tokens": 1303,
    "cost": 0.0383,
    "completion_tokens_details": {"image_tokens": 1290}
  }
}
```

## Cost (observed)

  ~**$0.04 per 1024×1024 PNG** at the time of writing
  (1290 image tokens × $0.00003/tok via the v2.5 flash-image model).
  Burst test: 10 images ≈ **$0.40**. Budget gates accordingly when looping
  in agent code.

## When to use this skill

Trigger keywords / patterns you should recognize:

  - "generate / create / make / draw / render / illustrate (an image / a picture / a logo / ...)"
  - "show me what X looks like"
  - "design a banner / poster / icon for ..."
  - "make it watercolor / cyberpunk / pencil sketch / pixel art" (edit existing)
  - "vary this image" / "more like this but with ..."

Don't use this skill for:

  - **Charts / plots from data** → prefer a Python plotting skill
    (matplotlib / plotly) rather than a generative image. Charts need
    accurate numbers; Nano Banana will hallucinate axes.
  - **Diagrams with precise structure** (UML, DAGs) → use mermaid /
    graphviz instead.

## Troubleshooting

  - **Empty `images` array**: you forgot `"modalities": ["image", "text"]`.
    OpenRouter falls back to text-only without it.
  - **HTTP 403 with "not available in your region"**: OpenRouter blocks
    Anthropic + some Google models from China-mainland IPs. Set
    ``HTTPS_PROXY`` to a proxy that exits via a non-blocked region.
  - **Response includes `refusal`**: prompt was content-filtered. Rewrite
    the prompt; don't retry the same string.
  - **PNG opens to blank / corrupt**: you wrote the data URI verbatim
    instead of decoding. Always strip the ``"data:image/png;base64,"``
    prefix and ``base64.b64decode`` the rest before writing.
