# PPT-Raven 架构设计

本文件是 PPT 能力的**设计权威**：分层、边界、决策与阶段规划。当前能力与验证证据写入
`docs/ppt-raven-status.md`；长效开发规则写入 `raven/ppt/AGENTS.md`。三份文件不重复彼此的内容。

本次重构的起点是 `upstream/main`（EverMind-AI/Raven `5a0c950`）。上游不含任何 PPT 代码，
也不含 `raven/rendering/`，因此 PPT 能力以一个独立顶层包 `raven/ppt/` 的形式落地，
对上游的改动收敛到一处装配点。

---

## 1. 为什么要重构

旧形态（fork 分支 `feat/ppt_free_composition`）的问题不是某个模块写坏了，而是三件互相
独立的事被压成了一个布尔量：

| 本应独立的轴 | 它回答什么 | 旧代码怎么表达 |
| --- | --- | --- |
| **路线** | 模型要走哪几个阶段 | `freeCompositionEnabled: bool` |
| **后端** | 一页怎么变成 DrawingML | 隐含在路线里，没有名字 |
| **权限** | 模型被允许写出什么 | 散在 prompt 散文 + 各处门禁代码里 |

后果是可度量的：`raven/agent/tools/ppt.py` 一个文件 8129 行装了 16 个工具，
装配函数 `build_ppt_tools()` 里有十处 `if not free_composition` 分支；两条路线各自实现了
一套测量（`text_fit.py` 量声明几何，`page_metrics.py` 量渲染真值），共享关注点
（图注门禁、返图、发布、图像证据）在两条路线里各写一遍。再加第三条路线，分支数是乘法关系。

所以重构的目标不是"整理文件"，而是**把这三根轴拆开**，让新增一条路线等于新增一份声明
加几个阶段，而不是在既有代码里再插一层条件。

---

## 2. 分层与依赖方向

```
raven/ppt/
  contracts/     纯数据：项目状态、outline IR、页 IR、finding。零逻辑
  services/      无状态能力：ingest / assets / measure / gates / render / imagegen / publish
  backends/      把一页变成 pptx：script / slots / imagetext
  stages/        一个阶段一个文件：(Project, StageInput) -> StageOutput + Finding[]
  profiles/      具名的阶段表 + 能力声明 + 门禁分级
  tools/         Tool 适配层：schema、参数适配、结构化返回
  vendor/        上游 ppt-master 快照，只经适配层触达
```

依赖只能自左向右单向流动：

```
contracts ← services ← backends ← stages ← profiles ← tools
```

具体禁令，全部由 `tests/ppt/test_layering.py` 用 AST 静态检查强制，违反即测试失败：

| 层 | 不得 import |
| --- | --- |
| `contracts` | 除 stdlib / pydantic 外的任何东西 |
| `services` | `stages` / `profiles` / `tools` / `raven.agent.*` |
| `backends` | `stages` / `profiles` / `tools` |
| `stages` | `tools`；不得直接 import `vendor` |
| 全 `raven/ppt/` | `raven.agent.loop` / `raven.spine` / TUI |

这条规则替代了旧 `CLAUDE.md` 里那张"模块 → 不得包含"的表：表是给人读的约定，静态检查是给
CI 执行的约束。

### 各层职责

| 层 | 唯一职责 | 明确不做 |
| --- | --- | --- |
| `contracts` | 定义模块之间传递的数据形状 | 任何校验之外的逻辑、任何 IO |
| `services` | 可复用的无状态能力 | 不知道有"路线"这回事、不发起 LLM 调用、不持有 workspace 状态 |
| `backends` | 页 IR / 脚本 → `.pptx` | 不做门禁判断、不决定阶段顺序 |
| `stages` | 一个阶段的编排；LLM 调用只在这里发生 | 不实现测量/布局算法本体 |
| `profiles` | 声明一条路线由哪些阶段、哪个后端、什么权限组成 | 不含任何算法 |
| `tools` | schema、参数适配、结构化返回、返图 | 不含算法与状态机本体 |

---

## 3. 三根轴

### 3.1 路线 = 阶段表

一个阶段是一个纯函数式的编排单元：

```python
class Stage(Protocol):
    name: str
    async def run(self, project: Project, payload: StageInput) -> StageOutput: ...
```

`StageOutput` 固定携带 `findings: list[Finding]`，所以"这一步发现了什么问题"在全系统只有
一种表达方式。旧代码里 `content_load` / `page_defects` / `undersized_type` /
`fact_violations` / `colour_bars` / `pages_not_in_build_py` 是六种形状不同的返回，
合并为一个 `Finding`：

```python
@dataclass(frozen=True)
class Finding:
    kind: str            # "citation" | "band" | "density" | "type_floor" | ...
    severity: Severity   # BLOCKING | WARNING
    page: int | None
    message: str         # 给模型读的一句话，自带修法
    detail: dict         # 结构化证据
```

曾经还有一个 `audience: AUTHOR | DESIGNER` 字段，用来把内容问题挡在精修环之外。精修环已删
（D18），只剩一个受众就不是受众，字段随之取消——每一条 finding 都是作者的。

### 3.2 后端 = 一页怎么落地

```python
class Backend(Protocol):
    name: str
    async def compose(self, project: Project, plan: DeckPlan) -> BuildOutcome: ...
```

| 后端 | 输入 | 实现 |
| --- | --- | --- |
| `script` | 作者写的 python-pptx 程序 | 子进程跑脚本，记录每页由哪段代码画出 |
| `slots` | 槽位布局 IR | 槽位 → SVG(ppt-master 方言) → DrawingML，经 vendor |
| `imagetext` | 背景图 + 文本区域 | 背景图铺底，文字按安全区排布 |

三者产出同一个 `BuildOutcome`（pptx 路径 + 页数 + 每页来源映射 + stdout/stderr），
所以 `measure` / `gates` / `publish` 对后端一无所知。

### 3.3 权限 = 能力声明

硬不变量"模型不写物理量"过去只存在于 prompt 散文和评审习惯里。现在它是一份声明，
门禁与工具 schema 都读它：

```python
@dataclass(frozen=True)
class Capabilities:
    raw_script: bool = False          # 可提交 python-pptx 程序
    normalized_regions: bool = False  # 可给归一化画布坐标(0..1000 网格 / 矢量画布)
    background_prompt: bool = False   # 可写背景生图提示词
    physical_geometry: bool = False    # 物理坐标/尺寸 —— 任何 profile 下恒为 False
    font_size: bool = False           # 字号 —— 任何 profile 下恒为 False
```

