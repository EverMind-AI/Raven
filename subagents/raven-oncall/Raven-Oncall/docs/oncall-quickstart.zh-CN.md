# 上手:体验 raven 的值守(on-call)能力

**写于 2026-08-05,对应分支 `feat/ops_round3_device`。**

这份只讲**值守这条线特有的东西**。装环境、配 provider 看仓库根目录的 `README.zh-CN.md`。

> ⚠️ **这是在研能力,不是已发布特性。** 下面 §5 列的粗糙处是实测出来的,
> 不是待办清单里的猜测。读完 §5 再决定要不要投入时间。

> **值守是什么**:你把一个要跑很久的作业交给 raven,它自己排时间回来看,
> 到岔路口时要么在授权范围内自己处置,要么上报一次。
> 它**不是**你盯着终端等它干活 —— 贴完任务就可以走开。

---

## 1. 三分钟跑起来

```bash
uv sync
```

### ⚠️ 坑 1:必须先构建 TUI,否则第一步就报错

`ui-tui/dist/entry.js` 是 gitignore 的构建产物,**新克隆里没有**:

```bash
cd ui-tui && npm install && npm run build && cd ..
```

不构建的话 `raven tui` 会报 `✗ TUI 构建产物缺失：…`,**而且报错里直接给了这条命令**,
所以踩了也不会卡住。需要 **Node ≥ 22**(低了也有明确提示,让你 `nvm install 22`)。

### 启动

```bash
uv run raven tui
```

**第一次启动会弹引导向导** —— 检测到没配 provider key + 默认模型时自动进,
跟着填就行,**不用手写配置文件**。

> 我们这几轮实验用的是内网自部署的 **qwen3.6-27B**(`custom` provider)。
> 换成别的模型完全能跑,但**值守行为跟模型强相关** ——
> 下面 §5 那些"粗糙处"有一部分是这个模型的表现,换模型不一定一样。

### ⚠️ 坑 2:同时开多个实例时,`workspace` 还要手改一次

不改会**共用同一个工作区**(同一份会话、同一份记忆,内存索引还会争锁),
**而且不报错,只是悄悄共用**。2026-08-13 实测发作过一次:两份配置是从第三份抄来的,
别的路径都跟着新位置走了,就这一行留在原地。

```bash
uv run raven tui --config ~/.raven-mine/config.json
```

```json
"agents": { "defaults": { "workspace": "~/.raven-mine/workspace" } }
```

`--config` 会带走 cron store、日志、campaign 台账(含报告和唤醒记账),
**以及 traces**(2026-08-13 起:没设 `RAVEN_TRACING_DIR` 时按配置文件所在目录推,
不传 `--config` 的默认位置逐字节不变)。**只剩 `workspace` 要手改。**

> campaign 台账原先**也不跟**:三个模块各有一份导入期硬编码的 `~/.raven/ops`。
> 后果不是文件放错位置 —— 报告落进一个没有 `meta.json` 的新目录,
> 于是**起始值闸门无可比之物,拒不了**,「没问题」和「没得核」都输出 `Accepted`。
> 现在统一走 `config.paths.get_ops_home()`,按调用求值。

---

## 2. 值守是怎么用的:贴一段话,然后走开

TUI 起来之后,**像跟人说话一样交代一个长作业**。它自己会去提交、排时间回来看。

### 例子(我们真实在跑的那个)

```
帮我在 <主机IP> 上微调一版 embedding 模型,看 NFCorpus 的 nDCG@10。先按这个配置跑:

{"lr": 2e-5, "queries_per_step": 16, "negatives": 3, "max_len": 256, "warmup_steps": 20,
 "grad_checkpointing": true, "param_dtype": "float32", "temperature": 0.05, "epochs": 8,
 "seed": 20260804, "eval_every": 200, "log_every": 20, "keep_recent_checkpoints": 3}

不微调的分数是 0.3674,低于这个就等于白训。总共给你 90 GPU 分钟,用完为止。
机器上只有一张卡能用,同一时间只跑一个作业。训练脚本和评测脚本都别改。
你自己盯着,完事跟我说结论 —— 拿不定主意可以问我,但我可能很久不回,别一直等我。

campaign="试试看",ledger="~/.raven/ops/试试看/ledger.json",host <主机IP>
```

