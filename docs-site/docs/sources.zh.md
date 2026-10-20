# 数据来源及其条款

Agent 取数的各个来源并非可以互换。有的可以随便读、随便用；有的要求结果里带上它的
名字；有的在许可下交付；有的除非你说明理由，否则根本不给。而把一视同仁的部署，
终会从其中一方那里收到通知才明白差别。

本页描述 `sources/` 注册表。当前检出提供该注册表、运维人员的 opt-in 记录，以及
Web 工具出站路径上的守卫、`sources.*` RPC 方法与 `raven sources` 命令；尚未提供设置页。
以下均为经过验证的行为。

## 四个层级 { #the-four-tiers }

| 层级 | 含义 |
| --- | --- |
| `public` | 可自由读取与加工。不必附带任何东西，也不必持有许可 |
| `attributed` | 可用，但来源条款要求它的名字随结果一同带出 |
| `licensed` | 在运维人员已持有或需另行取得的许可下可用 |
| `restricted` | 在运维人员记录下「这套部署为何可以使用」之前不可用 |

层级不是质量评分，而是关于**许可**的陈述。

## 两件事的先后 { #the-order-of-the-two-things }

规则在**请求之前**被查，而不是之后。事后再拒绝没有意义：数据已经离开机器了。

对已注册来源的每一次出站调用，都经由同一个地方——`raven.sources.transport.send`
——因此运行时自带的 Web 工具与研究流 Web 工具都受同一条规则约束。不在注册表里的
来源会被拒绝：代码里加了一个没有登记行的来源，就是没有被人看过，而「去看它」正是
这张表存在的全部意义。

来源被拒绝时，答复会说清命中了哪条规则、以及怎样才能改变结论，因为能改变它的是
运维人员，不是模型：

> Tavily refused by source policy. The source's terms do not permit the use
> as it stands. Tell the user: record why this deployment may use tavily
> (`raven sources optin tavily --basis "..."`), or pick another source. The
> source's terms are at https://tavily.com/.

## 来源上的附加条件 { #conditions-on-a-source }

有些来源可用，但不是对每种用途都可用。这些条件被单独命名，而不是折进层级里，
这样读者能确切看到被关掉的是哪一条：

| 来源 | 被关闭的用途 |
| --- | --- |
| `google_places` | `derivation`——Maps Platform 条款禁止基于 Maps 内容创作内容。`store`——只能保留 place id，不能保留名称、地址与评论 |
| `crunchbase` | `generative_pipeline`——条款禁止将数据用于训练模型，因此流水线需要一份如此约定的许可 |
| `cloudflare_radar` | `commercial`——该数据以 CC BY-NC 4.0 发布，只允许非商业使用 |
| `nominatim` | `platform`——使用政策禁止无代码、低代码与「凭感觉写代码」的平台在其公共 API 之上构建，而 harness 正属于这一类。读取同样数据的受支持方式是 Overpass |
| `g2` | `no_contract`——该 Data API 按授权发放，没有合同的账号根本够不着 |
| `capterra` | `reviews`——没有公开 API；该数据属于 Gartner Digital Markets，只能在商业数据源协议下取得 |
| `linkedin` | `profile_data`——会员数据并未授权采集 |
| `amazon_paapi` | `api`——Product Advertising API v5 已被 Creators API 取代 |
| `google_cse` | `api`——Custom Search JSON API 不再对新客户开放 |

## opt-in { #the-opt-in }

条款不允许的用途，仍可由有理由的运维人员使用：他持有的许可、他参与签署的合同、
或者他能事后指出来的一个决定。注册表不能替他做这个决定，但绝不能让这项用途在
毫无痕迹的情况下发生。

因此理由会与来源、以及它所覆盖的用途写在一起，注册表在放行之前会把它读回来。
没有理由的 opt-in 会被拒绝：否则后来的读者会以为这套部署是被检查过的，而其实没有。

一个 opt-in 只覆盖它写明的那一种用途。「在这套部署里可以用这个来源」并不等于
「它的每一种用途都可以」。

## RPC 接口面 { #the-rpc-surface }

三个 JSON-RPC 方法把注册表暴露出来：

| 方法 | 做什么 |
| --- | --- |
| `sources.list` | 列出各行：每个来源的条款允许什么 |
| `sources.check` | 这套部署能否为该用途使用该来源；若不能，为什么 |
| `sources.optin.set` | 记录，或撤回，这套部署为何可以使用某个来源 |

`sources.check` 从不抛拒绝。这个问题两种情况下都有答案，而一个必须靠捕获异常
才知道「不行」的调用方，会捕获它然后继续走下去。所以答案始终是一个载荷：
`allowed`、层级、理由，以及说明什么能改变这个答案的补救办法。

唯一会抛出的拒绝在 `sources.optin.set` 上：为不在注册表中的来源记录接受，是
`source_policy_refused`（-32034）。为没有人查阅过的条款记录接受，等于把一条
许可挂在一个注册表从未见过的来源上，而下一个读者会把它当成一份已检查过的部署。

## 命令行 { #the-command-line }

`raven sources` 是操作者进入其中的方式：

| 命令 | 做什么 |
| --- | --- |
| `raven sources list` | 列出各行：每个来源的条款允许什么 |
| `raven sources check <source> --use research` | 这套部署能否使用它；若不能，为什么 |
| `raven sources optin <source> --basis "..."` | 记录为何这套部署可以使用它 |
| `raven sources revoke-optin <source>` | 撤回那份接受 |

拒绝会在补救办法中点名 `raven sources optin`，于是操作者被告知该运行什么，
而不只是出了什么事。

## 什么可以被留存 { #what-may-be-kept }

同样的道理延伸到了记录存储。当某个来源的条款禁止留存其数据时，记录不能持有它——
而决定另行处置的部署，会先把该决定针对具体记录记录下来，存储也会在写入落地之前
核对它。参见[记录与支撑它的证据](records.md)。