`tests/ppt/test_profiles.py` 遍历注册表断言后两项恒 False。硬不变量从"文档要求"
变成"测试证明"。

---

## 4. 三条路线

| | A `script_author` | B `slot_author` | C `image_text` |
| --- | --- | --- | --- |
| 适用 | 强主模型 | 弱模型 / 低成本 | 设计驱动的版式 |
| 阶段 | prepare → (brief / template / gather / generate / ingest / inspect 皆可选) → plan → build → publish | brief → ingest → inspect → plan → fill → compile → review → publish | brief → ingest → plan → background → place_text → build → publish |
| 后端 | `script` | `slots` | `imagetext` |
| 能力 | `raw_script` | `normalized_regions` | `background_prompt` + `normalized_regions` |
| 复用 | ingest / measure / gates / render / publish 完全共享 | 同左 | 同左 |

路线 A 的作者工作区在 `ppt_prepare` 返回前就会预置 `ppt_layout.py`、`ppt_charts.py`、
`ppt_icons.py`、`ppt_theme.py`、`ppt_shapes.py` 及其数据文件；模板绑定后会刷新
`ppt_template.py` 和模板主题数据。
`ppt_build` 仍会重复投影这些文件作为兜底，但作者第一次写 `build.py` 时就必须能导入
这些模块，不能把它们延迟到第一次构建之后才出现。

路线 A 的图片取件并行走两条已有素材路径：`web_fetch(extractMode="images")` 从研究报告
已经引用的页面采集图像候选，`web_search(kind="images")` 主动补充搜索；候选经
`ppt_fetch` 进入项目 sources、立即 ingest，再由 `ppt_figure_inspect` 看图后选择。
两条路径都没有合适视觉时，才用 `ppt_generate_image` 调 GPT Image 2；生成图同样进入
sources 和 figure catalogue，但只作为概念示意，不替代产品截图、论文图表等证据。

路线 C 的新代码只有三块：`services/imagegen`（接口 + 一个适配器，本地模型与 API 同一接口）、
`stages/background`（把一页的意图变成生图提示词 + 文本安全区契约）、
`stages/place_text`（把文字放进安全区）。测量、门禁、渲染、发布一行不动 ——
`measure` 本来就从渲染后的 PDF 里读词级 bbox，这正是"验证生成背景上的文字有没有压住内容"
所需要的手段。

---

## 5. 已确立的决策

以下决策来自旧形态上的实测教训，重构后继续有效。

**D1 保证在代码里，不在 prompt 里。** 页数、语言、图注引用、页与代码的对应、渲染实测的
文字重叠与遮挡由工具执行路径强制；prompt 只做引导。（当时这条还写着"字符预算"——**一页的
字符上限从未在这套代码里落地**，`copy_density` / `MAX_CHARS_PER_PAGE` 全仓不存在，一页多不多
只由测量渲染结果决定。见 D7 的后继。）

**D2 测量结果回灌设计环，不硬拒——渲染重叠除外。** 字号下限、越界、冲卡、标签折行以 WARNING
finding 进下一轮设计调用；硬拒会与文本适配形成"缩回—再缩回"振荡，终点是没有交付物。
（**"设计环"这个收件人已经没有了**，D18：回灌的落点现在是 `ppt_build` 的返回——渲染图与页面
测量一起交回写程序的作者，由它的下一次构建消化。机制与理由一字不变，只是环从两个阶段之间
收进了一个阶段里面。）

**渲染实测的文字重叠（word_collision）是这条的例外，改为 BLOCKING。** 三轮真实运行都交付了
"字压在字上"的 deck，回灌没有收敛。它不是口味问题，也不能靠缩字号解决——字号下限同样被测量，
缩下去只是把一条 finding 换成另一条；它真正要的动作是把文本框画宽，而画宽不花任何代价。

**D3 fail-closed 发布。** 存在 BLOCKING finding 时拒绝发布。发布的拒绝判据是
`severity is BLOCKING or kind in profile.blocking_kinds` 两条并集，所以严重级有两个声明处，
由 `test_no_route_refuses_a_deck_over_something_the_gate_only_reports` 强制它们不许互相矛盾。
路线 A 当前的拒绝集合是 13 个 kind：

- **来自 profile**（`_PROVENANCE | _AGREED | {unmapped_page, house_style}`）：`citation`、
  `page_budget`、`language`、`unplaced_figure`、`unmapped_page`、`house_style`。
- **来自门禁自己的 severity**（`gates/registry.py` 的 `DISPATCH`）：另加 `word_collision`、
  `covered_shape`、`literal_escape`、`unreadable`、`placeholder_copy`、`template_underlay`。
- **构建阶段自己建的**：`unseen_page`（`stages/build.py`）。

**色带（`band`）不在里面**：它已降为 WARNING，见 D17。

**D3a 数字不做字面门禁。** 曾有一道 `fact` 门禁：页面上的每个数字必须逐字出现在素材正文里，
否则 BLOCKING 拒绝发布。七次真实运行、932 条 finding，它一共报了 10 条，**全部是误报**——
9 条的数字在素材配图里（模型读得一字不差），1 条是模型自己做的一次正确减法。它设计要防的
"凭空编数字"一次都没出现，而误报的代价是硬阻断：一次运行因它把整轮大纲重做。
判据是字面匹配，于是三种正常行为全被误判——引用图里的数、做一次算术、换单位或改舍入。
删除，不降级为 warning：一个 100% 误报的检查降级只是把硬伤变成噪音。素材体量（`stated_chars`，
用于"素材字数撑不起页数"的提醒）与之无关，保留在 `ingest/sections.py`。

**D3b 两条 blocking 降为 warning，页脚不受字号下限约束。** 逐条核对七次运行的 932 条 finding
后的结果。`page_mapping`（每页能否对应 build.py 的**一个自己的**代码块）保护的是把渲染图
对回画它那段代码的能力，不是读者看得见的东西；`prototype_kept`（页面是否建在大纲承诺的
模板页上）比的是页面与计划，一次运行自己画的四卡布局比承诺的模板页更好看，硬拒等于要一个
更差的页面，而且它给的出路"或者改大纲"改一行就能绕过。两者都改为报告。

**降的只是 `page_mapping`，`unmapped_page` 仍然硬拒。** 两者同题材、不同 kind，可以同时
出现：`unmapped_page`（`stages/build.py`，BLOCKING，且列在 profile 的 `blocking_kinds` 里）
说的是 build.py 里**根本没有逐页的块**——一张渲染图对不回任何代码，流水线对这份 deck 无法
推理，那不是审美判断。降为 warning 的只有"块在、但分不出哪块画哪页"这一档。