**贴完就可以关掉注意力了。** 它会:提交作业 → 自己排一个唤醒 → 到点自己醒来看进度 →
决定继续等 / 改配置重投 / 掐掉 → 最后交一份报告。

### ⚠️ 坑 3:campaign 名不用写进你的话里,但第一次要指名一次

标识**不需要**出现在你交办的那段话里。窗口第一次调 ops 工具时,如果这个实例下
有多个还没结题的 campaign,它会拿到一份候选清单(带各自的主机、算例路径、预算),
**指名一次之后这个窗口就一直是它了**,后面所有调用都不用再提。

```
2 live campaigns here; this window has not said which one it is watching:
  dambreak-legA    host 14.103.100.27   staged case /home/cfd/work/case_legA    150 core-minute
  dambreak-legB2   host 14.103.100.27   staged case /home/cfd/work/case_legB2   150 core-minute
Call any ops tool once with campaign='<name>'; this window then stays on it.
```

清单里带算例路径,是为了让它能和你交办的那段话对上(你那段话里通常写着算例在哪)。

**窗口关掉再开一个**:把同一段话再贴一次就会自动接手那个 campaign,不用指名 ——
每个 campaign 记着自己是为哪段话开的。已经有活窗口守着的那个不会被接手。

**仍然缺的**:没有任何工具能主动列出现有 campaign,候选名只在上面那种场合才出现。

### ⚠️ 坑 4:远端连接信息要预先放好,不能等它问

它**没有办法知道 SSH 端口**。如果只给 IP,它会挨个猜端口(22 / 2222 / 443 …),
猜不到就来问你 —— 我们实测过一次,它花了 17 个回合然后放弃。

**做法**:开跑前手写一份 campaign 的 `meta.json`:

```bash
mkdir -p ~/.raven/ops/试试看
cat > ~/.raven/ops/试试看/meta.json <<'EOF'
{
  "backend": "process",
  "host": "<主机IP>",
  "port": 58717,
  "key": "~/.ssh/id_rsa",
  "remote_dir": "/远端/工作目录",
  "command": "python3 /远端/训练脚本.py --config {config} --run-dir {job_dir}",
  "budget_minutes_total": 90,
  "interruption_contract": { "min_expected_loss_ms": 1800000 },
  "reference_values": { "ndcg": 0.3674 },
  "expected_baseline": { "ndcg": 0.3674 }
}
EOF
```

**`{config}` 和 `{job_dir}` 是占位符**,harness 会替换成每次试验自己的路径。

> ⚠️ **这个文件缺了不会报错,后果全是静默的**:没有 `backend` 就走 docker 后端;
> 没有 `budget_minutes_total` 算力预算完全不强制;没有 `interruption_contract` 打扰不再被拦。
> **而作业照样会跑完 90 分钟。** 我们差点因此白跑一轮。

---

### 作业脚本该多做的一件事:报一份"我实际用了什么"

写作业脚本的时候,在真正开跑之前,把**实际生效的设置**读回来,写成
`config.effective.json` 放在作业目录里(和 `config.json` 并排)。

```json
{
  "deltaT": "0.0001", "adjustTimeStep": "no", "maxCo": "0.5",
  "ignored": ["maxCo", "maxAlphaCo"],
  "ignored_reason": "adjustTimeStep=no under a fixed deltaT; the Courant ceilings take no part"
}
```

有了它,`ops_tune_status` 会在每个试验下面印出三种差异,而 harness **不解释任何键的含义**:

