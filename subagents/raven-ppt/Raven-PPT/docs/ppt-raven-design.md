# PPT-Raven 架构设计

本文件是 PPT 能力的**设计权威**：分层、边界、决策与阶段规划。当前能力与验证证据写入
`docs/ppt-raven-status.md`；长效开发规则写入 `CLAUDE.md`。三份文件不重复彼此的内容。

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
（事实门禁、返图、发布、图像证据）在两条路线里各写一遍。再加第三条路线，分支数是乘法关系。

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
    kind: str            # "fact" | "citation" | "band" | "density" | "type_floor" | ...
    severity: Severity   # BLOCKING | WARNING
    page: int | None
    message: str         # 给模型读的一句话，自带修法
    detail: dict         # 结构化证据
    audience: Audience   # AUTHOR | DESIGNER —— 谁有权解决它
```

`audience` 是一条既有教训的固化：内容密度必须给作者，不能给精修环——精修被禁止改内容，
把内容问题交给它等于交给它一个无权解决的问题，结果是同一页被反复重排。

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
| 阶段 | ingest → inspect → author → build → design_pass → publish | ingest → plan → fill → compile → review → publish | ingest → plan → background → place_text → build → publish |
| 后端 | `script` | `slots` | `imagetext` |
| 能力 | `raw_script` | `normalized_regions` | `background_prompt` + `normalized_regions` |
| 复用 | ingest / measure / gates / render / publish 完全共享 | 同左 | 同左 |

路线 C 的新代码只有三块：`services/imagegen`（接口 + 一个适配器，本地模型与 API 同一接口）、
`stages/background`（把一页的意图变成生图提示词 + 文本安全区契约）、
`stages/place_text`（把文字放进安全区）。测量、门禁、渲染、发布一行不动 ——
`measure` 本来就从渲染后的 PDF 里读词级 bbox，这正是"验证生成背景上的文字有没有压住内容"
所需要的手段。

---

## 5. 已确立的决策

以下决策来自旧形态上的实测教训，重构后继续有效。

**D1 保证在代码里，不在 prompt 里。** 页数、字符预算、事实锚定、无重叠由工具执行路径强制；
prompt 只做引导。

**D2 测量结果回灌设计环，不硬拒——渲染重叠除外。** 字号下限、越界、冲卡、标签折行以 WARNING
finding 进下一轮设计调用；硬拒会与文本适配形成"缩回—再缩回"振荡，终点是没有交付物。

**渲染实测的文字重叠（word_collision）是这条的例外，改为 BLOCKING。** 三轮真实运行都交付了
"字压在字上"的 deck，回灌没有收敛。它不是口味问题，也不能靠缩字号解决——字号下限同样被测量，
缩下去只是把一条 finding 换成另一条；它真正要的动作是把文本框画宽，而画宽不花任何代价。

**D3 fail-closed 发布。** 存在 BLOCKING finding 时拒绝发布。blocking 集合由 profile 声明，
当前为：事实未锚定、图注引用与实际图片不符、色带、页与代码块无法对应。

**D4 降级显式留痕。** 文本溢出降级链每步留痕，禁止静默截断半句。

**D5 vendor 隔离。** 只经适配层触达 vendor，固定上游 commit，改动以补丁记录。

**D6 空提交即"跑现有程序"。** 工具收到无程序内容的提交时保留原文件并构建它，而不是拒绝。
拒绝虽然保住了文件，但把作者推出工具去用 `exec` 自行交付，绕过全部门禁。
（实测：一次运行连续三次提交 `" "` / `"\n"` / `"# use existing build.py"` 被拒后离开工具。）

**D7 内容密度归作者，精修不动内容。** 密度只设上限不设下限。

**D8 草稿归作者迭代，精修只做定稿前的最后一眼；精修跳过必须留痕。**
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

**D9 模板出「框架 + 风格」，内容页由模型自己构图。**
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

**D13 精修阶段默认关闭，配置项打开。** 四次真实运行的实测结论是它让 deck 更差。最后一次：
3420 字里删掉 224 字（一整条要点、一张图的说明、一页的结论），另有三行被悄悄改写，全部是它
自己 brief 第一段明令禁止的；同时给 8 页加上装饰色条，而那正是本 deck 门禁判为色带的东西。
两条机械保证现在都有了（`copy_rejection` 与 band 门禁），但一个价值未被证实的阶段不该是默认，
何况这条路线不开它就已经不错。`tools.ppt.designer.enabled = true` 打开；关闭时回复里明说是
"配置关了"还是"草稿不精修"——静悄悄什么都不做的阶段，和跑过但没发现问题的阶段，读起来一模一样。

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

上游缺、必须补的四处（每处都有实测理由）：

1. **`_route_result_images` 会丢绑定文本**。上游 `agent/loop/main.py:982` 是
   `attach = [b for b in blocks if b.get("type") == "image_url"]`，把每张图前面那条
   "这是第几页" 的 text part 全部丢掉。PPT 一次返 8–12 张图，丢了绑定模型就对不上页号。
   改为保留紧邻图片之前的 text part。
2. **一轮只允许一个视觉结果**。上游没有这个闸。PPT 一次 review 回 8–12 张 1280×720 PNG，
   不闸住一轮就能塞进几十 MB base64。
3. **`get_definitions(only_names=...)`**。上游 registry 无此形参。PPT 工具全量暴露 schema 的
   token 成本很高，需要按已走到的阶段只暴露该阶段的工具。
4. **字符串内嵌 data URI 的脱敏**。上游只处理结构化 image part，抓不到 JSON payload 或错误
   消息里内嵌的 data URI，session 文件会涨到几十 MB 并在 resume 时灌回上下文。

### 轮内压缩

PPT 每轮返图量大，多轮必然撑爆上下文窗口。轮内压缩（把旧的 inline base64 换成
`[image elided: <path>]`，模型需要时用 `read_file` 取回）因此属于 PPT 必需，而不是可选优化。

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
设计环、装配与接线）与路线 A 端到端。Wave 3 的路线 B、路线 C 与带真实模型的端到端待做。

并行只经 worktree 分支进行，合回 `refactor/ppt_on_upstream`；不得两个执行体写同一分支。
