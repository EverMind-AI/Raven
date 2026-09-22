# 渠道与消息

渠道让用户通过消息平台访问 Raven；Agent 集成让 Raven 委派给另一 Agent。这是两个
方向：配置 OpenClaw Agent 不会配置 Raven 的 WhatsApp 渠道，启用 Telegram 也不会
创建另一 Agent。

## 消息路径 { #message-path }

```text
Platform message
  -> channel adapter: sender/group checks and media handling
  -> Intake: TurnRequest
  -> Gateway / Spine: conversation scheduling
  -> Agent Loop: tools, memory, delegation
  -> DeliveryHub / ChannelOutletAdapter
  -> originating channel and chat
```

常驻网关拥有渠道连接。单次 `raven agent -m` 不是持久消息机器人。服务部署见
[自托管](self-hosting.md)，后台宿主见[启动 WebUI](webui.md)。

## 可用适配器 { #available-adapters }

表格来自 Raven 发布包中的 `ChannelSpec` 声明，不是对你账户的实测。“发送附件”指适配器声明支持
附件投递，不代表所有入站媒体格式或平台大小限制都相同。

| 配置名 | 平台/连接 | 主要设置 | 发送附件 |
| --- | --- | --- | --- |
| `telegram` | Telegram bot | Bot token | 是 |
| `discord` | Discord Gateway / REST | Bot token 与平台 bot 权限 | 是 |
| `slack` | Slack Socket Mode / Web API | Bot token 和 app token | 是 |
| `whatsapp` | 经 Node bridge 连接 WhatsApp | 交互式扫码、bridge 配置 | 否 |
| `weixin` | 经 iLink 连接个人微信 | 交互式扫码 | 是 |
| `wecom` | 企业微信 AI bot WebSocket | Bot id 和 secret | 否 |
| `feishu` | 飞书/Lark 长连接 | App id 和 app secret | 是 |
| `dingtalk` | 钉钉 | Client id 和 client secret | 是 |
| `matrix` | Matrix sync | Homeserver、user id、access token | 是 |
| `qq` | QQ bot | App id 和 secret | 否 |
| `email` | IMAP / SMTP | 收发账户设置 | 否 |
| `mochat` | Mochat | 服务配置与 claw token | 否 |

用已安装版本的命令查字段，不要猜测：

```bash
raven channels list
raven channels show telegram
raven channels show slack
raven channels status
```

源码安装使用 `uv sync --extra channels` 安装可选渠道 SDK。WhatsApp 扫码还需要
Node bridge；依赖就绪不能替代平台认证。

## 接入第一个渠道 { #set-up-a-first-channel }

1. 在平台创建 bot/app 或账户，仅授予必要 scope，获取正确的发送者 id。
2. 通过 onboarding/WebUI 配置，或将渠道片段合并进已有 Raven 配置，保留模型和其他渠道设置。
3. 用 `raven channels get <name>` 检查；默认会脱敏秘密。
4. 启动或重启常驻网关，检查渠道状态与日志。
5. 从允许的发送者发一条纯文本消息，确认回复后再验证群聊或附件。

私人 Telegram bot 配置片段：

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

启用前替换 token、发送者 id 和目录。真实 token 不要提交到 git。优先使用设置 UI
或受保护的本地配置，避免把秘密写入 Shell 历史。

已配置凭据时，可以显式通过 CLI 修改：

```bash
raven channels enable telegram --allow-from 123456789 --group-policy mention
raven channels get telegram
raven gateway
```

最后一条启动前台宿主；若已有宿主运行，应重启它，而不是启动竞争进程。CLI 配置写入
会提示重启。不带字段的 `channels enable <name>` 可能只展示字段帮助，不会启用；
“enabled”是意图标志，不是健康检查。

## 发送者与群聊策略 { #sender-and-group-policy }

`allowFrom` 决定谁能调用宿主，与工具权限、群聊 mention 规则和平台 OAuth scope 分开。