| 情形 | 会印什么 |
|---|---|
| 提交了某个键,实际值不同 | `max_len: submitted 256, the run used 512` |
| 提交了某个键,实际根本没有它 | `epochs=4 was submitted but the run does not have it` |
| **没提交,而实际值和 campaign 声明的起点不同** | `eval_data was not submitted; this campaign declares '…/nfcorpus_dev', the run used '…/nfcorpus'` |
| 作业自报某些键失效 | `the job reports these took no part: maxCo, maxAlphaCo (…)` |

**为什么值得写这十行**,三次实测:

- 一个提交漏了 `eval_data`,脚本用自己的默认填上 → **在另一个数据集上打分**,
  再拿去和原数据集的基线比。同一次漏写还关掉了梯度检查点(**一个作业吃 78 GiB 直接 OOM**)
- CFD 一次提交同时给了 `deltaT` 和 `maxCo=0.5`,而固定步长会让 Courant 上限**完全失效**。
  agent 以为自己把 Courant 压在 0.5,报了结论;实测全程最大 2.81,716/10000 步越限
- 这两种都**不会让作业报错**,产物看上去正常,数字看上去正常

`ignored` 那一项只有作业自己知道("固定步长下 maxCo 是死的"是求解器语义),
所以由它说,harness 原样转述。**不写这个文件不会报错,只是少一层保护** ——
而这两次事故,少的正是这一层。

配置不是扁平键值的(比如整棵算例目录),走另一条通道:`meta.staged_case` 指向它,
harness 对整棵树取指纹,变了会在提交回执里逐文件列出来。粒度粗一些,但不会漏。

---

## 3. 它有哪些工具(你会在 TUI 里看到它调这些)

| 工具 | 干什么 |
|---|---|
| `ops_submit` | 提交一轮试验 + **自己排一个唤醒**(`eta_seconds` = 下次什么时候看) |
| `ops_tune_status` | 读台账和进度。会印出算力预算总额、campaign 记录的起始值,以及 **loss 和主指标两条时间序列**(按记录顺序,跨全程等间隔取样) |
| `ops_check_later` | 什么都不提交,只是再排一次唤醒("继续等") |
| `ops_kill` | 掐掉在跑的试验 |
| `ops_finish` | 收尾:交最终报告并同时结束整个 campaign(**报告会被校验,见下**) |
| `ops_ask_owner` | 找人。**这是唯一合法的找人途径**,走 campaign 的打扰契约 |
| `ops_note` | 你中途给它留话 |

### 会拒收的闸门(这条分支的重点)

**都是"保证放在机制里,而不是写在提示词里"**:

| 闸门 | 拒什么 |
|---|---|
| **起始值必须是真的** | 报告里的起始值填 `0` 或空 → 拒;和 campaign 记录的值不一致 → 拒。**填一个字段但不核真伪,比不要求更糟** —— 我们实测过它被逼着改了两次才交出正确的数 |
| **决定必须有依据** | `ops_check_later` / `ops_kill` / round≥1 的 `ops_submit` 都必填 `basis`,而且**必须引一个本回合刚观察到的数**。引半小时前那个数 → 拒 |
| **状态声明要对得上** | 报告里说"只保留了 3 个 checkpoint"而实际 5 个 → 拒 |
| **round 0 跑声明的起点** | campaign 声明了 `seed_config`,round 0 提交的配置就必须逐键等于它 —— 少一个键、改一个值、多一个键都拒,并逐条说差在哪。**从 round 1 起不再管**(那时已经有读数了,配置归 agent)。交班场景(账本里已有记录)也不管 |
| **`meta.json` 动过就拒** | 它是 campaign 自己的声明(目标、预算、哪台主机、哪个算例)。改它在任何域都是越界,而且伤害不止于判分:改了 `remote_dir`,作业就跑在没人看的地方。算例改动**不拒**,只逐文件报出来 —— 改算例是干活 |
| **名字被占就拒** | 新任务撞上一个已结题 campaign 的名字 → 拒,并点明是**名字**被占、什么时候结的、哪两个文件不许动。旧措辞说的是"被用户终结了",读起来像权限问题,而那正是它去删 `concluded.json` 的诱因 |

