# PPT-Raven 当前状态

本文件记录**已验证的能力、证据、限制与待办**。设计与决策见 `docs/ppt-raven-design.md`；
长效规则见 `raven/ppt/AGENTS.md`。三份文件不重复彼此的内容。

基线：`upstream/main` @ `5a0c950`（EverMind-AI/Raven）。分支 `refactor/ppt_on_upstream`。

---

## 1. 已落地

| 层 | 模块 | 状态 |
| --- | --- | --- |
| contracts | `project` / `findings` / `capability` / `deck` / `build` / `profile` / `stage` / `rendered` / `sources` | 完成 |
| services | `ingest`（文档、图注、事实、图表、图片、去重） | 完成 |
| services | `assets`（十套白底主题、180 图标、字体、脚本 helper 投影） | 完成 |
| services | `measure`（声明几何 + 渲染真值） | 完成 |
| services | `gates`（事实、图注引用、色带、页-代码映射、门禁注册表） | 完成 |
| services | `render`（pptx→pdf→png、词框、联系表、能力探测） | 完成 |
| services | `publish`（暂存、digest 校验、原子写、fail-closed） | 完成 |
| services | `template`（准备副本、反编译成 python-pptx、克隆改写、清单、绑定） | 完成 |
| backends | `script`（运行程序、块映射、提交守卫、脚本编辑） | 完成 |
| stages | `build`（构建→测量→精修→再测量→发布）、`design_pass`（两段式设计环） | 完成 |
| profiles | `script_author` / `slot_author` / `image_text` 三条路线声明 | 声明完成；后两条后端未实现 |
| tools | `ppt_brief` / `ppt_ingest` / `ppt_fetch` / `ppt_template` / `ppt_build` + 唯一返回信封 | 完成 |
| 接线 | `tools.ppt` 配置、agent loop 注册、三个 CLI 面 | 完成 |

**路线 A（`script_author`）可端到端运行。** 路线 B（`slot_author`）与路线 C（`image_text`）
已作为 profile 注册，但后端未实现，装配时返回空工具集并记一条 warning——不会注册一条
调用即失败的路线。

## 2. 验证证据

```
uv run ruff format --check raven tests && uv run ruff check raven tests   # 通过
uv run pytest tests/ppt -q                                               # 785 passed
uv run pytest tests/ -q --ignore=tests/tui                                # 见下
```

全量套件：**7288 passed / 9 failed / 20 errors / 36 skipped**。九条失败与二十条错误全部
在原始 `upstream/main` 上以相同数量出现，与本分支无关，逐条核对如下：

| 来源 | 数量 | 原因 |
| --- | --- | --- |
| `test_cli_cron_commands.py` | 3 failed + 20 errors | 环境缺 tzdata；该文件单独运行在基线与本分支上结果逐条相同 |
| `test_cron_tool.py` | 3 failed | 同上 |
| `test_everos_server.py` | 1 failed | 同上 |
| `test_config_loader.py` | 1 failed | 该测试假设某路径不可写，而以 root 运行时可写 |
| `test_cli_theme.py` | 1 failed | 单独运行在基线与本分支上均通过 → 测试间污染，非本分支 |

移植过程中本分支自己引入过一条失败：在设计环里手写了图片 content part 的字面形状，被上游
`test_image_block_is_the_only_place_the_shape_is_written` 抓到并已修（`266af76`）。

### 端到端（`tests/ppt/test_end_to_end.py`，无模型参与）

六条，从 materials 到交付文件：干净 deck 交付；编造数字拒绝交付；页与代码无法对应拒绝交付；
交付字节等于被测量的字节；字号低于下限只警告仍交付；渲染链每页产出一张图。

### 端到端抓出的真缺陷

测量阶段把 ingest **目录**传给了只吃索引**文件**的读取器，读取抛异常、异常被当作
"尚未 ingest" 吞掉，于是一份写着素材里从未出现过的数字的 deck 干净发布。
两侧的单元测试全部通过。这是这条测试存在的理由。

### 各段移植过程中发现并修复的既有缺陷

