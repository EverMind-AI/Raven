# 出站：不会发生两次的发送

Raven 自己的文档把这句话明明白白说了出来：*「一次重试会重复外部副作用。」*
一次运行给某人发了信、在记下这件事之前死掉、然后被重试——这等于给同一个人
发了两次。对潜在客户而言这不是重试，而是一个 contacting 了他两次的陌生人。

本页描述 `outbound/` 包，它是对这句话的回答。它自己不发送，也不裁决。它记下
尝试过什么，在第一次尚未解决时拒绝第二次尝试，其余的交给人。

**它改变了什么，又还没改变什么。** 这个守卫是一个库，而会使用它的发送路径——
外联那条竖线——尚未建好。在那条路径接到守卫之前，经由其他任何路径发出的消息
仍然可能重复，而排障指南里那句话对它们依然成立。现在存在的是这样一件东西：
它让**采用它的人**不可能重复；以及一条让人看见哪些发送正在等裁决的路。

## 裸发送所没有的三样东西 { #what-a-bare-send-does-not-have }

一个裸的 `channel.send(text)` 有三个缺口，而这三个是同一个：

- **在副作用之前什么也没写下来。** 关于「我们联系过这个人」的唯一记录，是
  之后某次运行可能写下、也可能没写下的那一条。
- **第二次尝试看起来和第一次一样。** 没有任何东西能区分「因为第一次尝试丢了
  而再发一次」与「因为忘了而再发一次」。
- **「我不知道」没有写法。** 一个结果未知的发送只能算作「已发送」或「未发送」，
  而猜错的那个，正好会写两次。

这个包用三样东西把它们关上。

## 在副作用之前 { #before-the-effect }

一次发送在调用方拿到任何可发送的东西**之前**，先被准备成一条 `interaction`
记录。该记录写在 `prepared` 状态，于是「我们是不是已经写过这个人」这个问题
由存储来回答，而不是由重试的乐观来回答。

存储自己的 interaction 链已经说明了这需要什么：`new -> prepared -> sent`，
而 `delivered`、`bounced`、`unsubscribed` 只能从 `sent` 到达。一条消息只有
发出过，才谈得上被投递。这个守卫写的就是那条链，而不是另造一条。

```python
from raven.outbound import guard_send

with guard_send(
    store,
    subject="acme-co",
    subject_type="company",
    channel="email",
    body="Hello there",
) as prepared:
    channel.send(prepared.subject, prepared.message_with_token())
```

记录在代码块运行期间就存在，而不是等它返回之后。若代码块抛出异常，这次发送
会留在 `prepared`，而不是被标成已发送或被遗忘——发送抛出异常并不意味着消息
没有发出去，而一个丢掉了回执的超时，与确实发出去的那次看起来一模一样。

在一次发送未解决期间，对同一 subject 的第二次尝试会被**拒绝**：

```
there is already a send to 'acme-co' whose outcome is unknown
(rv-10D6AABCA0). Reconcile it before sending again.
```

## 当没有人能分辨时 { #when-nobody-can-tell }

一条没人能解决的已准备发送，就是一个**模糊结果**。它会被呈报，而不会被自动
重发，也不会被悄悄放过——「大概没发出去」正是那个会写两次的猜测。

对账会逐条询问一个探测器，一次一条。探测器回答 `sent`、`not_sent`，或任何
别的值来表示「没人能分辨」；默认的探测器什么都分辨不了，所以**除非调用方
明确表示它知道，否则绝不会自动解决任何一条**：

| 探测器的回答 | 会发生什么 | 为什么 |
| --- | --- | --- |
| `sent` | 记录移到 `sent` | 消息发出去了，存储必须继续这样记着 |
| `not_sent` | 记录被移除 | 一条从未离开的消息的记录，是一条不该待在存储里的主张 |
| 其他任何值 | 它留在 `prepared` 并被呈报 | 没人能分辨，所以没人猜 |

剩下的由人来裁决。

## 关联令牌 { #the-correlation-token }

每一次发送都带一个**关联令牌**：一个短小可引用的把手，`rv-` 加十个十六进制
字符。它写在消息里，而引用了它的回复会解析回发出它的那条 interaction——从那里
再回到这条消息所关于的那条记录。

```python
from raven.outbound import find_by_token, token_from_text

token = token_from_text(reply_body)
interaction = find_by_token(store, token)
```

它是把手而不是秘密。它被引用在任何人都可能读到的消息正文里，所以知道一个
令牌，只能告诉你某条回复属于哪条消息，而不能告诉你它背后那条记录的任何事。

## 命令行 { #the-command-line }

`raven outbound` 是这个守卫在操作者这一侧：

| 命令 | 做什么 |
| --- | --- |
| `raven outbound list` | 出站 interaction：发了什么、发给谁 |
| `raven outbound ambiguous` | 结果无人知晓的发送 |
| `raven outbound resolve <token> --sent \| --not-sent` | 人对其中一条的裁决 |

这两种裁决并不对称。`--sent` 把记录移到 `sent`，于是这条消息算作一条已写出的
消息。`--not-sent` 移除它，因为一条从未离开的消息的记录，是一条不该待在存储里
的主张——留着它就等于说我们写了某个我们其实没写的人。

另见[记录与支撑它的证据](records.md)与[数据来源及其条款](sources.md)。