字号下限对**页脚**另立 `FOOTER_FLOOR_PT = 8.0`：页脚是元信息，没人从座位上读它。原先 10.8pt
的通用下限让每页的出处行都挨一条 finding——run44a 的 210 条 `type_floor` 去重后只有两行，
"Source: …" 与 "TarViS · CVPR 2023 · arXiv:2301.02657"，都是完全正常的 9.5pt；同一份产物
18 个页脚旧规则全报、新规则一条不报。`type_floor` 曾占全部 finding 的 30%。

同一轮核对的结论是**其余门禁不动**：13 个 blocking 里 7 个从未在真实运行中触发，但它们是零成本的
防御下限（deck 语言写错、页数不符、页面上打印出 `\n`、图注标 Fig.4 却显示 Fig.5），纯代码、
不进上下文，阈值都有实测依据（`MAX_CHARS_PER_PAGE = 700` 来自"评审接受的最密页 467 字符"）。
触发为 0 是阈值定得准，不是多余。（这段原先举 `MAX_CHARS_PER_PAGE = 700` 当例子——
**那个常量不存在**，字符上限没有落地过；留在代码里、真有实测出处的阈值是
`gates/brief.py` 的 `CHARS_PER_PAGE = 80` 与 `COPY_PER_PAGE = 250`，两者都只报警告。）

**D3c 模板底色以渲染为准，并把选择权交回模型。** 调色板原先只读文件的声明——主题的
`clrScheme` 经 master 的 `clrMap` 解析。拿 119 个真实模板逐个渲染对拍，**声明的底色只有
61% 与页面实际一致**，其中 11 个是方向性错误（声明白、实际蓝底；声明黑、实际白底），
因为消费级模板的视觉底色往往画在 layout 的形状上，主题里那套从没被用过。

先后试过两层声明都不够：master 的 `<p:bg>` 覆盖 `clrMap`（修好一类，但撞上
`alpha val="5000"` 这种变换，限制为"只认干净 solidFill"后仅覆盖 11 个模板）。
最终以渲染为准：绑定模板时本就会渲染示例页（`tools/template.py` 已为作者和字号阶梯读过
两次），再读一次取内容页主色，写入模板目录的 `ground.txt`。优先级
`rendered_ground > painted_ground > clrMap`。**命中率 61% → 93%**，方向性错误 11 → 5。

剩下的 5 个不再追：它们的内容页底色本身就是混杂的（深绿边框配白色内容区、浅底与棕底交替），
没有唯一正确答案，继续调阈值只是拟合个案。取而代之的是把决定权交回模型——`ppt_template`
的回复里现在带 `palette`：当前色值、**它是从渲染测的还是从文件读的**、模板还持有哪些槽位色，
以及一句明确授权：看着上面的渲染图，若与命名的底色不符，就在程序里 `T['background'] = ...`
覆盖，由它派生的平面与次要色会跟着走。模型能看见模板，这件事上它比任何像素统计都可靠。

**D4 降级显式留痕。** 文本溢出降级链每步留痕，禁止静默截断半句。

**D5 vendor 隔离。** 只经适配层触达 vendor，固定上游 commit，改动以补丁记录。

**D6 空提交即"跑现有程序"。** 工具收到无程序内容的提交时保留原文件并构建它，而不是拒绝。
拒绝虽然保住了文件，但把作者推出工具去用 `exec` 自行交付，绕过全部门禁。
（实测：一次运行连续三次提交 `" "` / `"\n"` / `"# use existing build.py"` 被拒后离开工具。）

**D7 内容密度归作者，精修不动内容。** 密度只设上限不设下限。
（**两头都反了，见 D19 与 D18。** 上限：`copy_density` / `MAX_CHARS_PER_PAGE` 全仓不存在，
一页密不密只由测量渲染结果决定，`_thin_pages` 的注释自己写着 "the built page has no floor
either"。下限：现在恰恰有三处下限类 warning——`thin_material`（素材字数撑不起谈定的页数，
`CHARS_PER_PAGE = 80`）、`thin_page`（一条 `says`、无 figure、无 errand、无 prototype 的计划）、
`excessive_whitespace`（渲染出来大片空白的内容页）。"精修不动内容"随精修阶段一起没了，D18。）

**D8 草稿归作者迭代，精修只做定稿前的最后一眼；精修跳过必须留痕。**（已被 D18 取代：精修环整个删除。）
精修的运行条件曾两次挂在 finding 上，两次都错：先是"存在任何 BLOCKING 就跳过"，于是唯一负责版式的
阶段恰好在版式出问题时不跑（两轮真实运行因一条本身判错的色带门禁被拒，整轮每一次构建精修都没跑）；
改成"非 DESIGNER 的 BLOCKING 才跳过"是同一个错误退一步——一份 deck 有 29 条未替换占位符（作者的活）
和 25 对文字重叠（精修的活），后者就排在前者后面等着。三轮完整运行里精修执行 **0 次**，而返回里
一个字都没提，缺席读起来就像"版式没问题"。

正确的分工不是两份问题清单排队，而是两种能力：作者拥有整个程序，能改一页说什么、框在哪、这页是否
存在；精修只能重排。所以 **deck 还是 draft 时，全部测量（含 DESIGNER）回给作者迭代**；正式构建时
精修必跑，做最后一眼。让只能重排的一方去修半成品程序的错，是让弱的工具干强的工具的活。

精修未运行时，返回里 `design_pass.did_not_run` 说明原因；若同时存在 DESIGNER findings，
asks 第一条明确"`for_the_design_pass` 这轮也是你的活"——实测有模型把这个桶名读成"别人会处理"，
连续八次构建都没动那 25 对重叠。

**D9 模板出「框架 + 风格」，内容页由模型自己构图。**（**已被 D19 取代**：默认反过来了——
现在每个内容页都要挑一张示例页当原型，自由构图是那条挑不出来的退路。下面这一段记的是当时
为什么反过来的第一遍，它量到的东西仍然成立，只是结论换了。）
把模板的内容页当原型来填，三轮真实运行给出了它失败的全部理由，且都是可测量的：
模板槽位是按它自带的示例短语（"单击添加小标题"）画的，塞进一句真话就靠 `normAutofit`
自己缩字——同一个 18pt 槽在一页里渲染成 11.7 / 13.7 / 13.5 / 10.8pt，封底一页把五行
正文缩到 2.01pt；六卡网格填四条内容就留一个洞；竖版图框放不进横版架构图（模型连续三次
只换 shape 序号，整轮没出 deck）；而技术评审要展示的表格与数字，本来就不是任何营销模板
的形状。

