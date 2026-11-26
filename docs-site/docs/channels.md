# Channels and Messaging

Channels let people reach Raven through a messaging platform. Agent
integrations let Raven delegate to another agent. These are separate directions:
configuring an OpenClaw agent does not configure Raven's WhatsApp channel, and
enabling Telegram does not create another agent.

## Message path

```text
Platform message
  -> channel adapter: sender/group checks and media handling
  -> Intake: TurnRequest
  -> Gateway / Spine: session scheduling
  -> Agent Loop: tools, memory, delegation
  -> DeliveryHub / ChannelOutletAdapter
  -> originating channel and chat
```

A resident gateway owns channel connections. A one-shot `raven agent -m`
invocation is not a persistent messaging bot. Use [Self-Hosting](self-hosting.md)
for service deployment, or [Launch WebUI](webui.md) for the background host.

## Available adapters

The table reflects shipped `ChannelSpec` declarations, not a live test of your
account. “Files out” means the adapter advertises attachment delivery; it says
nothing by itself about every inbound media format or platform size limit.

| Config name | Platform / connection | Main setup | Files out |
| --- | --- | --- | --- |
| `telegram` | Telegram bot | Bot token | Yes |
| `discord` | Discord Gateway / REST | Bot token and platform bot permissions | Yes |
| `slack` | Slack Socket Mode / Web API | Bot token and app token | Yes |
| `whatsapp` | WhatsApp via Node bridge | Interactive QR linking; bridge configuration | No |
| `weixin` | Personal WeChat via iLink | Interactive QR linking | Yes |
| `wecom` | WeCom AI bot WebSocket | Bot id and secret | Yes |
| `feishu` | Feishu/Lark long connection | App id and app secret | Yes |
| `dingtalk` | DingTalk | Client id and client secret | Yes |
| `matrix` | Matrix sync | Homeserver, user id, access token | Yes |
| `qq` | QQ bot | App id and secret | No |
| `email` | IMAP / SMTP | Receiving and sending account settings | No |
| `mochat` | MoChat | Service configuration and claw token | No |

Use the installed version's commands to inspect fields without guessing:

```bash
raven channels list
raven channels show telegram
raven channels show slack
raven channels status
```

For a source installation, optional channel SDKs are installed with
`uv sync --extra channels`. QR-backed WhatsApp also needs its Node bridge;
dependency readiness does not replace platform authentication.

## Set up a first channel

1. Create the bot/app or account on the platform and grant only the scopes it
   needs. Obtain the correct sender id for your allowlist.
2. Use onboarding/WebUI configuration or merge a channel section into the
   existing Raven config. Preserve provider settings and other channels.
3. Check `raven channels get <name>`; secrets are redacted by default.
4. Start or restart the resident gateway and inspect channel status and logs.
5. Send one plain-text message from an allowed sender. Verify the reply and
   then test any needed group or attachment behaviour.

