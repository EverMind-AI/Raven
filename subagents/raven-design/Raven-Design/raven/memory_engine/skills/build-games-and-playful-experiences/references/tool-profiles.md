# 二维游戏专业工具能力档案

本文件记录领域候选与已知 Registry canonical ID，不缓存“已登记”“未登记”或 availability。每次任务必须查询当前共享 Tool Registry 与环境 probe；只有 candidate ID 的档案按原 ID 查询，缺项不能据此声称可用或已使用。

Registry 查询或环境 probe 不通过时记 `candidate_unavailable`，正式 tool-use record 映射为 `unavailable`；需要商业席位、GUI 或人工操作时记 `human_handoff`。环境不可用不能证明工具缺能力，也不能授权手写替代。

每个被选工具的真实使用证据都必须包含 dependency、解析版本、license、真实 invocation、对应 concern 的 editable master、从母版 rebuild/export、当前 final pixels 或 consumer evidence。每档下面再列领域专属证据。

一个项目通常选择一个主运行时，再补充必要的关卡、叙事、视觉、音频和测试工具。运行时的场景/数据/生命周期是工程语言；视觉语言由资产母版和领域表现规则拥有，不能由框架品牌代替。

## 1. `phaser-runtime`

- **任务子型**：浏览器二维规则、动作、益智、轻量叙事、节奏和本地多人。
- **需要能力**：场景生命周期、精灵/相机、输入、时间、补间、音频、缩放及适用的碰撞/物理。
- **候选工具 / candidate ID / Registry canonical ID**：Phaser；atomic profile `phaser-runtime`。
- **原生模型**：Scene、Game Object、Loader、Input、Time、Tween、Sound、Scale、Physics 与插件边界。
- **Authority master**：TypeScript 工程、场景/规则源、锁文件、内容数据和资产 manifest；分发包是派生物。
- **为何选**：浏览器交付需要完整二维游戏生命周期，能避免重写成熟的场景、输入、时间和资源基础设施。
- **浏览器 proof 组合**：需要完整浏览器构建、公开输入和像素验收时，可查询 `phaser-browser-proof-pipeline`；该 pipeline 只编排 atomic runtime 与浏览器验证，不能取代 `phaser-runtime` 对场景和运行时 concern 的 authority。
- **何时不选**：单屏微玩具没有引擎净收益；主要难点只是高密度渲染；需要编辑器驱动多平台工程。
- **替代**：浏览器 Canvas/WebAudio 平台、PixiJS 或 Godot 2D，按能力重新选择。
- **真实使用证据**：共通七类，加 Phaser 真实 import、Scene 转移、原生输入/时间/暂停、生产构建及所声明内容迭代的重建轨迹。

## 2. `pixijs-2d-renderer`

- **任务子型**：互动玩具、生成体验、高对象密度二维场景和视听实验。
- **需要能力**：显示树、纹理、矢量图元、mesh/filter、ticker、render texture 与捕获。
- **候选工具 / candidate ID**：PixiJS；`pixijs-2d-renderer`，运行时按此 ID 查询 Registry。
- **原生模型**：Application/Renderer、Container、Sprite、Graphics、Mesh、Filter、Ticker、Texture。
- **Authority master**：TypeScript 渲染工程、生成参数/seed 数据、着色/滤镜源与资产 manifest。
- **为何选**：主要复杂度是二维渲染和生成表现，而不是完整游戏生命周期。
- **何时不选**：需要大量场景、物理、关卡、音频和会话状态；否则会在 PixiJS 外重新发明引擎。
- **替代**：Canvas 2D、Phaser；编辑器或多平台需求改选 Godot 2D。
- **真实使用证据**：共通七类，加 PixiJS 显示树/纹理/滤镜真实调用、种子复现、结构化变体、压力帧记录和捕获/导出路径。

## 3. `godot-2d-runtime`

- **任务子型**：多场景叙事探索、物理、动画树、本地多人、编辑器驱动关卡和多平台二维交付。
- **需要能力**：场景编辑、节点组合、资源、信号、InputMap、动画、音频总线、存档和导出 preset。
- **候选工具 / candidate ID**：Godot 2D；`godot-2d-runtime`，运行时按此 ID 查询 Registry。
- **原生模型**：Node/Scene、Resource、Signal、InputMap、AnimationPlayer、AudioBus、TileMap 与 export preset。
- **Authority master**：`project.godot`、`.tscn`、`.tres`、脚本、源资产和导出 preset。
- **为何选**：可视编辑、资源复用或目标平台使完整引擎的项目模型产生净收益。
- **何时不选**：单屏网页微游戏；目标渠道无法消费导出；工程只借编辑器摆放却绕开 Scene/Resource。
- **替代**：Phaser 加独立内容工具，或浏览器平台路线。
- **真实使用证据**：共通七类，加实际编辑器/CLI 版本、工程重开、原生 Resource 修改、目标渠道导出、InputMap 与存档回归。

