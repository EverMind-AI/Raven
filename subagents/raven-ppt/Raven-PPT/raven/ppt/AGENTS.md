# `raven/ppt/` 开发准则

本文件只定义 PPT 能力开发的**长效规则**，作用域是本目录。架构、分层与决策写入
`docs/ppt-raven-design.md`；当前能力与验证证据写入 `docs/ppt-raven-status.md`。
禁止把单次开发的过程记录写入本文件。

上游协作规范（注释、分支命名、Conventional Commits、PR 模板）以仓库根 `AGENTS.md` 为准；
冲突时本文件针对 PPT 的补充条款优先。

## 边界

PPT 能力全部位于本目录，分层为
`contracts ← services ← backends ← stages ← profiles ← tools`。
依赖方向、"不得触达 agent loop / TUI"、"vendor 只经适配层" 三条约束由
`tests/ppt/test_layering.py` 静态强制——**改动边界要先改那个测试**，不要绕过它。
各层职责见设计文档 §2。

对上游的改动收敛到工具注册这一处装配点。除此之外不修改上游模块；
需要上游能力时先确认上游是否已提供（例如返图走上游 `ToolResult.blocks` 与
provider 的 `supports_image_tool_result`，不要另造一套）。

## 硬不变量

1. **模型不写物理量**：不暴露物理坐标/尺寸/字号/字体字段。受限例外是归一化画布坐标，
   由 profile 的 `Capabilities.normalized_regions` 声明开放；`physical_geometry` 与
   `font_size` 在任何 profile 下恒为 False，由 `Profile.__post_init__` 与
   `tests/ppt/test_profiles.py` 双重强制。
2. **保证在代码里**：页数、字符预算、无重叠由工具执行路径强制，
   prompt 只做引导。
3. **测量回灌，不硬拒**：字号下限、越界、重叠以 WARNING finding 进下一轮设计调用。
   理由见设计文档 D2。
4. **fail-closed 发布**：存在 BLOCKING finding 时拒绝发布。
5. **降级显式留痕**：禁止静默截断半句，禁止把字号降到下限以下。
6. **vendor 隔离**：固定上游 commit，改动以补丁记录于 vendor 目录的 `PATCHES.md`。
> 事实锚定曾是本节的一条，由一个确定性门禁强制。该门禁实测假阳性过高（全大写普通词被拒、
> `A100,480p` 被读成千分位而拒掉三页），与 #7 直接冲突，已移除；解除该条不变量经维护者明确
> 同意。数字能否溯源现在没有代码层强制，只由 prompt 引导 —— 这是一个已知且被接受的缺口，
> 不是遗漏，不要按缺陷重新提报。

7. **工具不给死胡同**：拒绝一个提交时必须给出可执行的下一步。作者被拒到无路可走会离开
   工具自行交付，那条路上没有任何门禁（设计文档 D6）。

## 流程

1. 改前先读相关接口、实现、测试，确认所有权边界；采用能完整满足需求的最小改动。
2. 每段移植/每阶段完成后本地提交；push 需用户明确指令。
3. 并行开发只经 worktree 分支进行，合回主重构分支；不得两个执行体写同一分支。
4. 仓库可能存在他人改动；不得回退、覆盖或整理与当前任务无关的修改。

## 测试

```bash
uv run ruff format --check raven tests && uv run ruff check raven tests
uv run pytest tests/ppt -q          # 分层 + 契约 + 各层单测
uv run pytest tests/ -q             # 全量
```

- 单元测试默认无网络、无真实模型调用、不拉起容器；libreoffice/playwright 缺失时 skip 并
  标注前置条件。
- 每个 service 自带属性测试：文本适配的溢出、pairwise 无重叠、降级链每步留痕。
- 每个 profile 有一条"能力声明未被放宽"的断言。
- 工具层测 schema 拒绝路径、返图 shape、门禁短路与结构化错误格式。

## 安全与仓库卫生

- 凭据只经环境变量读取；端点与 key 不进入源码，配置样例只写占位符。
- 生成产物（pptx/png/pdf/归档/venv）不得进入提交；测试 fixture 保持最小。
- 不使用破坏性 Git 命令处理用户工作区；停自研进程时按完整命令行特征匹配。
- 评测集的 case 级 judge prompt / golden 内容不得进入代码、prompt、技能或 fixture。
