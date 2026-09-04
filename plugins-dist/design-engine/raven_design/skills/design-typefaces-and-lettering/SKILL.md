---
name: design-typefaces-and-lettering
description: "为字体家族、有限字集字体、定制字标与一次性 lettering、现成字体选型建立专业路由、权威母版、领域门控和有边界的验证声明。适用于创建、扩展、修复或审计字形结构、间距、字符覆盖、OpenType shaping、字体构建、真实消费者与现场可读性的任务。"
---

# 设计字体与字形

本 Skill 是 `$visual-artifact-design` 之上的字体领域层：共享底座负责通用证据、工具事实、review、
渲染与晋升；这里决定字体任务如何分路、何时返回，以及证据最多支持什么声明。

按需读取 [领域模式与检查](references/patterns.md) 和 [专业工具能力档案](references/tool-profiles.md)。本层保持介质中立：先由合同确定最终消费者与物理或数字介质，再选择生产和证据路径；不得把 HTML、浏览器或任一导出格式设为默认终点。

### 统一合同边界

- **最终消费者：** 指实际接收最终交付的具名应用、设备、Web 环境、印刷或制造流程。证据必须来自该消费者的真实加载、排版、渲染或生产结果。非网页媒介不能用浏览器证据冒充目标消费者；浏览器最多是预检或 review index。只有最终消费者是 Web 时才调用 `$build-polished-visual-frontends`。
- **来源与许可：** 每项输入字体、字形、文字、语料与辅助资产都记录来源、版本或 hash，以及修改、嵌入和再分发边界；外部字体二进制仍是 upstream source，不因进入 specimen 或查看器而成为母版。
- **字体母版与消费者：** 字体原生工程、UFO/designspace、SFD 或合格参数源拥有相应字体 authority；字标由可编辑原生矢量文档拥有固定构图 authority。消费者输出、网页、截图和 Gallery 只提供派生证据，不得反向改写母版。

## 1. 锁定四条主路由

| `route` | 权威交付 | 明确不自动承担 |
| --- | --- | --- |
| `font_family` | 可编辑多样式/多母版源、家族关系、构建字体；静态或变量 | 无用途的字重、轴、语言和平台 |
| `finite_charset_font` | 精确闭集内可输入、可安装的字形源与字体 | 完整语言、正文质量、完整家族 |
| `wordmark_lettering` | 已校文固定文字的可编辑完整词形与生产输出 | cmap、输入行为、额外字符；品牌策略由品牌领域负责 |
| `font_selection` | 现成字体的授权候选、受控比较、目标消费者部署决定 | 新字形原创、轮廓所有权或字体家族完成声明 |

执行模式统一使用共享 `work_mode: Create | Edit | Diagnose`，不另造领域模式。`extend` 与 `repair` 是 `Edit` 的动作子型；`audit` 映射为 `Diagnose/Audit`。固定文本却要求键盘输入时走字体路由；未创建轮廓、只决定采用哪款字体时走选型路由。主要问题是版式、品牌架构或图形符号时转交对应领域，本 Skill 只保留字体接口。

## 2. 先写合同与 authority graph

开工前记录：路由与模式、当前 `build_hash`、原文或字符范围、书写系统/语言/特性、样式关系、目标消费者、尺寸/介质/环境、许可边界、风险与 `excluded_claims`。

为下列实际 concern 各指定且只指定一个权威 owner：`glyph_geometry`、`spacing_metrics`、`coverage_shaping`、`family_model`、`build_config`、`fixed_composition`、`selection_decision`、`proof_index`。不适用项写带范围理由的 `not_applicable`，不要留第二真源。

- 可安装字体的母版必须可审阅、可逐字修改并可重建；工具原生字体工程、UFO/designspace 或满足下述条件的参数生成器均可直接拥有对应 authority。
- 参数生成器只有在确定性生成、暴露可检查中间轮廓、保存逐字 override、全部人工修正可回写时，才可拥有 `glyph_geometry`；否则它只是探索或初始化工具。
- OTF、TTF、WOFF2 与纯导出 SVG/PDF/EPS、截图和校样页默认是 derived output。只有显式记录 authority transfer 或有界 fork、退役同一 concern 的旧 owner、建立可编辑原生工程并完成从该工程重建的证明后，承接的新工程或 fork 才可接管该 concern；合同明示或文件可打开本身不构成 transfer。字标可让原生矢量文档直接拥有 `fixed_composition`，但其生产导出仍是派生物。
- 现成字体文件及其许可是选型输入；比较矩阵可拥有 `selection_decision`，不能反向成为字体轮廓母版。

