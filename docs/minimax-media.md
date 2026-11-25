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

Set the image model to `MiniMax-H3-Image` or `MiniMax-H3-Image-Highspeed` for
image generation. Both use `POST /v1/images/generations` on the global
(`api.minimax.io`) or China (`api.minimax.cn`) endpoint. They accept an optional
`aspect_ratio` (`auto`, or `W:H` with sides between 1 and 300 and a ratio from
1:3 to 3:1) and take a text prompt. A call that carries reference images goes to
`POST /v1/images/edits` instead, with the pictures as `images[].image_url`, one
to sixteen of them; every reference named is sent, and more than sixteen is
refused before sending. The text-to-image route takes the prompt alone: it drops
a reference field without an error, answers HTTP 200 and bills the call, so a
reference-carrying call is never sent there. A picture normally returns in about
ten to twenty seconds; the request is given a 600-second window, and a timed-out
request can still finish and incur a charge, so avoid resubmitting it.

The legacy image models keep their existing behavior:

- Route: `POST /v1/image_generation`, the picture returned inline.
- Models: `image-01` (the default) and `image-01-live`.
- `aspect_ratio`: `1:1`, `16:9`, `4:3`, `3:2`, `2:3`, `3:4`, `9:16`, `21:9`.
- An input image is the character to keep (`subject_reference`), not a picture to edit.
  MiniMax takes one per request; a request naming more is answered with HTTP 200 and
  `base_resp.status_code` 2013 (`invalid params, image_reference must be one`), and that
  reason reaches the caller. A local file travels as a data URL. MiniMax accepts JPG and
  PNG under 10 MB.
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
  first frame. A text-only call that names `adaptive` is refused before a request is
  sent, since only a first frame takes it; naming another ratio is passed through.
  Their ranges differ: `MiniMax-H3` takes `768P` or `2K` and 4-15 seconds,
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

A route that hands a deck to Raven-PPT opens only when the deck can draw, and it reads
these settings the way the launcher does: a deck on a provider of its own is ready with
`PPT_IMAGE_API_KEY` or the host's key for that provider, never with the host's image
key for another one.

On an H3 image role the deck inherits the host's route and deadline: a picture asked
for with `references` is edited through the host's edits route, up to the deck's own
six references, and the deck takes its deadline from the host image tool, so a batch
gets one H3 window per four pictures in flight instead of the deck's own six minutes.

## References

- [Image generation](https://platform.minimax.io/docs/api-reference/image-generation-t2i)
- [Video generation, MiniMax-H3](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)
- [Video generation, the other models](https://platform.minimax.io/docs/api-reference/video-generation-t2v)
