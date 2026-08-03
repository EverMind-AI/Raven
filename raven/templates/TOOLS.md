# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

## read_file — Images

- Images (PNG, JPEG, GIF, WebP, and other common formats) are returned as
  pictures, not text. File type is detected from the file's own bytes, so the
  extension does not matter.
- Large images are downscaled before sending (long edge 2000px, and further if
  needed to stay within the per-image token ceiling). The reply says when this
  happened — fine detail and small text may be unreadable after a downscale, so
  crop the region you care about and read that instead of squinting at the whole
  screenshot.
- An image is visible for the turn that read it. Later turns see a placeholder
  with the file path, so read the file again if you need another look.
- Some model endpoints cannot carry an image inside a tool result. There the
  picture arrives in the message right after it instead; nothing is lost.

## cron — Scheduled Reminders

- Please refer to cron skill for usage.