⚠️ 被拒收的报告**不会写盘、不消耗 dedupe key**,所以它可以改完重发。
**看最终报告会以为它一次就报对了 —— 真相在退稿次数里。**

---

## 4. 想看它到底做了什么

```bash
# 事件流:提交、排唤醒、醒来、观察到终态、报告被拒/被接受
cat ~/.raven/ops/<campaign>/events.jsonl | python3 -m json.tool --json-lines 2>/dev/null \
  || cat ~/.raven/ops/<campaign>/events.jsonl

# 台账:每个试验的状态和结果
cat ~/.raven/ops/<campaign>/ledger.json

# 工具层探测到的事实(模型改不了这个文件,闸门用它核对声明)
cat ~/.raven/ops/<campaign>/state_facts.json

# 它引用过哪些读数(闸门用它判断"依据是不是本回合刚看到的")
cat ~/.raven/ops/<campaign>/decisions.json

# 最终报告 —— ⚠️ 这个文件要等报告被接受之后才出现,跑动中看不到是正常的
cat ~/.raven/ops/<campaign>/reports.jsonl

# 它每一步调了什么工具
grep "Tool call:" ~/.raven/logs/tui.log | tail -40
```

`--config` 换了位置的话,上面所有 `~/.raven/` 都要换成那份配置所在的目录。

---

## 5. 已知的粗糙处(先说,免得当成 bug)

| | |
|---|---|
| 它常常先自己 `ssh` 而不是用工具 | `ExecTool` 没有目录/主机限制,所以"自己上手"是一条随时可走的路。工具描述第一句就写着"不要这么做",它照样会走 |
| 没有工具能主动列出现有 campaign | 见坑 3。指名一次即可长期归本窗口,但「有哪些」只在候选清单里露面 |
| SSH 端口靠 `meta.json` | 见坑 4 |
| `workspace` 不跟 `--config` 走 | 见坑 2。`traces` 2026-08-13 起跟了 |
| **loss 序列印出来了,但读不出趋势** | 逐批 loss 的点噪声跨约 550 倍,而整段均值只漂移约 6 倍 —— **噪声把漂移埋住**。要看出来得做窗口平滑,而平滑是一次归约,属于单独的设计决定。主指标那条序列不受影响(评测点稀疏,趋势可读) |
| 掐掉作业会让已记账算力**少一点** | 上界是一个日志间隔。已披露,暂不修 |
| 它读得到自己的评测装置 | `ReadFileTool` 没有目录限制。**如果你在同一个 `~/.raven/ops/` 下放过历史轮次,它会去读** —— 我们实测发生过。做对照实验请用独立的 `--config` |

---

## 6. 已经测出来的结论

**评测记录不在仓库里**(在 lxt 本机的 `~/workspace/raven-oncall-*.md` /
`raven-round[345]-*.md`),要看找 lxt 拿。下面是能公开说的部分。

任务形状:交一个**明显跑坏**的微调作业(学习率高一档,第 4 分钟起就低于不微调的分数),
给 90 GPU 分钟,看它会不会动手。

| 能力格 | 状态 |
|---|---|
| **守住时间**(到点自己醒、不掉唤醒) | ✅ 跑通,且有机制兜底 |
| **如实上报**(报告数字经得起核) | ✅ 跑通,靠 §3 那三道闸门 |
| **看到问题之后动手** | ❌ **至今没有测到过一次成功** |

最后那格的实测形状(2026-08-05 那轮):

- 它**主动查了**进度,拿到了低于基线的评测分
- 它**说得出**基线是 0.3674,并明确写"低于基线"
- 它引的依据**经得起闸门核**(引的是本回合刚看到的真实读数)
- **然后它在还剩 69 分钟预算时,把下次复查排到了只剩 9 分钟的位置**

> **所以瓶颈不是信息不够,也不是工具难用。** 这条线上做的改动
> (工具描述、必填依据、印出起始值、复查间隔语义)都是为了把"没看到 / 不好操作"
> 这类借口逐个拿掉 —— 拿掉之后行为仍然是等。**这就是目前的真实位置。**
