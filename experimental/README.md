# experimental：数字员工的 Worker RSI

这个目录实验一件事：让 Curator 通过多轮交互，把一个 Raven 数字员工的 Harness 改造到能满足交互方提出的核心要求，像培养一个新员工那样。

- **按资料定制**：交互方交出的资料（流程、规范、模板）被 Curator 落到 Harness 的四个策略面上，员工从第一轮起就按这些规矩工作。
- **按反馈改进**：员工工作后，交互方说出哪里不对，Curator 修订 Harness，下一轮再检验。

通用机制（Curator、Analyst、Iteration）不认识任何具体场景；`simulation/` 用一个模拟旅行社把这套机制跑起来，并留下一个展示案例。

整个目录对 Raven 源码零侵入：`raven/` 没有为它改动，也不引用它。Curator 生成的东西落在 Raven 本来就开放的扩展点上（home 文件、配置、钩子、插件贡献、ACP 子代理），由真实的 Raven AgentLoop 执行；连接方式和依赖的少量内部成员见 [design.md 第 5 节](docs/design.md#5-与-raven-的连接)。

## 目录

```text
experimental/
├── README.md
├── requirements.py              # 行为要求 Requirement：Analyst、模拟评审与 playbook 节点要求共用
├── docs/
│   ├── design.md                # Curator 怎样生成与管理 harness-of-harnesses
│   └── rsi-iteration.md         # Harness 怎样在多轮反馈中迭代改进
│
├── curator/                     # 【通用】读取、生成、校验、安装 Harness
│   ├── workflow.py              #   入口 improve / propose
│   ├── composition/             #   两层组合：根候选 → 各子候选 → 整体校验与一次生效
│   ├── generation/              #   分阶段生成
│   │   ├── stages/              #     understand → select → design → implement → repair
│   │   ├── prompts/             #     各阶段提示词
│   │   └── context/             #     生成材料的组装与只读查询
│   ├── harness/                 #   产物模型、target 声明、四个公共策略协议与参考材料
│   └── raven_adapter/           #   与 Raven 运行时的连接层
│       ├── targets/             #     四个策略面在 Raven 上的扩展点声明
│       ├── bind.py, strategy.py #     把生成的策略与组件绑定到原生扩展点
│       ├── worker.py            #     被培养的 worker：装载、执行、安装
│       ├── deployment.py, hosting/  # 子 Harness 的部署与 ACP 托管
│       ├── inspection/          #     从实际运行时读出当前 Harness
│       ├── observe.py           #     机制每次决定写成观测记录
│       ├── exploration.py       #     生成期间 Curator 的只读探索空间
│       ├── planning/, action/, capability/, memory/  # 各策略面的运行时绑定
│       ├── baselines/           #     专家与朴素 Raven 基线的准备
│       └── reference/           #     给 Curator 读的宿主参考材料
│
├── analyst/                     # 【通用】把评价者的话整理成 Curator 的行为要求
│   ├── run.py                   #   一次有界的模型交换，产出 Feedback
│   ├── feedback.py              #   Feedback：决定 + 行为要求
│   ├── materials.py             #   材料组装与 read_records 查询
│   ├── activity.py              #   机制活动：装上的机制本轮实际做了什么
│   ├── role.py                  #   Analyst 基类，场景可继承
│   └── prompts/
│
├── iteration/                   # 【通用】多轮闭环
│   ├── run.py                   #   入职 curation → 每轮：试炼 → 评价 → 分析 → 修订
│   ├── protocols.py             #   Trial / Evaluator 协议与 Signal
│   ├── conversation.py          #   对话型 Trial
│   ├── dataset.py               #   数据集型 Trial 兼 Evaluator
│   ├── exchange.py              #   各模型角色共用的有界结构化提交
│   ├── human.py, __main__.py    #   真人 CLI：真人既对话又评价
│   └── records.py               #   按轮读取一次运行的记录
│
└── simulation/                  # 【实例】旅行社场景
    ├── __main__.py, suite.py    #   单次运行与批量挖掘入口
    ├── scenarios/travel_agency/ #   场景数据：岗位说明、资料、评审标准、客人画像
    ├── employee.py              #   数字员工：每张题卡一个隔离副本
    ├── traveller.py, cards.py   #   模拟客人与每轮重新抽取数值的题卡
    ├── agency.py, prompts/      #   模拟老板：演练后评审，决定交出哪些资料
    ├── channel.py, files.py, render.py, reference.py  # 客人实际看到的内容、交付物读取与页图、价目计算器
    ├── record.py                #   把一次运行导出成培养记录 transcript
    ├── value.py, attribution.py, isolation.py  # 价值判定、机制归因、越界检查
    ├── caching.py               #   网关模型的提示词缓存断点
    └── cases/s0925c/            #   展示案例
```

## 通用机制与场景的关系

依赖方向固定：`simulation/` 引用通用层，通用层不引用 `simulation/`。

| 通用接口 | 旅行社场景的实现 |
|---|---|
| `iteration.run.run(worker, provider, trials, analyst, opening=…)` | `simulation/__main__.py` 组装参数，默认最多 4 轮 |
| `Trial.run(worker) -> Sessions` | 通用的 `iteration.conversation.Conversation` 配模拟客人 `Traveller`；`employee.Fresh` 让每次演练从干净的子代理 home 开始，`Together` 让每张题卡在员工的隔离副本上并行演练 |
| `analyst.role.Analyst.review(…)` | `agency.Agency`：老板说出评审意见；默认把原话交给通用 Analyst 整理成要求 |
| `curator.raven_adapter.worker.Worker` | `employee.hire` 雇来的员工，带上 Curator 不能读的评测侧文件（`withheld`）；`employee.Replica` 是每张题卡的隔离副本 |
| `curator.workflow.improve(worker, provider, feedback=…)` | 不改，直接调用 |
| 入职资料 `opening` | 老板的入职对话与交出的资料 |

## 运行

```bash
# 旅行社模拟：一次运行（需要自备带 key 的配置文件）
uv run python -m experimental.simulation --config <config.json> --home <基线 home> \
  --state-dir <新的空目录> --scenario travel_agency --chain documents --rounds 4

# 真人 CLI：真人既是对话方也是评价者
uv run python -m experimental.iteration --help

# 测试
uv run pytest tests/test_harness_curator_*.py tests/test_analyst_*.py tests/test_iteration_*.py tests/test_simulation_*.py
```

## 展示案例

[`simulation/cases/s0925c/`](simulation/cases/s0925c/) 是一次自动运行的完整培养记录：老板只交资料、扮客人演练、说意见，Curator 在入职时凭资料生成了流程与关口，之后按意见逐轮补齐，第 3 轮全部红线通过。目录里有案例说明、系统自动导出的 transcript，以及第 1 轮和第 3 轮的方案 PPT。