所以模板现在借出两样不同的东西：

- **封面、目录、章节页、封底**：克隆。这四页是读者认出"这是谁的模板"的地方，且实测
  13 页里有 9 页含 python-pptx 写不出来的图元（自定义几何、渐变、15% 不透明填充），
  重画只会更差。`ppt_template` 只渲染并只交出这四页的源码。
- **其余每一页**：自己构图，但必须落在从模板量出来的 house style 里——背景所在的
  layout、标题行的位置与字号（本模板：10 个内容页里 7 页一致）、master 声明的字号阶梯、
  示例页实际用的字体、模板自己守的安全区。大纲拒绝内容页声明 `prototype`。

字号阶梯取自 master 的 `titleStyle` / `bodyStyle`，不取自示例页：示例页自带的填充文字
比槽位长，读出来的"正文字号"是 8pt、"标题字号"是 24pt，而渲染出来的标题是 28pt。

`template_adherence` 随之换了问题。它原来数"有多少页建在模板自己的页上"，于是 11 页里
克隆 4 页读作"4/11，再多克隆些"；现在只问这份 deck 是否在模板自己的封面/目录/章节/封底
里开场、索引、分节、收尾，中间的页一句不问。

**D10 字号按读者看到的量，不按文件声明的量。** 模板文本框普遍带 `<a:normAutofit/>` 且
不声明字号，文件里什么都没写，渲染器自己决定——于是旧的字号普查跳过了每一个这样的 run，
上面那三页（11.7–10.8pt、2.01pt）全部报"干净"。现在字号从渲染结果读，文件声明的数字并列
带上：两者之差就是 finding 的解释（没人选了 10.8pt，是装不下的框选的）。

同一份测量顺带回答了另一半抱怨——不是"这页字小"，而是"这几页不一致"。`type_drift` 把
deck 里重复出现的同一槽位（同样的声明字号、同样的尺寸）归组，报出被渲染器缩到与槽位本身
不同的那些副本。组内的"标准字号"取观测到的最大值而不是最常见值：autofit 只会缩不会放，
按最常见值取会把结论倒过来——在产生这条规则的那一页上，它把四个被缩小的正文认成标准、
把两个保持原尺寸的标题认成异常。

**D11 遮挡是重叠的另一半，且只有 z-order 能判。** `word_collision` 读渲染图，报"字压在
字上"；被盖住的内容它看不见——图在不透明卡片下面时，没有任何东西压在任何东西上，一个词都
不挨着另一个词，而这页引用了它并不展示的证据。判据是 z-order 加面积：一个形状被画在它
之后、且填充不透明的形状盖掉大部分。次序是可判定性的来源，因为"卡片里放文字"同样是两个
框重叠在一起，那是卡片的全部用意——盖在文字上面的才是缺陷。

这里试过并否决的做法：按文件里的框报"两个文本框重叠"。文本框总比里面的文字宽，于是整宽
标题框会和角上的页码框重叠——在一份好 deck 上它报的 9 条里有 8 条正是这个，剩下 2 条真
重叠早已由 `word_collision` 以渲染出的词为准拦下。

**试过并否决：按大纲反查页标题。** 一份交付 deck 的目录页标题错成了它自己第 4 条条目
（"四任务结果：统一是否有效"），于是试了一条判据：把每页标题与全部页的 `claim` 比字符重合度，
若"更像别页的计划"就报。它确实抓到了那一页（自身 0.18、对第 12 页 0.55），但三次命中里两次是
取标题取错——目录页的列表项落在标题带内、被当成标题。判据本身要先解决"哪个形状是标题"，
而那正是 house style 已经量出来的东西（模板自己的标题行）；等自由构图的页也统一落在那一行之后，
这条可以重做。当前不上：噪声比信号大。

**D12 渲染看不见的缺陷，由构建收尾机械修正，不进 finding。** 本管线全部按"渲染出来再量"
判 deck，于是有一类缺陷它结构上看不见：LibreOffice 宽容、PowerPoint 严格的那些。实测两条，
都在每一份已交付 deck 里：

- 页面从带标题占位符的 layout 起页，占位符没被填就留在页上。放映时不显示，编辑视图里是一个
  框加"Click to add title"——而收到 deck 的人打开的正是编辑视图。一份 13 页 deck 里 10 页有，
  全都落在模板标题行的 (0.72, 0.14) 11.88×0.98in。
- `add_table` 给每个表盖上 Office 图库的 "Medium Style 2 - Accent 1"（蓝底斑马），python-pptx
  没有卸载它的 API；而单元格四条边框元素被按 `lnB, lnT, lnR, lnL` 写出，ECMA-376 规定的次序是
  `lnL, lnR, lnT, lnB`。五个表 175 个单元格全错。LibreOffice 两者都不校验，所以每一次渲染、
  每一项测量都说表格是干净的；PowerPoint 校验，丢掉它解析不了的属性，读者看到的就是蓝色斑马表。

判据是它们不是作者的错，也无从修：finding 是"请你改"，而这两条没有可改的东西。所以
`raven/ppt/services/tidy.py` 在每次构建产出前无条件跑一遍，删空占位符、卸图库样式、把边框
按 schema 次序放回，只记 debug 日志。源头也修（`table()` 自己写对次序），收尾是兜底——作者写的
是程序，直接 `add_table` 的页要拿到同样的结果。

护栏不在 tidy 自己的单测，而在 `test_a_built_deck_carries_neither_defect_a_render_cannot_show`：
真跑一份含占位符与表格的程序，断言交付文件里两条都不存在。任何绕过 tidy 的改动（换构建路径、
调整 runner 次序）都会在这里失败，而不是又一次静悄悄地交付出去。

**D13 精修阶段默认关闭，配置项打开。**（已被 D18 取代：默认关闭之后没有一次证明它值得打开，阶段整个删除。）
四次真实运行的实测结论是它让 deck 更差。最后一次：
3420 字里删掉 224 字（一整条要点、一张图的说明、一页的结论），另有三行被悄悄改写，全部是它
自己 brief 第一段明令禁止的；同时给 8 页加上装饰色条，而那正是本 deck 门禁判为色带的东西。
两条机械保证现在都有了（`copy_rejection` 与 band 门禁），但一个价值未被证实的阶段不该是默认，
何况这条路线不开它就已经不错。`tools.ppt.designer.enabled = true` 打开；关闭时回复里明说是
"配置关了"还是"草稿不精修"——静悄悄什么都不做的阶段，和跑过但没发现问题的阶段，读起来一模一样。