| 缺陷 | 后果 | 发现于 |
| --- | --- | --- |
| 两个并发 LibreOffice 转换共用 profile | 只产出一个 PDF，输家目录空、stderr 无输出 | render |
| `soffice` 是 shell wrapper，超时只杀 wrapper | 孤儿 `soffice.bin` 持着 profile 目录 | render |
| bbox 解析用 `[\d.]+` 且不反转义 XML | 负坐标词静默丢失；`R&D` 变 `R&amp;D`，事实门禁永远匹配不上 | render |
| `_BAD_ESCAPE_RE` 无法跳过合法 `\\` 对 | 含路径或转义反斜杠的代码块越修越坏 | 设计环 |
| 表格裁剪上下限无条件推进 | 并排两表时第二张图从它命名表格下方 200pt 开始 | ingest |
| `ppt.py:7493` `_err(..., stderr=…, **payload)` 重复关键字 | 必然抛 `TypeError`，被吞成 "Error executing ppt_build" | 审查（未移植该形状） |
| 门禁与 ingest 各写一份 `SourceIndex`，字段名已漂移 | `stated_numbers` 读成 None，图表数值检查静默失效 | 合并 |

### 删除的不可达代码（不移植，证据见各段报告）

`PptOutlineDesignTool` 483 行 + `PptLayoutDesignTool` 1022 行 + 专属 helper 约 1300 行：
注册卫语句 `if expert_outline and not free_composition` 与 `expert_outline` 的定义互相矛盾，
**任何配置下都注册不了**，且有一条测试把这个 bug 固化成预期行为。
`PptDeckTool` 392 行从未被实例化（唯一实例化点在测试里）。
合计约 3200 行 / 旧 `ppt.py` 8129 行。

另删除：四份重复实现（形状遍历、填充判定、"有无文本"、三个 finding 投影中的第四份内联副本）、
声明几何版的溢出与重叠检测（文字位置不是 pptx 的属性，旧代码自己的注释写明它会在渲染干净的页上报错）、
`ingest` 的 `tile_grids`/`stitch_grid`、`theme_helper.catalog_summary`、`state.pending_figure_view`、
`BuildResult.fact_violations`。

### 真实素材上的 ingest（TarViS 论文，8 页）

11 个资产、10 个带 `source_label`，Figure 1–5 与 Table 1/2/4 全部正确归属；
事实索引 309 个数字 / 307 个 stated / 90 个实体，materials.md 56110 字符。

**同时暴露一个问题**：Figure 2 出现三次——它的三个子面板各自匹配到同一条图注，
而"有图注就保留"这条规则（图注即否决权）让 bbox 去重放过了它们。后果不是判错，
而是目录里三个资产同名，模型引用 "Figure 2" 时指向哪一个不确定；图注门禁按图片字节
映射到 label，三者都映射到 "Figure 2"，所以门禁仍然通过。
**重复的 `source_label` 本身就是碎片信号**，应当作为去重的第二个判据——未实现，
因为盲改有丢掉真正 Figure 2 的风险，需要对着论文实测。

### 带真实模型的端到端（gpt-5.6-sol，TarViS 论文）

走通了：`ppt_ingest`（24s，11 资产 10 带标签）→ 作者写 16KB 的 `build.py` → `ppt_build`
**构建出 18 页并触发门禁**（返回 `ok:false`）→ 作者按 findings 改脚本 → 再次构建。
装配、后端、执行侧页-代码映射、测量、门禁、返图链路全部实际运行。

**同时暴露两件事，其中第一件是本轮最重要的发现。**

**(a) 事实门禁在这份 deck 上有 19 条阻塞级假阳性。** 被拒的是全大写的普通英文词——
`VIDEO` / `TASK` / `INPUTS` / `QUERY` / `WHAT THE PAPER SHOWS`。原因是 `ENTITY_RE` 的
`[A-Z]{2,6}` 分支把任何全大写词当作缩写，而 caps 豁免要求**整段大写串作为一个短语按原序
出现在素材里**。19 条里 12 条的词**单个都在素材里**（`TASK INPUTS` 是列头，论文说 task
也说 inputs，只是没并排出现）。

已修的部分：豁免的窗口从 2..6 词扩展到 1..6 词，`CAPS_PHRASE_RE` 从"至少两个词"改为
"一个或多个词"。单个全大写普通词（`AGENDA` 这类，只要素材用过该词）现在放行，有测试。