## 4. `web-platform-canvas-audio`

- **任务子型**：对象少、状态简单、单屏的二维微游戏、互动玩具或程序化图形。
- **需要能力**：语义 DOM、Canvas/SVG 绘制、统一时钟、输入、Web Audio 与可访问状态桥接。
- **候选工具 / candidate ID**：浏览器 Canvas 2D、SVG、Web Audio 平台 API；`web-platform-canvas-audio`，运行时按此 ID 查询 Registry；这是 platform profile，不是第三方工具。
- **原生模型**：DOM/SVG scene、Canvas immediate mode、`requestAnimationFrame`/高精度时钟、AudioContext 和显式状态机。
- **Authority master**：模块化 HTML/CSS/TypeScript、规则/内容数据、SVG/音频源和构建配置。
- **为何选**：范围足够小，平台原语已覆盖合同，引擎只会增加体积和层次。
- **何时不选**：需要场景管理、资源加载、相机、复杂输入、物理、补间或音频总线；单文件膨胀是返回信号。
- **替代**：Phaser、PixiJS 或 Godot 2D。
- **真实使用证据**：共通七类，加具体浏览器 API 调用、支持环境、公开输入、确定性重建，以及 probe 证明没有重写成熟引擎能力。

## 5. `tiled-level-authoring`

- **任务子型**：基于房间、tile、对象层、碰撞层、实体或 world 文件的二维游戏与叙事探索。
- **需要能力**：空间编辑、图层/对象语义、自定义属性、碰撞/导航标记和稳定导出。
- **候选工具 / candidate ID**：Tiled Map Editor；`tiled-level-authoring`，运行时按此 ID 查询 Registry。
- **原生模型**：tile layer、object layer、tileset、template、world、custom property 与 TMX/TMJ。
- **Authority master**：`.tmx/.tmj`、tileset、template 和源资产；运行时 JSON/二进制是派生物。
- **为何选**：关卡需要反复编辑、语义对象和可追踪导入，不应退化为代码中的坐标常量。
- **何时不选**：固定单场景、纯程序生成、Tiled 模型无法表达所需拓扑，或 Godot TileMap 已拥有同一 concern。
- **替代**：LDtk、Godot TileMap/Resource，或经 schema 验证的非空间内容数据。
- **真实使用证据**：共通七类，加 Tiled 真实重开、层/对象/属性语义、运行时导入、碰撞/实体映射和可见改动 round-trip。

## 6. `ink-narrative-authoring`

- **任务子型**：对话密集、条件分支、变量、关系状态、检查点和本地化驱动的叙事探索。
- **需要能力**：段落、选择、条件、变量、跳转、编译和运行时状态恢复。
- **候选工具 / candidate ID**：inkle Ink/Inky；`ink-narrative-authoring`，运行时按此 ID 查询 Registry。
- **原生模型**：knot、stitch、choice、divert、variable、function、tag 与编译后的 story state。
- **Authority master**：`.ink` 源、变量契约与本地化源；编译 JSON 是派生物。
- **为何选**：分支结构需要独立编辑、编译检查和稳定运行时导入，而不是散落在场景脚本。
- **何时不选**：文本极少、价值主要来自环境空间、Ink runtime 无法接入目标引擎。
- **替代**：Yarn Spinner、Godot Resource 或经 schema 验证的节点数据。
- **真实使用证据**：共通七类，加 Ink 编译器/Inky 实际调用、分支覆盖、变量/存档恢复、运行时导入及源码到当前 build 的映射。

## 7. `aseprite-pixel-authoring`

- **任务子型**：像素精灵、逐帧角色/对象动画、palette 管理和 sprite sheet 切片。
- **需要能力**：frame、layer、tag、palette、slice、onion skin 和 atlas metadata 导出。
- **候选工具 / candidate ID**：Aseprite；`aseprite-pixel-authoring`，运行时按此 ID 查询 Registry。
- **原生模型**：sprite/canvas、layer/cel/frame、tag、palette、slice 与 timeline。
- **Authority master**：`.aseprite` 文件及源 palette；PNG、sheet 和 JSON metadata 是派生物。
- **为何选**：像素资产的帧、palette 和切片语义需要可编辑母版，便于保持轮廓与动画连续性。
- **何时不选**：分层绘画应选 Krita；可缩放几何应选 Inkscape；程序图形已有更清晰参数 authority。
- **替代**：Krita 用于分层光栅与绘画，Inkscape 用于矢量，或经 probe 允许的最小程序资产。
- **真实使用证据**：共通七类，加 Aseprite 实际 GUI/CLI、帧/tag/slice 母版、导出 metadata、运行时引用和隔离副本的可见改动重建。