- 具体 id 只允许匹配发送者，标识应使用字符串。
- `["*"]` 允许所有能访问渠道的人。Schema 默认值较宽，因此私人部署应显式设置名单。
- `[]` 拒绝所有人；网关拒绝以空白名单启动已启用渠道。要关闭渠道应 disable，而非使用空的活动配置。
- CLI 在终端启用隐式 wildcard 前询问；无人值守时拒绝，除非显式传入 wildcard。

身份匹配因平台而异。Telegram 支持适配器的 id/username 形式；其他平台可能使用
account id、open id 或 JID。不要把显示名或 room id 当作 sender id。

群聊规则不统一：Telegram、Discord、Feishu 提供 `groupPolicy`；Slack 另有群聊与 DM
设置；Matrix 还支持群白名单。请查 `channels show <name>`。Mention 不会覆盖发送者
拒绝；授权群内一个人也不等于授权所有参与者。

## 会话与工作目录 { #sessions-and-working-directories }

通常对话地址来自渠道和 chat id。适配器可以提供更细的键，例如 Slack 可按群聊 thread
隔离会话。多人共享 room/thread 时，不应假定每位发送者都有私人记忆或会话。

`channels.<name>.workspace` 选择渠道的用户工作目录。未设置时，默认位于
`~/.raven/tmp/<channel>`，并跟随实例 home 调整；不能指向 Agent home 中受保护的
记忆、技能或 transcript 状态。工作目录不是 OS 沙箱，也不会自动限制外部 Agent 能访问什么。

需要隔离文件时使用专用目录和窄发送者策略。让公开或团队渠道调用能改文件、执行命令
的 Agent 前，请读[权限与安全](permissions.md)。

## 媒体与投递限制 { #media-and-delivery-limits }

入站下载、转录和出站附件是不同能力。部分适配器使用共享转录 helper 和其配置的服务商；
企业微信可以接收平台提供的语音转录。能收到音频不代表已配置转录凭据。

当前 channel outlet 投递最终回复，不提供逐 token 原位编辑流。浏览器/TUI 的流式输出
和 DAG 可视化不会完整复制成消息平台 UI。

不支持附件的 outlet 会发送“Files ready”提示并列出文件名，请用户在 Raven UI/TUI
打开同一会话。这不是下载链接；模型回答成功也不代表附件上传成功。支持文件的适配器
仍受平台权限、格式、大小上限和瞬时传输错误影响。

## 扫码登录与生命周期 { #qr-login-and-lifecycle }

只有声明 interactive login 的适配器才使用 `channels login`，目前是 WhatsApp 和个人微信：

```bash
raven channels login whatsapp
raven channels login weixin
```

这些命令会配对真实账户，必须在交互式终端运行。Token 型渠道使用 `channels set`
或配置 UI。企业微信 WeCom 不使用个人 `weixin` 的扫码流程。

`raven channels disable <name>` 保留已存凭据；重启宿主以应用 CLI 修改。Disable
不等于撤销凭据。泄漏时还要在平台撤销，并替换本地值。

## 排障 { #troubleshooting }

| 现象 | 优先检查 |
| --- | --- |
| Enabled 但未运行 | 可选 SDK、必填凭据、网关日志 |
| 私聊可用，群聊不可用 | 平台 bot 权限、mention/group 策略、sender id |
| 入站文本没有进入轮次 | 白名单与 Intake/网关连接 |
| 轮次完成但无回复 | 出站凭据/scope、目标 chat、传输错误 |
| 音频没有可用文本 | 适配器媒体支持与转录配置 |
| 有“Files ready”但没附件 | 出站文件能力；在 Raven UI/TUI 打开会话 |
| QR 命令不显示提示就失败 | 交互式终端、适配器支持、bridge/账户状态 |
| 配置编辑未生效 | 重启实际常驻宿主，检查其 config/home 选择 |

使用 `raven gateway status`、`raven channels status`、`raven doctor`。分享日志时不要
公开原始 token、二维码登录数据或私有消息 payload。增加适配器的开发者请读
[协议与后端集成](protocol-backends.md)。