**未修的部分**：逐词回退（整段短语不匹配时，退化为逐个词判定）会推翻一条既有的、
有意为之且有测试的策略——`test_lowercase_words_do_not_globally_authorize_a_caps_phrase`
断言 "FAST STEP CARE" 即使每个词都在素材里也应被拒，理由是"素材自己的词、且按素材的顺序"。
两种读法各有道理：整段短语可能是编造的口号；而列头不是断言。实测代价是 12 条误拒，
且作者随后离开工具自行交付。**这条需要你定。**

**(b) 门禁阻塞后，模型绕过工具自行交付。** 日志里可见 `exec` 做
`cp build/build.py …` 然后 `PPT_OUTPUT=… python build.py` 再 `mkdir -p exports/tarvis`。
这与旧 fork 上观察到的行为一致，也印证限制 8：交付旁路无法在工具内封堵。
但要注意因果——它是被 (a) 的误拒推出去的。

### 模板路线（在用户自己的 193 份模板上实测）

证据都来自跑，不是读代码。脚本在 scratchpad，结论如下：

| 测量 | 数字 | 它决定了什么 |
| --- | --- | --- |
| 模板的视觉元素落在示例页 vs 版式 | 587 : 102（每模板 29 : 5，20 份样本） | 示例页才是参考，`add_slide(layout)` 拿到的是占位符和几乎没有设计——所以是"反编译示例页"而不是"列版式名" |
| 页面自身声明字号的文本形状 | **0 / 295**（40 份样本） | 62% 的字号在版式占位符上，38% 在母版 `txStyles` 上。补完继承链前，每份模板的每个标题在参考里都是"没有字号" |
| 补完继承链后 | **295 / 295** 有字号 | — |
| 反编译出的源码能执行 | **199 / 199 页** | 唯一有意义的正确性判据；四个 bug（组的缩放、`frame.text` 之前取 `para`、`clrMap`+alpha、整页设计在版式上）全是重放抓出来的，读代码一个没抓到 |
| 页面含 python-pptx 写不出的东西 | **128 / 199 页（64%）** | 自定义几何 88、半透明填充 77、渐变 48、图片填充 41、阴影 21、图表 3。**这就是 `compose` 存在的理由**：这些页只能克隆后改写，写代码到不了 |
| 跨文件克隆整份模板 | 232 页 / 28 模板，0 个重复 zip 条目，0 异常 | 修掉两个会静默产出损坏包的缺陷：`add_slide` 收到外部包的版式（版式+母版+主题被重复写入），以及图片部件跨包搬运（媒体编号冲突） |

端到端：绑定真实模板 → 取示例页代码 → 通过真实 backend 跑作者程序（克隆封面改字、克隆内容页、
按参考给的版式索引自己写一页）→ 渲染。三份模板都产出完整继承模板视觉的三页（渲染图在 scratchpad）。

### 五轮真实模型端到端（run5–run11：sol / opus5，OpenRouter 与 qimux）

每一轮都以修掉产品缺陷收尾；下面只列跑出来的事实，逐条对应一个提交。

| 轮 | 模型/通道 | 结局 | 它证明的事 |
| --- | --- | --- | --- |
| run5 | sol / tokrouter | **8 页导出，0 blocking** | 精修每轮都跑（它是唯一没有 blocking 的一轮），页面自己画流程/卡片/表格/折线，并两次主动标注证据边界 |
| run6 | opus5 / OpenRouter | 20 页建成未导出 | 5 条 `band` 误拒把精修整轮关掉 |
| run7 | opus5 / OpenRouter | 20 页建成 | 同上 + 12 页正文低于 14pt |
| run9 | opus5 / qimux | 只到大纲 | 大纲 12 页**每页都指名了模板原型**；驱动把"无工具调用"当收工 |
| run10 | opus5 / qimux | 只到大纲 | `finish_reason=length` ×3：思考 + 12 页脚本撑爆 32k 输出预算 |

真缺陷（都由跑出来的证据定位，非读代码）：