Example config fragment for a private Telegram bot:

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "REPLACE_WITH_BOT_TOKEN",
      "allowFrom": ["123456789"],
      "workspace": "/absolute/path/to/bot-work",
      "groupPolicy": "mention"
    }
  }
}
```

Replace the token, sender id, and directory before enabling. Never commit a real
token. Prefer the setup UI or a protected local config over a command containing
secrets in shell history.

With credentials already configured, CLI changes can be made explicitly:

```bash
raven channels enable telegram --allow-from 123456789 --group-policy mention
raven channels get telegram
raven gateway
```

The last command starts a foreground host; if a host already runs, restart that
host rather than launching a competing one. CLI configuration writes print a
restart reminder. `channels enable <name>` without fields may show field help
rather than enable anything, and “enabled” is an intent flag, not a health check.

## Sender and group policy

`allowFrom` chooses who may invoke the host. It is separate from tool
permissions, group mention rules, and platform OAuth scopes.

- Specific ids allow only matching senders; use strings for identifiers.
- `["*"]` allows everyone who can reach the channel. The schema defaults to
  this broad value, so set an explicit list for a private deployment.
- `[]` denies everyone; gateway startup refuses an enabled channel with an
  empty allowlist. Disable the channel instead of using an empty active config.
- CLI enable asks before an implicit wildcard on a terminal, and refuses it
  unattended unless the wildcard is explicitly supplied.

Identity matching is platform-specific. Telegram accepts its adapter's
id/username forms; other platforms may use account ids, open ids, or JIDs.
Do not substitute a display name or a room id for a sender id.

Group rules are not uniform: Telegram, Discord, and Feishu expose
`groupPolicy`; Slack adds group/DM settings; Matrix also supports group
allowlisting. Inspect `channels show <name>`. A mention does not override a
sender refusal, and authorizing one person in a group does not make every
participant authorized.

## Sessions and working directories

The normal session address derives from the channel and chat id. An
adapter may provide a more specific key: Slack can scope a group chat
to its thread. Do not assume every sender gets a private memory/session when
they share a room or thread.

`channels.<name>.workspace` chooses the channel's user working directory.
When unset, the channel default is under `~/.raven/tmp/<channel>` (adjusted for
the instance home). It must not point into protected Agent home state such as
memory, skills, or transcripts. This directory is not an OS sandbox and does
not itself restrict what an external agent can reach.

Use dedicated directories and narrow sender policies where people should not
share files. Review [Permissions and Security](permissions.md) before giving a
public or team channel an agent that can edit files or run commands.

## Media and delivery limits

Inbound downloads, transcription, and outbound attachments are separate
capabilities. Some adapters use the shared transcription helper and its
configured provider; WeCom can consume platform-supplied voice transcription.
Receiving audio does not guarantee transcription credentials are configured.

The current channel outlet sends final replies rather than edit-in-place
token streams. Browser/TUI streaming and DAG visualizations are not reproduced
as a full messaging-platform UI.

If an outlet cannot attach files, it sends a “Files ready” notice naming the
files and asks the user to open the same session in Raven UI or TUI. That notice
is not a download link, and a successful model answer is not proof of successful
file upload. Platform permissions, formats, size limits, and transient transport
errors still apply to file-capable adapters.

## Matrix: a federated channel with an opt-in SDK

Matrix is an open protocol for decentralized messaging: accounts live on
homeservers, rooms can span servers, and end-to-end encryption is built in.
Raven supports it because it is the only channel in the table that is both
open-source and end-to-end encrypted by design, which makes it a natural fit for
self-hosted or privacy-conscious deployments where Slack or Discord is not an
option.

The adapter is built on matrix-nio and ships as an optional extra
(`channel-matrix`). It is unavailable on Windows, and the reason is Raven's
own extra rather than the SDK: matrix-nio publishes a `py3-none-any` wheel
and installs anywhere, but the extra declares `sys_platform != 'win32'`, so
the dependency is never pulled in there. It authenticates with a homeserver
URL, a user id, and an access token — no bot approval flow is needed, but
the account must already exist on the homeserver.

End-to-end encryption is on by default (`e2eeEnabled: true`), and it does
not work out of the box: the extra installs matrix-nio without its `e2e`
extra, so `python-olm` is absent, and `MatrixChannel.start()` then raises
`ImportWarning: Encryption is enabled in the client configuration but
dependencies for E2E encryption aren't installed`. Install `matrix-nio[e2e]`
yourself before enabling the channel, or set `e2eeEnabled: false`. Even with
olm installed, the crypto store loads only when `deviceId` is set; without
it a restart may replay recent messages. Turning E2EE off is a deliberate
config change — it exposes message bodies and attachments to anyone who can
read them in transit, while the access token reaches the homeserver either
way.

Room behavior follows `allowFrom`, and Matrix adds its own group rules:
`groupPolicy` defaults to `open` here, where Telegram, Discord, Feishu and
Slack default to `mention`, and the `allowlist` option reads
`groupAllowFrom`, which holds room ids rather than sender ids. A direct
room — two members or fewer — is still gated by `allowFrom`; being direct
is not a bypass. The adapter sends a typing notice while a turn runs and
supports file attachments up to the homeserver's advertised limit (or
`maxMediaBytes`, whichever is lower).

One operational note: because Matrix is federated, the `homeserver` field is
sensitive — it decides which server receives credentials and messages. The
default `https://matrix.org` is a public homeserver; a private deployment should
point at its own.

## QR login and lifecycle

Only adapters declaring interactive login use `channels login`. Today that is
WhatsApp and personal WeChat:

```bash
raven channels login whatsapp
raven channels login weixin
```

These commands pair real accounts and must run in an interactive terminal.
Token-based channels use `channels set` or the configuration UI instead.
WeCom is enterprise WeChat and does not use the personal `weixin` QR flow.

`raven channels disable <name>` preserves stored credentials; restart the host
to apply the CLI change. Disabling is not credential revocation. Revoke leaked
credentials at the platform and replace the local values as well.

## Troubleshooting

| Symptom | First check |
| --- | --- |
| Enabled but not running | Optional SDK, required credentials, gateway logs |
| Direct messages work, groups do not | Bot platform permissions, mention/group policy, sender id |
| Incoming text produces no turn | Allowlist and Intake/gateway wiring |
| Turn finishes but no reply arrives | Outbound credentials/scopes, destination chat, transport error |
| Audio produces no usable text | Adapter media support and transcription configuration |
| “Files ready” but no attachment | Outbound file capability; open the session in Raven UI/TUI |
| QR command fails without a prompt | Interactive terminal, supported adapter, bridge/account state |
| Config edit appears ignored | Restart the actual resident host; inspect its config/home selection |

Use `raven gateway status`, `raven channels status`, and `raven doctor`.
Do not post raw tokens, QR login data, or private message payloads with logs.
Developers adding an adapter should read
[Protocol and Backend Integration](protocol-backends.md).