**D15 形状的矩形一律按页面坐标读。** python-pptx 返回的是形状在**它所属组**的坐标系里的
数字（`a:chOff`/`a:chExt`），没有任何东西做转换。实测一份真实模板的目录页：六个编号排成
两列三行，全部报在 (7.67, 4.27)、宽 4.46in —— 既不是位置也不是尺寸。**每一个读矩形的检查
对每一个组内形状都是错的**，而模板 75% 的页面由重复 unit（即组）构成。

它一直不可见，因为那些编号原本是空文本，没有检查会去量它；等编号被填回来，`covered_shape`
立刻报出五条 BLOCKING（"'02' 100% 被 22、23、27、28 盖住"），一页本来没问题的目录页无法导出。
`page_box()` 沿祖先组逐层套用标准变换，`shape_rect_emu`/`shape_rect_pt` 走它，band 门禁、
layout 检查、继承装饰检查因此一并修好；`overlap`、`contrast`（裁剪一直取在渲染图的错误位置）、
`type_size`（渲染 span 被归给报在该点的任意框）改为直接调用。

**D16 轨迹里出现盲调，就是缺一个原语。** 判据不是"这个功能好不好"，而是"模型有没有在试"。
逐步读一次运行的 105 步得到三处：表格列宽改了 13 次（缩短表头 + 手写 weights）、band 的 y
坐标试了 5 次、图片等比适配自己实现了一遍还动了私有 API。对应补上 `table()` 按内容定列宽、
`stack()` 接续放置、`picture_fit()`。同一份轨迹里另有四处是**信息缺失**（helper 签名、图标名、
`adapt(items=)` 语义、素材路径与分节），那些补在 skill 与返回值里而不是加原语 —— 两类的区别
是：模型不知道 vs 模型知道但做不到。

三条都补上了：`table()` 按内容定列宽、`stack()` 接续放置、`picture_fit()` 等比适配，都在
`services/assets/layout.py` 里。

**判据第二次应用，指向的不是"缺原语"而是"原语不肯说"。** 一个原语替模型把几何算完、却不说
算出了什么，模型只能对着渲染图猜——那仍然是一次盲调，只是盲的东西从"没有这个能力"变成了
"有能力但答案不外传"。每个 helper 原先只回 python-pptx 对象，作者接下来最需要的那个数
（这东西到哪儿结束）只存在于渲染图里。所以两侧都开口：

- **画之前问**，十一个函数：`layout.py` 的 `text_size` / `points_size` / `table_size` /
  `picture_size` / `lines_needed` / `formula_type_size` / `fits` /
  `the_largest_step_this_copy_takes`，`charts.py` 的 `what_a_chart_will_do` /
  `whether_a_chart_fits` / `the_smallest_box_a_chart_needs`。
  图表那三个是拿真图表对着一块"吞形状"的假 slide 跑一遍，所以答案就是图表自己的算术，
  不是另写一份估算。
- **只有一个函数往上问。** 前七个都是向下量——"我已经挑好的字号放得下吗"，于是没有任何东西
  说得出"这块盒子最多能放多大"，十页实测里字号阶梯用了零次、页面自己造了 10 到 32pt 之间
  十三个字号，而用到阶梯的地方一律取最小一档（1.25in 高的 chevron 里 `LABEL_PT` 的步骤名）。
  `the_largest_step_this_copy_takes` 补的是这个方向，**答案只能是阶梯上的一档，不能是两档
  之间的整数**：两种实现都建了四十个用例的真实渲染，用 `pdftotext -bbox` 量下来阶梯离盒子
  最近一边中位留白 11.6pt、40 例中 2 例压到边外，逐整数搜索是 5.7pt 与 7 例——底下是
  `_em_width` 的估算，`FACE_WIDTH` 记着它每种字面差 5 到 22 个百分点，把最后一磅余量花掉的
  答案在本渲染器上放得下、在读者机器上放不下。阶梯的粗粒度就是这份余量，也是稳定性：盒子按
  百分之一英寸从 0.60 长到 6.00in，答案每条序列只变 0 到 3 次且每次只跳一档，逐整数是 13 到
  16 次。这条不构成硬拒（D2/D6 未变），回到下限只是"盒子该更大"的信号。
- **画之后回读**：每个绘制 helper 返回 `Drawn(shape, box)`，`box` 是它真正占掉的矩形，
  所以下一件东西的位置是 `written.box.y1 + GUTTER` 而不是一个试出来的数；`Stack` 另有
  `.room`；图表的 `Drawn` 还带它让出了什么（没写下的点名、没放下的读数、掉到下限的气泡）
  和值到英寸的换算。`table_size` 与 `table` 走同一个 `_table_geometry`，所以"问到的尺寸
  不是画出来的尺寸"是不可能，而不只是不太可能。

**D14 公式与卡片是原语，不是文档里的条目。** 两者都是几何，而几何归引擎：

- 公式用 `write` 写就是正文，正文在框宽用尽处折行，而用尽处正好是符号中间。交付页实测：
  3.9in 栏里 `掩码 logits = (F4, Q'inst)；分类 logits = ...` 在"分类"后折行，半个子句留在上一行，
  且所有下标是平的（F4 而非 F₄）——记号唯一携带的信息没了。`formula()` 整体不折行、`_x` 与
  `_{xyz}` 下标、`^x` 上标、单字母变量斜体多字母函数名正体，字号沿阶梯下降到装下为止，floor
  还不够就在公式自己的分号处断。
- icon 的情况更直接：两份 13 页 deck 调用 `add_icon` 各 0 次，180 个图标一个没用，而并列要点
  画成了一列一模一样的粗体标题。skill 里"请多用 icon"已经写着了，没用。`card()` 一次调用画出
  底面、icon、标题与正文并拥有那部分几何，于是带 icon 的版本比手画四个矩形更省力——引导要落在
  省力的那条路上，而不是落在措辞上。

**D17 band 门禁改为报告。** 它是 blocking 集合里唯一的审美判断，靠"散文证明拦不住"这一条留在
那里。三次误判把 demonstrably 拿掉了，而每一次的补丁都是又一条豁免——豁免本身就是承认这个测量
认不出自己在看什么：