| 缺陷 | 证据 | 修复 |
| --- | --- | --- |
| 精修被任何 blocking 关掉 | run4/run6 三次构建精修一次没跑，而拒绝理由本身是错的 | 只有作者向的 blocking 才跳过精修，设计者向的照跑，导出仍拒 |
| band 门禁两套 hairline 定义 | 标题下 1.05×0.04in 橙线被拒 8 次；119 份真实模板里 2.5pt 以下 0 条、2.5–4.5pt 占 30% | 门禁改为引用 `RULE_MAX_HEIGHT_PT` |
| 承载文字的浅色横带被当空色条 | 20 页学术 deck 上 5 条，每条都有 1–6 个文本框压在上面 | 判据改为"承载内容即分组"，位置不限 |
| 引用门禁从未运行 | 读 `payload["figures"]`，写入方是 `payload["assets"]`；且 `_figure_labels` 传的是 catalogue 而非 sha→label | 两处修正，run4 deck 上恢复为 13 条目/10 标签 |
| `evidence` 的手绘分支从未触发 | 自己画了 5 页图表的 deck 被告知"0 of 8 pages show anything" | 判据改为"空形状"，两份真实 deck 均落在 6/8 |
| 镜像目录静默丢文件 | `.zip`/`.doc` 既不复制也不提 | `mirror` 报出留下的文件；`.pptx` 单独报为模板 |
| 事实门禁拒纯字母缩写 | run7 大纲因 `SOTA` 被拒一轮 | 纯大写降为 `unfamiliar_name` 警告，字母数字标识符仍拒 |
| 工具在双重编码数组上崩栈 | run8 两次 `AttributeError: 'str' object has no attribute 'get'` | 四个数组参数统一经 `_args` 读取 |

### 输出被截断是网关的限制，不是产品的

三轮死在同一处：模型要一次吐出整份 12/20 页程序，传输把回复切断（日志里先
`RemoteProtocolError`，再 `finish_reason=length`），被切掉的正是工具调用，于是什么都没运行。
输出上限从 16k 抬到 32k、64k、128k，失败只是换了位置——约束在传输，不在预算，判断是
API 供应商侧的问题，将来换掉即可恢复"一次生成完整程序"。

产品侧留下的是**能力而非规矩**：`ppt_build(draft=true)` 接受"还在写"的中间状态，构建已有页面、
测量、回渲染图，不拿约定页数/大纲映射/逐页看过三条要求卡它，也不发布。缺的从来不是分块写的
手段（`write_file` 能追加、`ppt_build(slides=…)` 能只看几页），而是中间状态的**许可**——
三页草稿撞上十二页的 brief 就是一条 `page_budget` 拒绝，那会把作者推回那一次巨大的调用。

### 上下文规模（实测，无压缩的测试驱动）

驱动不做任何压缩——工具结果与渲染图永久留在消息里。末轮请求估算：run6 最大，**约 80k
tokens**（56 张渲染图 ≈ 69k），run5 68k，run7 33k。**没有任何一轮接近 1M。** 图片是主导
成本（96dpi → 1280×720 ≈ 1.2k tokens/张），这也是渲染分批（`BATCH_VIEWS=12`）的理由。
生产形态下压缩归上游 `context_engine`（Curator + HistoryTrimmer），PPT 包按分层规定不碰。

### 模板贴合度可测（三档标定）

| 情况 | 形状落在模板位置的比例 |
| --- | --- |
| `adapt` 直接克隆 | 100% |
| 克隆 + 删 1/3 + 挪 2 个 + 加 1 个 | **86%**（删除不计入分母） |
| 手绘 8 页（同模板只当底色） | 2%–6%，封面 50%（确实继承了 Title Slide 的家具） |

门槛 0.5 判"用了模板"，0.2 以下才点名，中间是"留框架重画正文"（计数不点名）。按面积加权
测过，更差：满版背景矩形就能对上，手绘 deck 能拿到 12%–23%。

### 2.x 模板：框架克隆 + 内容页自由构图（D9–D11）

**填模板槽位的失败是量出来的，不是判断出来的。** 同一份模板、同一篇论文、四轮真实运行：

| 现象 | 量到的数 |
| --- | --- |
| 一个 18pt 卡片槽塞进一句话 | 渲染成 11.7 / 13.7 / 13.5 / 10.8pt（同一页四张卡） |
| 同一原型的另一页 | 13.5 / 13.5 / 13.5 / 11.1pt——与上一页同槽不同号 |
| 封底一页五行正文 | 渲染成 2.01pt |
| 六卡网格填四条 | 底行 2/3 空白，白卡 40 sq in 里 11 sq in 是洞 |
| 竖版图框 + 横版架构图 | 比例差 2.4x，模型连续三次只换 shape 序号，整轮没出 deck |
| 模板自带装饰图占页面 | 26%，同页没有一张本 deck 自己的图 |