## 3. 使用 Tool Registry 选择能力，不按品牌套风格

按 `任务子型 → 所需能力 → Registry 候选 → 选择理由 → 原生模型 → master-of-record → 自定义边界 → 使用证据 → 失败返回` 记录选择。先查共享 Registry，再查 [tool-profiles.md](references/tool-profiles.md)；candidate id 不是使用事实。

- 未登记或未探测的候选记为 `considered` 且 availability 为 `unknown`；探测确认不可执行才是 `unavailable`，需要真人操作才是 `human_handoff`。
- 只有依赖、版本、许可、调用/导入、可编辑母版、重建/导出、当前消费者或最终像素七类证据齐全，才可写 `used`。
- 一旦选择工具，就用其原生字形/母版/特性/构建模型组织作品；不能只借一个功能，其余另写成无 authority 的平行系统。
- 商业、GUI 或未安装工具不得伪装为自动化成功；环境未安装也不等于工具没有能力。
- 自研前必须执行 capability probe；只有工具在目标输入上真实失败且替代项也不满足，才记录 gap、最小自定义边界和一致性证据。自研不得吞并工具已能可靠完成的部分。
- proof 写权限只依据统一 `work_mode`：若专业工具 proof 需要写入，`Create/Edit` 只可在授权 workspace 制作一次性验证副本，不得触碰母版、canonical 或 promotion record；`Diagnose/Audit 一律零写入`。proof 与最终交付分别记录输入、输出 hash；proof 只验证能力链，不能改变 canonical/promotion 状态。

## 4. 八道领域 gate

每道 gate 都按共享 schema 记录 `gate_id`、是否 required、输入、动作、当前构建证据、claim ceiling 与 failure return。第一个失败的 required gate 决定返回位置；下游文件存在不能越过上游失败。

### `type-route-contract`

- **输入/动作：** 把 brief、准确文本/字符、用途、消费者、权利与风险锁成四路之一，写支持项和排除项。
- **晋升证据：** 可执行输出合同、范围清单、风险驱动验证计划及责任人；信息未知时必须显式 unknown。
- **失败返回/上限：** 返回需求澄清或领域转交；通过前只能声称“问题已识别”。

### `type-tool-authority`

- **输入/动作：** 由能力需求选择 Registry candidate，锁定原生模型、唯一母版、派生链和必要 handoff。
- **晋升证据：** 选择理由、availability probe、authority graph、工具状态；参数源另有逐字修正与审阅路径。
- **失败返回/上限：** 返回工具选择或 source governance；通过不等于工具已使用，也不支持完成声明。

### `type-control-forms`

- **输入/动作：** 按书写系统、语料和失效代价选控制字形/完整词形/候选对照，修骨架、黑白形、端点、接合与易混组。
- **晋升证据：** authority source 中的可编辑形态、同条件对照、目标尺寸预检；核心区分不得只依赖默认关闭的 feature。
- **失败返回/上限：** 返回 `glyph_geometry` 或选型候选集；通过前不得声称范围内视觉系统、辨识性或字体设计成立。

### `type-spacing-composition`

- **输入/动作：** 字体修 sidebearing、advance、kerning、锚点和行框；字标修完整词形；选型在同文本、字号、行长与设置下比较。
- **晋升证据：** 真实词句/数据/固定文本的同步样张、度量解析及问题组合；不能用全局 tracking 隐藏源问题。
- **失败返回/上限：** 结构问题回控制字形，否则回 spacing authority；通过前不得声称排版节奏或生产组合可用。

### `type-coverage-shaping`

