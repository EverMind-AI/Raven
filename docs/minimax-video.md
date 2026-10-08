# MiniMax image-to-video

Configure the video tool in `~/.raven/config.json`:

```json
{
  "tools": {
    "media": {
      "video": {
        "model": "MiniMax-H3",
        "apiBase": "https://api.minimax.io"
      }
    }
  }
}
```

Set `MINIMAX_API_KEY`, or set `tools.media.video.apiKey` to your MiniMax
API key. Video credentials are explicit and are not borrowed from the chat
configuration. For the China endpoint, use `https://api.minimaxi.com`.

Call `video_generate` with:

```json
{
  "prompt": "The waves roll gently toward the camera",
  "first_frame_image": "https://images.example.com/beach.png",
  "params": {"duration": 5, "resolution": "2K"}
}
```

The image must be a public HTTP(S) URL or an image data URI. Local file paths
are not uploaded. MiniMax-H3 requires a non-empty prompt, uses a first-frame
image content item, and supports integer durations from 4 to 15 seconds.
The tool polls the task and saves the completed video under the workspace.

The V1 image-to-video models MiniMax-Hailuo-2.3, MiniMax-Hailuo-2.3-Fast,
MiniMax-Hailuo-02, I2V-01-Director, I2V-01-live, and I2V-01 use the
`first_frame_image` field and the V1 query and file retrieval flow.
Their duration and resolution settings depend on the selected model.

Configure a MiniMax video model before using a MiniMax model override.
A first-frame image is required for this MiniMax integration.

API reference: [MiniMax V2 video generation](https://platform.minimax.io/docs/api-reference/video-generation-v2-create).