**改法**：`ppt_template` 只渲染并只交出封面/目录/章节/封底四页；内容页给
`house_style`（量出来的，不是抄的）。本模板量到：`layout="Title Only"`、标题行
`(0.72, 0.14) 11.88x0.98in @28pt`（10 个内容页里 7 页一致）、字号阶梯
`28 / 24 / 18 / 16 / 14`（取自 master `titleStyle`/`bodyStyle`）、`face="Arial"`
（模板 theme 声明的是微软雅黑，示例页 203 个 run 全是 Arial）、安全区
`(0.72, 0.14, 11.88, 6.56)`、正文区 `(0.72, 1.24, 11.88, 5.46)` 外加一行可直接粘贴的
`Box(...)`。三份不同模板上都完整量出（title + ladder + safe + layout）。

**字号改从渲染读**（D10）：模板文本框普遍 `<a:normAutofit/>` 且不声明字号，旧普查跳过这类
run，上表前三行全部报"干净"。`type_drift` 归组同槽位并报被缩小的副本；组内标准字号取观测
最大值（autofit 只缩不放）。

**新门禁**：`covered_shape`（BLOCKING，z-order + 不透明 + 面积，合成页实测 98% 遮挡命中、
卡片在图下方时不误报、整页背景画在最后时两条内容都命中）、`clipped_copy`（WARNING，
文件声明的字 vs 渲染出的字，三份真实 deck 上 0 误报）。否决了"按文件报两个文本框重叠"：
好 deck 上 9 条里 8 条是整宽标题框压角上页码框。

**密度**：`copy_density` 从数 `split()` 词改成数字符——旧的 250 词上限在中文 deck 上永不触发
（四份真实 deck 的最大值 75 词）。真实内容页 200–467 字符，其中 467 的那页（对比表 + 两条
结论 + 注）是评审认可的好页，故上限定在 700。大纲的 thin_page 下限随之从 4 条/80 字符提到
5 条/140 字符：模板槽位不再是天花板之后，计划是唯一决定密度的东西。

**D9 之后的第一份产物**（opus5，自由构图内容页）：标题落在 house 标题行、左图右四项、
统一字号、accent 只用在一条结论上，无卡片洞、无 autofit 缩字、无模板占位图。

### 2.y 精修阶段：三次被挡住，第三次是它自己拒绝

D8 修完"什么时候该跑"之后，精修在真实运行里仍然 **0 次执行**，原因换了两个：

1. `ppt_build` 有个 `polish` 布尔参数，描述还写着"false 可以跳过"。sol 连续 **8 次正式构建**都带
   `polish=false`。参数已删除——草稿不精修、正式必精修，`draft` 就是全部选择。
2. 参数删掉后它开始跑，然后**自己拒绝**：`the block that draws slide 1 creates 0 slides
   (looking for add_slide)`。准入检查用文本搜 `add_slide(` 来确认"一块画一页"，而 D9 的结构页是
   `adapt(...)` 建的（内部走 `add_slide`，执行映射看得见，文本扫描看不见）。两轮真实运行各 20 次
   构建返回，凡是没被 `polish=false` 关掉的，全部因此被拒。
   修法：`clone_page` / `adapt` 计入建页名单；计数改用词边界正则，且**点号不算边界**——第一版把
   前导点排除掉（为了不误数 `ppt_template.page(...)`），结果 `prs.slides.add_slide(LAY)` 一律不算，
   两份真实脚本改在第一个自绘页上被拒。现在两份脚本的准入检查都返回 `None`。

还有一条纯粹是返回文本的错：构建干净时首条 ask 是"逐页看图"，对已经看完的模型不是下一步，于是
它把同一份成品重建了 8 次。现在会说 deck 已交付在哪里、其余都是报告。（我自己一度把这句写成"去调
`ppt_publish`"——这条路线上没有这个工具，正式构建本身就是交付。）

### 2.z 精修第一次真正执行后看到的东西

