# `raven/ppt/` 开发准则

本文件只定义 PPT 能力开发的**长效规则**，作用域是本目录。架构、分层与决策写入
`docs/ppt-raven-design.md`；当前能力与验证证据写入 `docs/ppt-raven-status.md`。
禁止把单次开发的过程记录写入本文件。

上游协作规范（注释、分支命名、Conventional Commits、PR 模板）以仓库根 `AGENTS.md` 为准；
冲突时本文件针对 PPT 的补充条款优先。

## 边界

PPT 能力全部位于本目录，分层为
`contracts ← services ← backends ← stages ← profiles ← tools`。
依赖方向与"不得触达 agent loop / TUI"由 `tests/ppt/test_layering.py` 静态强制——**改动
边界要先改那个测试**，不要绕过它。各层职责见设计文档 §2。

同一个测试还有一条 vendor 适配层规则，但 `raven/ppt/vendor/` 已不存在（无目录、无
`PATCHES.md`、无 `VENDOR.md`），那条断言现在恒真。要么重新引入 vendor，要么删掉它——
不要把它当成生效中的保证。

对上游的改动收敛到工具注册这一处装配点。除此之外不修改上游模块；
需要上游能力时先确认上游是否已提供（例如返图走上游 `ToolResult.blocks` 与
provider 的 `supports_image_tool_result`，不要另造一套）。

## 硬不变量

1. **工具 schema 不收物理量**：暴露给模型的**工具参数**里没有物理坐标/尺寸/字号/字体
   字段。`Capabilities.physical_geometry` 与 `font_size` 在任何 profile 下恒为 False，
   由 `tests/ppt/test_profiles.py` 强制（只此一处；`Capabilities` 没有
   `__post_init__`，别照旧文当成双重强制）。受限例外是归一化画布坐标，由
   `normalized_regions` 声明开放。
   **这条不管脚本作者路径。** 作者写的是 python-pptx 程序，`ppt_layout` 给它英寸盒子
   （`Box`、`stack(0.36)`）、磅值阶梯（`size=BODY_PT`）和字面名（`font=F`），技能里有
   十几处这样的调用——那不是违规，是那条路径的前提：物理量由作者写、由引擎测量并回灌。
   要判断某个改动是否越界，看的是它进不进**工具 schema**。
2. **保证在代码里**：页数（`page_budget`）、语言（`language`）、图注引用（`citation`）、
   未落地的图（`unplaced_figure`）、页与代码的对应（`unmapped_page`）、模板风格
   （`house_style`）、渲染出的文字重叠与遮挡（`word_collision` / `covered_shape`）由工具
   执行路径以 BLOCKING 强制，prompt 只做引导。权威清单是 `profiles/registry.py` 的
   `blocking_kinds` 与各 finding 自己的 `severity`，不是这份列表。字符预算与事实锚定
   **不在此列**：字符上限从未在这套代码里存在，逐字比对数字的 `fact` 门禁已删（设计 D3a）。
3. **测量回灌，不硬拒**：字号下限、越界、溢出、折行等测量以 WARNING finding 随渲染图
   回给作者，进它的下一次构建（没有第二个阶段接手——设计 D18）。理由见设计文档 D2。
   **渲染实测的文字重叠与遮挡是这条明写的例外**，BLOCKING：见 D2 与 D11。
4. **fail-closed 发布**：存在 BLOCKING finding 时拒绝发布。
5. **降级显式留痕**：禁止静默截断半句，禁止把字号降到下限以下。
6. **工具不给死胡同**：拒绝一个提交时必须给出可执行的下一步。作者被拒到无路可走会离开
   工具自行交付，那条路上没有任何门禁（设计文档 D6）。

## 流程

1. 改前先读相关接口、实现、测试，确认所有权边界；采用能完整满足需求的最小改动。
2. 每段移植/每阶段完成后本地提交；push 需用户明确指令。
3. 并行开发只经 worktree 分支进行，合回 `feat/ppt`；不得两个执行体写同一分支。互不依赖
   的改动**默认并发**：冲突判据是文件级（先 `rg -l` 查各任务要碰的文件有没有重叠），
   触及全部调用点的改动（构造函数签名一类）留着自己顺序做。
4. 仓库可能存在他人改动；不得回退、覆盖或整理与当前任务无关的修改。

## 测试

```bash
uv run --frozen --python 3.12 --all-extras ruff format --check raven tests
uv run --frozen --python 3.12 --all-extras ruff check raven tests
uv run --frozen --python 3.12 --all-extras pytest -q tests/ppt/test_<改动涉及的>.py  # 迭代中
uv run --frozen --python 3.12 --all-extras pytest -q tests/ppt   # 提交前：分层 + 契约 + 各层单测
uv run --frozen --python 3.12 --all-extras pytest -q tests       # 全量，仅在跨包改动时
```

裸 `pytest` 不算跑过：它会挑到别的解释器或缺 extras 的环境，结果不可比。

改一处跑一次 `tests/ppt` 是浪费——那是两千五百多条、约 77 秒。迭代中只跑改动符号被覆盖的
那几个文件（`rg -l <符号> tests/`），目录级留到提交前。

- 单元测试默认无网络、无真实模型调用、不拉起容器；libreoffice/playwright 缺失时 skip 并
  标注前置条件。
- 每个 service 自带自己的测试：文本适配的溢出、渲染实测的词重叠与 z-order 遮挡、
  helper 让出了什么（图表丢掉的标注、公式降到的字号）每步都说出来。
  "按文件两两比对文本框重叠"是刻意否决的判据（设计 D11），不要按它写测试；
  事实门禁已不存在，不要为它写测试。
- 每个 profile 有一条"能力声明未被放宽"的断言。
- 工具层测 schema 拒绝路径、返图 shape、门禁短路与结构化错误格式。

## 安全与仓库卫生

- 凭据只经环境变量读取；端点与 key 不进入源码，配置样例只写占位符。
- 生成产物（pptx/png/pdf/归档/venv）不得进入提交；测试 fixture 保持最小。
- 不使用破坏性 Git 命令处理用户工作区；停自研进程时按完整命令行特征匹配。
- 评测集的 case 级 judge prompt / golden 内容不得进入代码、prompt、技能或 fixture。
