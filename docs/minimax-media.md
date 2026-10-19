# MiniMax image and video generation

The image and video tools run on MiniMax when their section names one of its two
API-key providers: `minimax` (global, `https://api.minimax.io`) or `minimax_cn_api`
(China, `https://api.minimaxi.com`). The tool then takes that provider's address, key
and headers, and speaks MiniMax's own image and video APIs.

## Configure

Connect MiniMax under Settings > Model, then pick a MiniMax model for the image or the
video role. In `~/.raven/config.json` the same choice reads:

```json
{
  "providers": { "minimax": { "apiKey": "<your MiniMax API key>" } },
  "tools": {
    "media": {
      "image": { "provider": "minimax", "model": "image-01" },
      "video": { "provider": "minimax", "model": "MiniMax-Hailuo-2.3" }
    }
  }
}
```

The key comes from the provider section alone: `MINIMAX_API_KEY` in the environment is
not read for these tools, and the OpenRouter key is never sent to MiniMax. An empty
`provider` moves the role back to OpenRouter.

## Images

- Route: `POST /v1/image_generation`, the picture returned inline.
- Models: `image-01` (the default) and `image-01-live`.
- `aspect_ratio`: `1:1`, `16:9`, `4:3`, `3:2`, `2:3`, `3:4`, `9:16`, `21:9`.
- An input image is the character to keep (`subject_reference`), not a picture to edit.
  MiniMax takes one per request, so the first of several is sent; a local file travels
  as a data URL. MiniMax accepts JPG and PNG under 10 MB.
- A picture MiniMax's content check withholds is answered as a refusal, not as an empty
  answer to ask again for.

## Videos

- `MiniMax-H3` and `MiniMax-H3-Max` take `POST /v2/video_generation`. The other models
  take `POST /v1/video_generation` and hand the result over as a file.
  `MiniMax-Hailuo-2.3` is the default.
- A call is text-to-video unless it gives `first_frame_image` (a local file, an http(s)
  URL or a data: URI), which starts the video from that picture; the picture then sets
  the aspect ratio.
- `params` takes what the model family documents, and any other key is refused before a
  request is sent:
  - `MiniMax-H3` / `MiniMax-H3-Max`: `duration`, `resolution`, `ratio` (text-only calls),
    `callback_url`, `extra` (`prompt_expansion_mode` on `MiniMax-H3-Max`), and
    `aigc_watermark` on `minimax_cn_api`;
  - the other models: `duration`, `resolution`, `prompt_optimizer`,
    `fast_pretreatment`, `callback_url`.
- `MiniMax-H3` and `MiniMax-H3-Max` default to `768P`, 6 seconds, and `16:9` without a
  first frame. Their ranges differ: `MiniMax-H3` takes `768P` or `2K` and 4-15 seconds,
  `MiniMax-H3-Max` `480P` or `768P` and 5-15 seconds. MiniMax answers a value outside
  them with an error.

## Decks

Raven-PPT draws its pictures with the host's image role and follows it live, so a
host whose image role runs on MiniMax draws its decks there too. A deck that pays for
its pictures from an account of its own sets, in the Raven-PPT agent's `.env` (see
its `.env.example`):

```
PPT_IMAGE_PROVIDER=minimax
PPT_IMAGE_API_KEY=<the deck's MiniMax API key>
PPT_IMAGE_MODEL=image-01
```

`PPT_IMAGE_PROVIDER` is needed only when the host's image role runs on another
provider, and then `PPT_IMAGE_MODEL` is too. On a host already on MiniMax,
`PPT_IMAGE_API_KEY` alone puts the deck on its own key. A `PPT_IMAGE_API_BASE` set
without `PPT_IMAGE_PROVIDER` is called as an OpenAI-compatible endpoint, which
MiniMax's address is not.

## References

- [Image generation](https://platform.minimax.io/docs/api-reference/image-generation-t2i)
- [Video generation, MiniMax-H3](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)
- [Video generation, the other models](https://platform.minimax.io/docs/api-reference/video-generation-t2v)
