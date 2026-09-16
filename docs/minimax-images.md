# MiniMax image generation

Set the image tool's API base, model and credential in the Raven configuration:

```json
{
  "tools": {
    "media": {
      "image": {
        "apiBase": "https://api.minimax.io/v1",
        "model": "image-01",
        "apiKey": "<MiniMax API key>"
      }
    }
  }
}
```

For the China region, use `https://api.minimaxi.com/v1`. The model can also be
`image-01-live`. With either regional base, `MINIMAX_API_KEY` can supply the
credential when `apiKey` is empty.

Call `image_generate` with a prompt and `images` containing portrait reference
URLs, PNG/JPEG local paths, or base64 image data URLs. References are sent as
MiniMax character subjects; this is portrait reference generation, not a mask
editing operation. Input images must meet the MiniMax format and size limits.
The `aspect_ratio`, `filename`, `output_dir`, and per-picture `prompts` batch
options continue to work. Generated images are saved locally and returned in
`paths`.

See the [MiniMax image-to-image API](https://platform.minimax.io/docs/api-reference/image-generation-i2i).