单独驱动一次（run31 的 13 页成品，opus5）：**门禁 1 条 → 0 条**，字符 3930 → 3838。
它自己记下的整份改动是一套真东西：间距收敛成两个 token、全 deck 一种面板底色与一种线宽、
新增 `band()/rail()/stat()/kv()/split()` 打破"处处卡片"、字号事后钳制到 14pt 下限，并明确
"palette、Arial/微软雅黑 配对、28/24/18/16/14 阶梯保持不变"——它用上了接进 brief 的 house 数值。
逐页命中的都是肉眼能看出的：封面副标题在词中断行、目录页六个序号圈的橙白交替（对 1/4/5 的无故
强调）、图注离图 2in 且越过内容下边界、图被模板箭头压住、正文跑到 y=7.02（超 6.70）、
标题上的 `word_wrap=False` 让字冲出列宽。第 8 页它删掉了与表格重复的条形图（数字仍在表里，
但少了一个图形——记在账上）。

完整 agent 流程里（run35）也跑起来了，并暴露两件事：

- 一轮精修**把程序改坏了**：`KeyError: <PP_PARAGRAPH_ALIGNMENT.RIGHT: 3>`——`ppt_layout.write`
  的 `align` 收字符串，而写 python-pptx 的人自然写枚举。回滚机制正常（整轮撤回、恢复能构建的版本、
  finding 指明第 3 页），但一次拼写让整份 deck 丢掉其余每页的改进。现在两种写法都接受。
- 正式构建耗时 **982s**：13 页 × 每页一次带图的模型调用（并发 6）+ 渲染 + 重建 + 复测。
  这是真实成本，不是缺陷；但它意味着"正式构建"不适合被当作迭代手段用。

剩下的 blocking 是作者的：目录页克隆后没替换模板自带的 "Agenda"（`placeholder_copy` +
`template_underlay`），以及一页从未回看（`unseen_page`）。

### 2.w 逐页审阅一次，抓出四条门禁看不见的缺陷

两份成品（opus5 精修后 13 页、sol 12 页）逐页看完：**没有文字溢出，也没有文字压文字**——
`overset_copy` 与 `word_collision` 的结论与肉眼一致。看到的 11 类问题里有四类可量而当时无人测，
都已补上门禁并用 4–6 份真实 deck 校准：

| 门禁 | 眼睛看到的 | 校准结果 |
| --- | --- | --- |
| `crowded_panel` | 注释卡最后一行贴着面板下沿 | 只报下沿、每面板一条：32 处命中里 20 处是左右方向、全是设计自身内缩。opus5 精修版 4 条（p3 0.039in、p7 −0.004in…），旧模板 deck 0 条 |
| `orphan_line` | 目录页四个标签折成"为什么要统 / 一" | 改从渲染读——第一版用估算器预测折行，在一页评审认可的页面上误报了一个渲染图里没折的标题。sol p2 四处全中，opus5 零误报 |
| `unseparated_blocks` | 四项说明连成一片、表格末行贴着注释 | 跨形状 + 框内段落两种，且只报"新组开头贴紧上一组"（粗体或更大）：精修前 6 条、精修后 0 条 |
| `literal_escape` | 卡片上印着 `48.3\nOVIS` | 交付版是真换行、当前构建是字符——同一页两版只有文件能区分 |

**同时补掉一个存在很久的盲点**：模板照片常常不是 PICTURE，而是**带图片填充的自由形状**
（`PictureMisc1`，占页 27%）。`shape.image` 对它抛异常，于是 `template_picture` 从未看见过它、
`evidence` 把那页算成纯文字——用户两次问"模板的图为什么还在"，门禁一直答"没有"。geometry 里现在
有统一判据 `picture_blob`（只看形状自身填充，避免组合重复计数）。

**当前两份成品**（同版本渲染 + 全部门禁）：opus5 精修版 **4 条 0 blocking**（全是 `crowded_panel`）；
sol **24 条 4 blocking**——2 条标题压角标（`word_collision`）、2 条 `\n` 印成字符，外加标题越出画布
（`spilled_copy`，第 7 页首字母"T"确实丢了）、四处孤字、贴边与卡片溢出。

**记录但不建门禁**（属于设计判断）：三个平级条用三种饱和色；精修删掉与表格重复的条形图；
封面上模板的 ↗ 装饰读作游离符号。另外精修的 `notes` 是它的**决定**而非结果——它写"六个序号圈已
统一成一种强调色"，文件里那六个形状的填色根本没动。引用它的记录时要以文件为准。

### 2.aa 用户在 PowerPoint 里看到的四条，以及各自的机制（设计 D12–D14）

