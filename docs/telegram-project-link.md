# Use a Telegram DM with an existing page conversation

The gateway can bind one Telegram private chat to an existing page conversation. The page keeps its session key, history and working directory. Telegram turns use the same serial lane as page turns, so they cannot run concurrently against that conversation.

This is an opt-in, single-owner setup. Without the binding file, channel routing is unchanged. With it, other Telegram senders, groups and topics are refused. This initial implementation requires POSIX file ownership checks; configuring a binding on an unsupported platform fails closed, while an absent file leaves the gateway unchanged.

## Configure the link

1. Configure and enable Telegram normally. Put the owner's numeric Telegram user ID explicitly in the channel's `allow_from` list. A wildcard alone does not authorize explicit outbound sends.
2. Identify the exact existing page session key. Do not use its title or guess a key. The gateway must host the page that owns the conversation.
3. Create `telegram-project-link.json` in the configured agent workspace. It must be a regular file owned by the gateway user, with no group or other permissions, for example mode `0600`. Symlinks are refused.
4. Set these fields, replacing the synthetic values below with verified local values:

```json
{
  "chat_id": "123456789",
  "sender_id": "123456789",
  "session_key": "tui:existing_project"
}
```

The chat and sender IDs must match and must be positive numeric strings. The file contains no bot token. Restart the gateway at a safe checkpoint after creating or changing it. The gateway refuses a malformed or unsafe binding file. If the page or session is unavailable, an incoming message gets an unavailable response instead of silently creating another conversation.

Existing standalone Telegram history is left in place; the gateway does not merge or overwrite it.

## Replies and explicit updates

A Telegram question and its reply stay on Telegram, and the resulting exchange belongs to the existing page conversation. New Telegram input queues behind an active page turn instead of being injected into that turn. Page input during a Telegram turn runs separately rather than inheriting the DM's reply address.

Questions and approvals raised by a Telegram turn go to the linked DM. The link does not expose a credential-entry responder. `/stop` cancels work on the shared conversation, including queued page turns and associated subagents. Other slash commands are refused in the linked DM; use the page for resets and configuration changes.

Linking does not broadcast background worker output, tool chatter, progress notices or page replies. To send an explicit update from the project, the `message` tool must name both the channel and chat ID:

```json
{
  "content": "The requested milestone is complete.",
  "channel": "telegram",
  "chat_id": "123456789"
}
```

Changing only one destination field is rejected rather than completed from the current reply address. The destination must be explicitly admitted and its adapter must be running. Same-target messages retain the existing streaming behavior.

`Message queued ... (transport delivery pending)` means that the native delivery hub accepted the message, not that Telegram delivered it. Successful Telegram text and media sends log the returned message ID. Final transport failures reach the delivery hub with sanitized errors. Receipt logs contain chat/message identifiers, so treat them as private operational data.

## Disable the link

Remove the binding file and restart the gateway at a safe checkpoint. This restores independent Telegram conversations without deleting either transcript. Reverting the code change also disables the link; the old gateway ignores the file.