- 一页 8 个 bar（4 行，每行 track 2.55in + value 2.26/2.36/2.17/2.20）全判成装饰色条，而
  "ranking → sorted horizontal bars"正是 skill 自己要求的画法。补丁：靠"共享 baseline + 共享
  厚度 + 长度不同 + 每行 ≥2 个 slot"识别 bar series 并豁免。
- 20 页学术 deck 的 5 个满宽 plane（承载公式、训练时间表、结论行）判成"色带上什么都没有"，而
  每个上面坐着 1–6 个文本框；同页一个高 0.06in 的 plane 反而放行。补丁：去掉"必须在页顶"。
- 8 个标题上的 kicker rule——模板自己的装置，1.05in 橙色、0.04in 高、渲染正确——全被拒，差了
  八分之一毫米。补丁：把 hairline 上限从 0.035in 换成测量模块的 `RULE_MAX_HEIGHT_PT`。

下游代价两笔：两轮真实运行因一条本身判错的色带门禁被拒，整轮每一次构建精修都没跑（见 D8）；
而精修——这道门禁自己的 audience——给 8 页加了 accent 色条，正是它判为违规的东西（见 D13）。

拒绝必须第一次就对；三条豁免之后不是。所以读数留下，拒绝取消。它要防的东西没有变：色条仍是
生成式 deck 最响的 tell，finding 照样报出来，只是收件人现在只有作者一个。与 D3a 删掉事实门禁不同——那个
是 10 条全误报、一次真阳性都没有；band 确实抓到过真的（"凡是只被要求别画的 deck，它每次都
回来了"），所以走 D3b 的降级而不是删除。`data_mark_ids()` 的豁免保留：作为 warning，一条已知
的误报仍然是噪音，而噪音会占掉作者本该用来看页面的注意力。

降级之后又出现第四次误判，正落在这条上：`ppt_charts.stacked_bar` 的堆叠柱，三根柱各切三段，
三个细顶段（2.46×0.09/0.13/0.08in）全判成 "an accent strip"。bar series 的判据看不见堆叠柱——
分段不共享 baseline（每段坐在前一段头上），也不在值的方向上共享厚度（那里的厚度就是值），只有
贴基线的底段偶然混进了同一组。补丁：先把分段沿长边接回被切开前的那根柱（同槽位、同厚度、边缘
相接；与邻居重叠的一律另起一根，这正是"焊在卡片边上的色条"进不来的原因——记录在案的那种色条画
在卡片之上），再对这些柱问 bar series 那套问题，只把"长度不同"换成"形状不全同"：100% 堆叠柱
每根长度本来就一样，值在切口位置上。残余一类分不开：一排高度不同、各戴同一条细色条的卡片，与
"第一个 series 恒定"的堆叠柱是同一张图（实测两者矩形集合结构一致），矩形几何无从区分，现按数据
放行。

**D18 精修阶段整个删除，迭代回到 build 的返回里。** 取代 D8 与 D13，并取消 `Finding.audience`。

默认关闭（D13）之后没有一次运行证明它值得打开，而它留下的接线仍在收费：返回里一个
`for_the_design_pass` 桶（实测被模型读成"别人会处理"，25 对文字重叠连续八次构建没人动）、
一个 `polish` 参数（实测被连续八次正式构建传成 `false`）、两份只有它读的 brief，以及 `Finding`
上一个只剩一种取值的 `audience` 字段。

分工的判据不是"多一双眼睛好不好"，而是**这双眼睛能不能改它看见的东西**。精修看得见一页，
却不许改这一页说什么——而一页出问题，一半出在它说了什么。让只能重排的一方做最后一眼，等于
把"这页话太多"变成"这页话太多，但排得整齐"，实测就是同一页被反复切成小格子。

作者拥有整个程序，没有这个限制。所以迭代整个搬到 build 的返回：每次 `ppt_build` 把这一批页的
渲染图连同页面测量一起交回写程序的人，作者看图、改程序、再构建。三条机械保证撑着这条路：
`unseen_page`（BLOCKING）保证没有一页能在作者没看过渲染图的情况下交付——精修没了以后它比以前
更要紧；`unmapped_page`（BLOCKING）保证渲染图对得回画它的那段代码（同题材的 `page_mapping`
只是 WARNING——见 D3b，两者是不同的 kind，硬保证在前者身上）；brief 的 `forbidden` 每次构建
都随回复重述，而不是只在三十轮之前记过一次。

技能侧对应改动：`ppt-script-authoring` §10 明说这是 deck 的最后一眼，后面没有别人。

**D19 内容页默认挑原型，自由构图变成退路。** 取代 D9 的默认值。

D9 把默认设在"内容页自己构图"，理由是填模板槽位失败得可测量：`normAutofit` 把一个 18pt 槽
渲染成 11.7 / 13.7 / 13.5 / 10.8pt，六卡网格填四条留一个洞，竖版图框放不进横版架构图。那些
测量没有被推翻。被推翻的是从它们得出的结论——**失败的不是"用示例页"，是"把示例页当不可改的
模具"**。同一份模板，允许克隆之后再替词、再删掉多余的重复单元、再挪一挪改改尺寸，六卡网格
填四条就是删掉两张卡，竖版图框就是换掉那个框；这些都是程序做得到的编辑，而 D9 一刀把整页
重画，代价是每一页都得自己重新发明标题行、间距和分栏，读起来就不再是用户那套模板了。

所以现在的默认与 D9 相反：

- `ppt_template` 分批交出**每一张可见示例页**的渲染图与反编译源码（`BATCH_PAGES = 8`，96dpi），
  而不是只交封面/目录/章节/封底四页。上限被否决过一次，理由记在 `tools/template.py`：
  实测模板有十三页而上限是十二，第十三页就永远看不见，而"挑一页来改"挑不了看不见的那页。
- `ppt_outline` 把**没有声明 `prototype` 的内容页列进 asks**，措辞是"这份 deck 有模板，
  为每一页挑最近的可编辑示例页；只有当哪张示例页改完也装不下这个信息形状时才留空，
  并把那个具体的不匹配写进 `needs`"。
- 自由构图仍然在，仍然落在量出来的 house style 里（D9 那半没变），但它现在是退路而不是默认。