前面所有测量都是"渲染成 PDF 再量"，于是有一类缺陷本管线结构上看不见。用户在 PowerPoint 里
打开成品后提的四条，两条属于这一类，两条属于"引导写在文档里就等于没写"。

| 用户看到的 | 量出来是什么 | 机制落在哪 |
| --- | --- | --- |
| 表格没按要求画 | 五个表全带 python-pptx 的图库样式 `{5C22544A…}`（蓝底斑马），且 175 个单元格的边框元素按 `lnB, lnT, lnR, lnL` 写出，ECMA-376 的次序是 `lnL, lnR, lnT, lnB` | `tidy()` 每次构建收尾卸样式并按 schema 次序放回；`table()` 源头也写对 |
| "Click to add" 和它的框没删 | 13 页里 10 页留着空标题占位符，全在 (0.72, 0.14) 11.88×0.98in | `tidy()` 删掉既无文字、也不承载图片/表格/图表的占位符 |
| 公式没适配好 | p5 的 `掩码 logits = (F4, Q'inst)；分类 logits = …` 在"分类"后折行，下标全平 | `formula()`：整体不折行、真下标、变量斜体、字号沿阶梯降、floor 不够就在分号处断 |
| 几乎没用 icon | 两份 13 页 deck 调用 `add_icon` 各 **0** 次（唯一的 freeform 来自模板 p2） | `card()` 一次画出底面 + icon + 标题 + 正文并拥有那部分几何，带 icon 成为更省力的路径 |

**表格与占位符的验证**：在已交付的 opus5 原版 deck 上跑 `tidy()`——删 10 个占位符、卸 5 个表的
样式、重排 175 个单元格；重新渲染后与修正前**13 页像素级完全一致**（`ImageChops.difference`
的 bbox 全为 None），即这两项只影响 PowerPoint 看到的东西，不影响任何已有测量。护栏不是 tidy
的单测，而是 `test_a_built_deck_carries_neither_defect_a_render_cannot_show`：真跑一份含占位符与
表格的程序，断言交付文件里两条都不存在——任何绕过 tidy 的改动都会在这里失败。

**公式与卡片的验证**：三页对照实跑并渲染（`scratchpad/prim/`）。旧写法那页复现了交付页的断行；
`formula()` 那页三条公式的下标（`Q_out`、`F_4`、`T_clip`）全部下沉、变量斜体函数名正体、
无一处在符号内折行，且中间栏因栏宽不足自动在 `；` 处断成两行；`card()` 那页五张卡片的 icon
在标题左侧、取主题橙色、导出后仍是可编辑的描边矢量。

**精修改为默认关闭**（D13）：它删过 224 字并改写过三行，也是那 8 页色条的来源。
`tools.ppt.designer.enabled` 默认 false，`test_a_provider_alone_does_not_switch_the_design_pass_on`
锁住这一点——有 provider 不等于开精修。

## 3. 限制

1. **路线 B 与路线 C 未实现**。只有声明与能力约束，后端与阶段待做。
2. **精修环默认关闭**（设计 D13）。带真实模型跑过四次，结论是它让 deck 更差：删过 3420 字
   里的 224 字、改写过三行、给 8 页加过色条。机械保证已补（`copy_rejection` 与 band 门禁），
   但默认不开；`tools.ppt.designer.enabled = true` 打开。
3. `evidence_coverage` 的"≥4 个绘制面板算证据"这一支在 python-pptx 1.0.2 下不可达
   （凡 `.fill` 可解析的形状都报告有文本帧）。现状已被测试钉住，改动会改变校准过的行为。
4. **色带门禁只看顶层形状**，组内色带可逃过。参考版的零误报是在这个不对称下测出来的，
   放宽只会新增 findings。
5. `wrapped_labels` 目前用免字体的宽度估算（真实字形宽度的下界），比旧实现少报。
6. `type_floors` 忽略画布高度参数；5.625in 画布会被量高约三分之一。
7. 渲染服务无并发闸（有意：services 层无状态），限流在 `DeckViews`。
8. **交付路径旁路无法在工具内封堵**：作者若离开工具自行 `cp` 到交付路径，门禁看不到。
   需要评测/交付侧校验，属产品决定。