- **输入/动作：** 字体对账声明字符、cmap、非空轮廓、script/language/features 与目标 shaper；选型核对授权覆盖、fallback 和实际输入。
- **晋升证据：** 精确范围清单、编译结果、真实 shaping 序列/定位和缺字行为；多文字系统记录合格语言审阅者状态。
- **失败返回/上限：** 回字形源、feature/coverage 配置或候选选择；只能声明已证实的字符、语言、特性和引擎。固定 lettering 可带范围理由 N/A。

### `type-build-delivery`

- **输入/动作：** 从唯一母版干净重建；字体检查表、命名、版本、家族/轴和输出，lettering 检查原生源与生产导出，选型检查获取、许可与部署映射。
- **晋升证据：** 锁定依赖、真实调用、构建/导出日志、可解析输出、hash 与 source-to-output 对应；家族另证插值风险点。
- **失败返回/上限：** 回 authority 或构建配置；通过前不得声称可交付、可安装、可重建或发布候选。

### `type-real-consumer`

- **输入/动作：** 在合同列出的真实应用、OS/browser、shaper/rasterizer 或生产软件中使用当前构建，覆盖默认、关键 feature、fallback 和失败状态。
- **晋升证据：** 消费者版本、输入参数、未缩放输出/像素、安装或导入记录及重建后回归；校样网页不能替代目标消费者。
- **失败返回/上限：** 消费端映射错回构建，字形/间距错回对应 gate；只能声明实际通过的消费者与条件。

### `type-field-conditions`

- **输入/动作：** 仅在物理尺寸、距离、光照、材料、加工、长期阅读或安全可读性属于 claim 时，执行真实介质、观察者或现场验证；模拟只作 preflight。
- **晋升证据：** 当前 build 对应的实物/设备参数、原始观察或测量、适格审阅人与限制；无现场 claim 时写清范围后 N/A。
- **失败返回/上限：** 回控制形、spacing、输出工艺或缩窄合同；未做真实验证时禁止“现场可读、实体可靠、安全适用”等声明。

Specimen 的色条、边框、背景场、字母装饰和 CSS 包装只在承担排版条件、对照分组或已批准身份语法时
保留；删除后若字形、间距、覆盖、选择结论和消费者行为不变，就不是字体证据。展示外壳不能替轮廓与
间距制造完成感，也不能用“背景图 + 半透明文字板”掩盖阅读问题。来源与限制邻近受影响样张并保持
可读，但视觉权重不得压过正在验证的文字。

## 5. Claim ceiling 与晋升

- `font_family` 最多声明已验证的样式关系、字符/语言、构建格式和消费者；未验轴区间、平台或语言一律排除。
- `finite_charset_font` 最多声明闭集、特性和目标流程；不得扩成正文、完整语言或完整家族证据。
- `wordmark_lettering` 最多声明已校文固定词形在已测尺寸/工艺中的质量；不得支持输入字体、额外字符或品牌策略。
- `font_selection` 最多声明具名候选在相同内容、许可与消费者条件下的选择结论；不得支持原创、轮廓质量或范围外授权。

Gate 通过、artifact lifecycle、canonical release、assurance 与 few-shot pool 相互独立。`SELF_REVIEW_ONLY` 只支持当前构建的技术/协议自检；视觉、语言文化、阅读、品牌与现场 claim 仍未独立验证。`internal-positive` 必须绑定 scope、build 与 `positive_for / not_evidence_for / excluded_claims`；`externally-validated` 还需非作者的相关专业审阅覆盖同一 claim，不得整包背书。

## 6. 停止与转交

- 文本、语言、书写传统、许可或商标不明：停在已验证范围，转母语/文字专家、权利方或品牌负责人。
- 所选商业/GUI 工具不可自动执行：保留 `human_handoff`，交付明确输入与期望的原生母版/证据，不伪造调用。
- 缺目标消费者：最多交可构建候选；缺真实现场证据：最多交 simulation/preflight，不提高 field claim。
- 新增字符、语言、样式、轴、消费者或介质会改变 gate 计划时，先更新合同再继续。
- CSS、样张背景或装饰框看起来精致，不能补偿字形、间距、coverage、shaping 或选型失败。
- 达到合同范围与证据上限即停止；不要为显得完整而自动扩成家族、网页包、海报或 Gallery 外壳。