`prototype_kept`（页面是否建在大纲承诺的原型上）仍然只是 WARNING，理由没变（D3b）：它比的是
页面与计划，而一页自己画得更好时硬拒等于要一个更差的页面。`template_adherence` 也仍然只问
开场/索引/分节/收尾四处——**这一条与新默认是有张力的**：计划要求每个内容页都挑原型，而这道
检查刻意不数内容页。张力是有意留的（检查一旦数内容页，就会把"改得多"读成"没用模板"），但
两处的措辞要一起读，不要各自当全部。

**D20 量缺陷，不量构件。** 一道门禁应当测"读者看到的那个毛病"，而不是"页面上有没有这类
构件"。测构件的检查有两种失败方式，而且总是同时出现：**它报出正确的做法，同时对错误的做法
一无所知**。三次应用，每次都是把判据从名词换成症状：

| 门禁 | 原判据（构件） | 现判据（缺陷） | 换判据时量到的 |
| --- | --- | --- | --- |
| `native_table` | `shape.has_table` 为真 | 表上还穿不穿 Office 自己给的那套外观（行/列 banding + python-pptx 那个 GUID 图库样式） | `ppt_layout.table()` 也走 `add_table`（.pptx 里没有别的路），所以 deck 自己的表格 helper 画的每一页都被报——一道对着最好答案开火的门禁，教出来的是别用最好答案 |
| `wide_table` | 列数 > 8（`MAX_TABLE_COLUMNS`） | 每一列有没有它自己最宽那格所需要的宽度，按那格自己的字号量 | 十列短列的 `ppt_layout.table` 从"报"变成 0；2.4in 里塞四列短语的手绘表从 0 变成 1。第二行才是重点 |
| `band` | 在页顶、且满宽的填充条 | 这条色带上面有没有承载文字（承载即分组，位置不限） | 20 页学术 deck 的 5 条满宽 plane 上各坐着 1–6 个文本框，全被判成"色带上什么都没有"；同页高 0.06in 的 plane 反而放行 |

判据是可否证性：构件是名词，页面上有或没有，问它得到的答案永远是"有"；缺陷是谓词，可以为
假。所以 `MAX_TABLE_COLUMNS` 删掉了——一个数不出缺陷的常量，调它只是换一批误报。

同一条原则的反面也要守住：`native_table` 现在只判 banding 与那个 GUID，**刻意不判**一张表
占了它那块地方的多少。文件里没有东西能区分"本来就该小"和"没填满"，那要读渲染图。宁可少说
一句，不要把一个测不出来的问题写成一条 finding。

**D21 第二读按"有多少页没被读过"触发，草稿也算，读过的记号按页版本记。** 原先只在
"交付、且无 blocking"的那一次构建里跑一次，理由是草稿是半成品、被拒的不是 deck，读它等于
列一份即将改掉的清单。测量推翻了前半句：18 页那次运行前十一次构建都是草稿，第二读因此落在
终点线上——它在一次 873s（占全程 19%）的 `ppt_build` 里读完 18 页、在 18/18 页上报出 45 条
问题，措辞具体且正确（p11 论文页眉被裁进图里、p14 的 `EFFICIENTNET` 标题加黑线、p3/p8
全角逗号前的空格）；而 `review.json` 写下 38 秒后 `deck.pptx` 就发布了，中间只有一次
`edit_file` 和一次 `ppt_build`。上一次运行也只消化了 49 条里的 11 条。

问题不在措辞而在时机：站在终点线上，答复 45 条等于重做 18 页，发布才是理性选择；同样这
45 条落在第五页，改的是后面十三页即将重复的那个模式。所以：

- **只保留 blocking 这一道否决**（一页正被门禁拒着，它本来就要改），撤掉草稿这道。
- **未读页数 >= 5 时触发**（`tools/build.py` 的 `UNREAD_PAGES_BEFORE_READING`，用户定的数，
  不许调参），只读没读过的那几页。交付的那次构建门槛是 1——把剩下的都读掉，
  **不变量是交付时没有任何页版本是未读的**。
- **记号按页版本，不按页号**：`review.json` 的 `pages_read` 从 `[3, 7, 9]` 变成
  页号到指纹的映射，指纹取 `services/regress.py` 已经在记的那个（`seen.blocks_of` 对该页
  代码块的 sha256 前 16 位），不另造一套哈希。读过之后又被改写的页重新算未读——没有这一条，
  早读就是严格更差：早读一页、之后重写，交付的就是没人看过的那一版。旧格式（列表）按
  "读过、版本未知"处理，续跑不崩、也不重读。

代价说清楚：**这不省模型时间**。原先 18 次页读全在终点，现在是 18 次加上"读过之后被改写"
的那些，摊在全程，总量相同或更多。变的只是发现落在哪里。"模式改一次而不是十八次，于是重建
轮次变少"（那次运行花了 53 次 `edit_file`、17 次 `ppt_build`）是**未测量的**期望，不作为结论。
风险同样明写：原本 16s 的草稿构建现在可能要几分钟，上界由页版本记号给出——每个页版本只读一次。

---

## 5.5 上游接线：收敛到配置，不改造主干

旧 fork 不是"上游 + PPT 插件"，而是把主干改成了 PPT 专用 sub-agent。三处硬编码：

| 位置（旧 fork） | 做了什么 |
| --- | --- |
| `context_engine/segments/render.py:50-73` | `identity_text()` 整体替换为 "# Raven PPT Authoring Sub-agent"，末尾强制 `MEDIA: <pptx 绝对路径>`，上游通用身份与 platform policy 被删 |
| `context_engine/segments/render.py:76-88` | `load_bootstrap_files()` 不再读 workspace 的 `soul.md`/`agent.md`，改从 `raven.templates` 包读 `AGENTS-ppt-build.md` |
| `context_engine/factory.py:92-96` | 活跃 skill 白名单硬编码为两个 PPT skill，`allowed_sources={"builtin"}` |

这三处让 PPT 能跑，代价是仓库只能做 PPT。**重构后一律由配置驱动**：`tools.ppt.profile`
选中一条路线时，该 profile 的 `skill` 字段声明它要激活的技能，身份与 bootstrap 走上游原有
机制；未选中 profile 时上游行为一字不变。这是"对上游的改动收敛到一处装配点"的实际含义。

### 必须落地的接线点

上游已提供、不要重造：`ToolResult.blocks` + `providers/capabilities.supports_image_tool_result`
（返图）、`utils/helpers.ContentPart`/`image_block`/`text_block`、`security/network`、
`chat_with_retry`/`chat_stream`。fork 的 `providers/media.py`、`_responses.py`、
`openai_responses_provider.py` 被上游能力取代，不移植。