9. **技能文档未移植**。三条路线的 profile 各声明了一个 skill 名，但技能本体（旧仓库的
   `ppt-visual-authoring-expert/SKILL.md` 406 行 + `AGENTS-ppt-build.md` 158 行）尚未落地。
   旧版这两份文档里有 8 组规则各重复 4–6 次表述，合并时应让每条规则只在门禁代码里有唯一
   权威表述、文档只引用——设计环的两份 brief 已经这么做了（字号数字从测量常量格式化进去）。
10. **交付文件名不可由作者指定**。`ppt_build` 不再暴露输出路径参数，名字来自
   `tools.ppt.deckName`。这是有意的（发布只允许落在 profile 决定的位置），但如果任务书
   指定了文件名，需要配置侧对齐。
11. **版式上的装饰对所有校验不可见**。母版与版式自己画的东西不会进 `slide.shapes`，
   所以读构建产物的校验（重叠、色带、词框）看不见它。两个后果同时成立：用户模板自己的
   设计不会被门禁误拒；而文字压在模板的插画上时，没有任何测量会报重叠——实测渲染里已经
   出现过（见 §2 模板路线）。这是模板路线目前最实际的缺口。

## 3.5 旧 fork 有、本仓库如何接的

| 能力 | 旧 fork 里是什么 | 现状 |
| --- | --- | --- |
| **图片搜索** | `web_search(kind="images")` 走 Serper 图片面，带 `min_width` 过滤（窄于 ~640px 的图在 1280 画布上半幅就已发虚），每条结果带尺寸与来源页以便判断能否署名 | **已接入**，作为上游 `web_search` 的一个面而不是新工具 |
| **素材抓取** | `ppt_fetch_asset`：把网上的 PDF / 图片 / 文本 / PPTX 下载进项目 | **已接入**为 `ppt_fetch`（32MB 上限、magic-number 嗅探而非信 Content-Type）。文档与图片落进 materials 续接 `ppt_ingest`；`.pptx` 直接绑定为模板 |
| **用户模板** | `template_*` 子系统（约 3600 行）：净化 pptx → vendor `analyze` 抽 region/slot/capacity → 只向模型暴露 slot ID 与容量 → 语义闸 → vendor `check-plan` → `apply` → XML 后处理 → overlay 合并 | **换了一条路接入**（`services/template`，1100 行）。slot/capacity 那套在本仓库里写过又删了：容量只能从占位符文字猜（模板基本不声明字号），角色只能从版式名猜（实测 30 份模板几乎全叫 `Title Only`——那是某个导出器的特性，不是通用事实），而且它把页面上限锁死在模板自己那页装得下多少——模板画 6 张卡就做不了 8 个点。改成"把模板页反编译成 python-pptx 给模型参考改写"，加上模型写不出来的那部分用克隆改写 |

**"用户给了模板就用模板"现在有入口**：`ppt_template(project, path=...)` 绑定，
`ppt_template(project, pages=[...])` 取示例页代码；`ppt_fetch` 下到 `.pptx` 时走同一条绑定。
构建时程序拿到 `PPT_TEMPLATE`（清空示例页的副本，加页即继承母版/主题/画布）与
`PPT_TEMPLATE_SOURCE`（还留着示例页的原件，克隆用），克隆改写的四个操作作为
`ppt_template.py` 写进构建目录。设计环在有模板时会被告知调色板与字体是用户的、不归它改。

## 4. 待办

| 项 | 归属 |
| --- | --- |
| 路线 B 移植（保留不可替代核心：`layouts` 闭环预算、`text_fit`、`schema`、`svg_primitives` 方言、vendor 边界） | S10 |
| 路线 C 实现（`imagegen` 服务、`background`/`place_text` 阶段、安全区契约） | S11 |
| 带真实模型的端到端 | S9b |
| harness 层分流（有无模板 / 有无素材 / 图够不够 / 大纲全不全 / 指令全不全） | 待定 |
| `Finding` 补 `finding_id` 与 `repair_route`，让"修了三次还在"可查询 | 设计环 |
| `LADDERS`/`DENSE_LADDERS`/`FONT_FLOORS_PX`、画布与安全区常量、`lint_title_casing`、`legibility_note` 目前无人接手 | 随 S10 |
| profile 声明式 severity 分级（目前 profile 只能过滤不能改级） | S8b |
| `caps_phrases` 持久化体积（词数 × 5 个窗口） | 评估 |