## 8. `inkscape`

- **任务子型**：可缩放二维对象、图标、路径动画源、HUD 图形和需保留几何语义的资产。
- **需要能力**：object/layer、path、symbol、clone、style、viewBox、画板与结构化 SVG 导出。
- **候选工具 / candidate ID / Registry canonical ID**：Inkscape；atomic profile `inkscape`。
- **原生模型**：SVG object tree、layer、path、group、symbol/clone、style、gradient 与 document geometry。
- **Authority master**：Inkscape 可编辑结构化 SVG；优化 SVG、PNG 和 atlas 是派生物。
- **为何选**：资产需要可缩放几何、对象复用和可追踪样式，而不是扁平位图。
- **何时不选**：像素帧应选 Aseprite；复杂分层绘画应选 Krita；运行时高密度动态图元可由 PixiJS/Canvas 持有。
- **替代**：Krita、Aseprite、引擎原生矢量图元或经 probe 允许的程序几何。
- **真实使用证据**：共通七类，加 Inkscape 实际 GUI/CLI、对象/图层母版、结构化导出、运行时尺寸映射和可见改动 round-trip。

## 9. `reaper-audio-production`

- **任务子型**：动态音乐、节奏、循环环境、多轨效果、语音、bus 混音和 stem 导出。
- **需要能力**：tempo map、track/item/take、bus、region、自动化、render matrix、循环点和响度控制。
- **候选工具 / candidate ID**：REAPER；`reaper-audio-production`，运行时按此 ID 查询 Registry。
- **原生模型**：project、track、item/take、FX chain、routing/bus、tempo marker、region 和 render matrix。
- **Authority master**：`.rpp`、无损源素材、stem/谱面与 render preset；压缩 runtime 文件是派生物。
- **为何选**：同步、多轨或混音复杂度需要可编辑时间线、路由和可重复导出。
- **何时不选**：合同无音频；仅有少量剪辑可由 Audacity 完成；确定性程序音的生成器是更清晰 authority。
- **替代**：Audacity 用于轻量录制/清理/裁剪，引擎 AudioBus，或经 probe 允许的参数化合成。
- **真实使用证据**：共通七类，加 REAPER 工程、源许可、路由/tempo/region、render matrix、cue map；节奏 claim 另需谱面同步与实际设备校准。

## 10. `playwright-chromium`

- **任务子型**：浏览器二维游戏的公开输入、生命周期、视口、控制台/网络、状态和像素回归。
- **需要能力**：真实浏览器输入、语义定位、状态断言、trace、截图、控制台和网络记录。
- **候选工具 / candidate ID / Registry canonical ID**：Playwright + 固定 Chromium；atomic profile `playwright-chromium`。
- **原生模型**：browser context、page/locator、fixture、project、trace、screenshot 和 assertion。
- **Authority master**：可编辑 Playwright 测试源与配置；trace、截图和报告是派生物。
- **为何选**：Registry 与环境 probe 证明当前可用时，可通过玩家可达路径重复检查浏览器行为。
- **何时不选**：非浏览器消费者；证明好玩、视觉成熟或可访问性有效；规则纯函数应由项目测试器补充。
- **替代**：引擎原生测试、项目单元/属性测试或真实设备人工路径；替代工具也需独立 Registry/tool-use 证据。
- **真实使用证据**：Registry 共通七类，加当前 build 的真实命令、选择器/输入、状态断言、固定浏览器版本、trace/截图及未绕过内部状态的证明。

## 三维 future/unavailable 路由

以下仅是具体工具的未来路由 ID，不属于上述二维工具池，也不支持当前 Skill 的任何三维 claim：

| 路由 ID | 具体工具与未来 concern | 当前状态 |
| --- | --- | --- |
| `unity-3d-runtime-future` | Unity：三维运行时、Scene/GameObject/Component、物理与多平台导出 | `future_unvalidated`；不可用时 `candidate_unavailable` 或 `human_handoff` |
| `unreal-engine-3d-runtime-future` | Unreal Engine：Actor/Component、Level、Blueprint/C++、材质与高端三维管线 | `future_unvalidated`；不可用时 `candidate_unavailable` 或 `human_handoff` |
| `godot-3d-runtime-future` | Godot 3D：Node3D、Scene、Resource、物理与三维导出 | `future_unvalidated`；不得继承 `godot-2d-runtime` 证据 |
| `blender-3d-content-future` | Blender：mesh、modifier、armature、material、animation 与三维内容导出 | `future_unvalidated`；不可用时 `candidate_unavailable` 或 `human_handoff` |

任何三维主任务都应停止二维 claim，等待对应 Registry profile、专业 gate、原生母版和真实项目证据；不能以工具名称、未来 ID 或二维工程通过代替。