上游缺、必须补的四处（每处都有实测理由）。**四条都已落地**，落点记在每条后面：

1. **`_route_result_images` 会丢绑定文本**。上游原来是
   `attach = [b for b in blocks if b.get("type") == "image_url"]`，把每张图前面那条
   "这是第几页" 的 text part 全部丢掉；丢了绑定模型就对不上页号。
   → `raven/utils/helpers.py` 的 `labelled_images()`：保留紧邻图片之前的 text part，
   一条 label 只用一次，单张图裸走（没有要消歧的东西）。测试在
   `tests/ppt/test_upstream_wiring.py`。
2. **一轮只允许一个视觉结果**。不闸住一轮就能塞进几十 MB base64。
   → **换了个位置解决**：闸不在上游而在 PPT 侧，因为"一轮几张"是路线的事而不是环路的事——
   `tools/build.py` 的 `BATCH_VIEWS = 1`、`stages/build.py` 的 `BATCH_VIEWS = 3` 决定张数，
   `stages/_views.py` 的 `MAX_IMAGE_BYTES = 900_000` 决定每张的字节上限（超了就折半重编码）。
   上游那侧补的是撑爆窗口时的应急路径：`_elide_older_images` 只留最近一条带图消息。
3. **`get_definitions(only_names=...)`**。PPT 工具全量暴露 schema 的 token 成本很高，
   需要按已走到的阶段只暴露该阶段的工具。→ `raven/agent/tools/registry.py:43`，
   点名一个没装的工具只是把列表变短而不是报错。
4. **字符串内嵌 data URI 的脱敏**。session 文件会涨到几十 MB 并在 resume 时灌回上下文。
   → `agent/loop/main.py` 的 `_strip_inline_images()` 在 `_save_turn` 里把结构化的 inline
   base64 换成 `[image]`；`str` 内容走 `_TOOL_RESULT_MAX_CHARS = 16_000` 的截断。图片路径
   经 `describe_image` 留在文本里，所以模型后面还能 `read_file` 取回。

### 轮内压缩

PPT 每轮返图量大，多轮必然撑爆上下文窗口。轮内压缩（把旧的 inline base64 换成占位文本，
模型需要时用 `read_file` 取回）因此属于 PPT 必需，而不是可选优化。已落地为
`_elide_older_images`（撑爆窗口时只留最近一条带图消息）与 `_strip_inline_images`（写入
session 时一律换掉）；占位文本目前不带路径，路径由 `describe_image` 另行留在正文里。

**返图的实际形状**：`render_dpi` 默认 144，16:9 一页正好 **1920×1080**（`services/render/pdf.py`
的注释就是这么定的）；一次 `ppt_build` 回的张数由 `BATCH_VIEWS` 决定（工具侧 1、阶段侧 3），
不是一次 8–12 张。`ppt_template` 挑示例页那批用 96dpi（`BATCH_PAGES = 8`），因为那批回答的是
"哪页最像我要说的话"，不是"这号字在会议室里看得清吗"。

---

## 5.6 从 AutoDesign 借来的三条

同机器上的 `AutoDesign` 仓库解决过同一类问题，以下三条经过评估后采纳，其余不采纳的原因也记下来。

**采纳一：finding 与修复动作在数据结构层面绑定。** 它把 16 个异构检查器的结果折叠成一种
`DesignFeedbackFinding`，字段含 `target`/`evidence`/`suggested_action`/`repair_route`/`repairable`，
而修复工具 `apply_design_ops` **要求每个 op 带 `finding_id`**，且一批 op 原子生效。
好处不是整洁：它让"这条 finding 修了三次还在 → 换策略"成为可查询的事实，而不是靠人读日志。
本仓库的 `Finding` 在设计环落地时补上 `finding_id` 与 `repair_route`。

**采纳二：模型写语义，渲染器算几何，几何反向测量回来。** 它的 `render_mode` 有
`scene_graph`（模型给 bbox）与 `authored_html`（模型只写语义，浏览器算布局，再用 Playwright
量真实 box）两种，学术海报强制走后者。溢出与重叠是**测出来的**，不是模型自报的。
本仓库的三条路线正是这个谱系的三个点：脚本路线几何来自执行，槽位路线几何来自引擎，
图文路线几何来自安全区契约——三者的共同点是测量永远读渲染产物，不读模型的声明。
它的 grounding 在 Playwright 缺失时降级为 warning 而非硬失败，与本仓库"缺依赖时 skip 并标注
前置条件"一致。

**采纳三：生成的栅格图永不作为交付物承载文字。** 它的 `generate_background` 对模型写的任何
提示词都**强制追加** "No text, no characters, no lettering, no symbols, no logos, no watermarks"，
并且对学术海报直接拒绝生图；生成图只作为风格参考（`generate_visual_reference` 的注释写明
"advisory only, must never become the final artifact"），因为交付物需要原生文字、可编辑图层与
有出处的图表。这条直接定型了本仓库路线 C：背景阶段的提示词由引擎加固（模型写不出带字的图），
文字阶段只能写进背景阶段声明的安全区，`text_in_background` 是 fail-closed 类别。

**不采纳：** 它的 `_run_inner` 是一个 1600 行单函数，"阶段"只存在于 51 个取消检查点的字符串里，
resume 分支与正常分支在同一函数体内交错——这正是本仓库要反着做的地方。它的
`ctx.state: dict[str, Any]` 是全局可变布告板（声明了 8 个 key，实际塞了几十个，无 schema 无
owner），是该仓库最大的耦合源；本仓库 `StageResult.data` 是不可变 mapping 且只由拥有该阶段的
工具适配层读取，不设跨阶段共享的可变状态。它按档位改 prompt 文本而非改编排（"档位"靠自然语言
说服模型），以及 skill 选择写成 if-elif 硬链（加 skill 必须改 registry），两者都不采纳。

---

## 6. 移植进度

阶段划分与实际结果记入 `docs/ppt-raven-status.md`。已完成 Wave 0（骨架与契约）、
Wave 1（render / ingest / assets / measure+gates 四段并行）、Wave 2（脚本后端、工具层、
设计环、装配与接线）、路线 A 端到端，以及**带真实模型的端到端**——三十余轮实跑，证据与
每轮修掉的缺陷逐条记在状态文档 §2。Wave 2 里那个"设计环"此后整个删除（D18）。
待做的是 Wave 3 的路线 B 与路线 C。

并行只经 worktree 分支进行，合回 `feat/ppt`；不得两个执行体写同一分支。
