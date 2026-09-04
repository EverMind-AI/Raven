# 营销传播工具能力档案

这些是 D05 的选择档案，不是安装声明，也不是工具事实 Registry 的替代品。先从共享 Tool Registry 读取版本、许可、可用性、能力与限制；项目证据决定 `used`。没有真实依赖、授权、调用、可编辑母版、重建/导出和最终消费者证据时，只能记为 `candidate`、`unavailable` 或 `human_handoff`。

选择记录统一写：`subtype → required capability → registry id → reason → native model → authority concern → master → export → custom boundary → usage evidence → failure return`。选定工具后，布局、组件、样式、资产链接、母版和导出必须以它的原生模型继续；只借一个功能再另起手写体系，视为工具选择失败。

## 协作布局与渠道母版

**Registry id：`figma-design`（已有共享 profile）**

- **适用**：需要多人审批、变量/组件复用、多个数字触点母版和可检查交接的 campaign system。
- **原生模型**：frame/node、Auto Layout、components/variants、variables/styles、libraries；它没有可照搬的默认审美。
- **权威母版**：获得授权的可编辑 Figma 文件版本拥有设计意图、变量和本媒介布局；导出的 PNG/SVG/PDF 是供最终消费者接收的 derived deliverable。
- **为何选择**：协作、结构化复用和多画板 review 是主要能力需求，且接收方确实消费 Figma。
- **何时不选**：没有合法账户/文件权限；任务要求完整自动重建；最终权威是复杂印前、精细位图或时间线。
- **可替代**：原生矢量/版面工具；经验证的程序化 rendition pipeline。
- **使用证据**：共享 Registry profile 及项目文件版本、seat/许可、真实编辑或调用、母版节点、导出记录和最终像素。当前 Registry 为 `human_handoff` 时不得写成自动使用。

## 专业矢量与版面生产

**Candidate id：`adobe-illustrator`**

- **适用**：海报、户外、零售图形、复杂路径、专色/印刷交付和需要精确矢量母版的触点。
- **原生模型**：artboards、layers、paths、symbols、graphic styles、linked assets、swatches 与印刷文档设置。
- **权威母版**：可编辑 AI 文档及链接资产拥有本媒介布局和矢量几何；PDF/SVG/位图为受控导出。
- **为何选择**：路径、字体轮廓策略、色板、链接资产和专业导出是核心能力。
- **何时不选**：主要问题是批量数据替换、复杂位图合成、团队实时审批或长时间轴动效。
- **可替代**：Affinity Designer/Publisher 等经 Registry 核验且接收方可编辑的原生工具；协作优先时可选 Figma。
- **使用证据**：安装与版本、商业许可、真实打开/编辑、AI 母版、链接资产清单、重建导出、预检/最终消费者。未核验时 `candidate` 或 `human_handoff`。

## 专业位图与摄影合成

**Candidate id：`adobe-photoshop`**

- **适用**：摄影精修、产品/人物抠合、色彩处理、纹理和需要可追溯合成链的 campaign key image。
- **原生模型**：layer groups、smart objects、masks、adjustment layers、linked assets、color profile 与 nondestructive edits。
- **权威母版**：分层 PSD/PSB 拥有像素合成和处理；它不拥有 campaign 文案事实或其他媒介布局。
- **为何选择**：非破坏位图编辑、智能对象替换和颜色管理决定质量。
- **何时不选**：主要输出是可缩放矢量、结构化模板、文本密集版面或自动多尺寸排版。
- **可替代**：Affinity Photo 等经核验的分层位图工具；简单批准裁切可留在布局母版。
- **使用证据**：安装/版本/许可、源资产 import、分层母版、编辑链、profile/export、最终像素和 rights refs。扁平图不能证明使用。

## 模板型渠道生产

**Candidate id：`canva-design`**

- **适用**：品牌已稳定、由非专业团队持续替换批准内容、需要受控社交或门店模板的生产。
- **原生模型**：page/template、brand kit、locked elements、editable placeholders、resize/export；可用能力受方案和账户权限影响。
- **权威母版**：特定版本的可编辑模板拥有该模板族的锁定/可变关系；campaign record 仍拥有事实和 asset id。
- **为何选择**：交接对象、权限模型和重复生产比自由构图更重要。
- **何时不选**：需要高端矢量/位图、复杂印前、严格自动化、特殊动效或模板无法表达 grammar。
- **可替代**：Figma 受控组件；程序化 rendition pipeline；专业软件人工生产。
- **使用证据**：账户/方案/许可、模板 URL 与版本、真实编辑/导出、锁定字段、极值内容测试、最终消费者。无席位证据时只能 `human_handoff`。

## 程序化多尺寸 rendition

**Candidate id：`satori-sharp-rendition-pipeline`**

- **适用**：批准 grammar 稳定、内容结构化、尺寸族明确，并需要确定性批量生成数字图像。
- **原生模型**：Satori 的受限 JSX/CSS-to-SVG 布局加 Sharp 的 raster composition/encoding；字体、图片和数据必须本地可复现。
- **权威关系**：campaign record 拥有承诺、条件、行动及 asset id 引用；asset manifest 拥有 id 对应的文件版本、权利和批准状态；版本锁定的模板源码与内容 schema 可组成一个 native bundle，拥有该模板族的 layout concern；export recipe 拥有转换。生成的 SVG/JPEG/PNG 是 derived channel output，不拥有布局。
- **为何选择**：可重复构建、内容极值检查、批量变体和 build-hash 绑定是核心需求。
- **何时不选**：自由美术指导、复杂文本排版、精细摄影、原生动效、接收方只接受 GUI 母版，或能力 probe 不能复现关键视觉。
- **可替代**：Figma/Canva 模板交接；Illustrator/Photoshop 人工母版；经核验的同类 pipeline。
- **使用证据**：包锁/版本/许可、真实 invocation、模板与 schema、字体/资产输入、可重复 build、导出日志和最终像素。Satori 与 Sharp 必须作为 pipeline 分别取证。

## 动效传播生产

**Candidate id：`adobe-after-effects`**

- **适用**：时间层级、转场、摄影/字体动画、音画和多版时长是传播语法的一部分。
- **原生模型**：compositions、layers、keyframes、expressions、precomps、linked footage 与 render queue。
- **权威母版**：可编辑 project/composition 拥有时序与运动；静态母版拥有静态视觉，campaign record 拥有主张。
- **为何选择**：时间线合成、可编辑关键帧和专业编码链决定交付。
- **何时不选**：只需简单网页状态变化、交互向量状态机、实时三维或单张静态适配。
- **可替代**：Rive 用于交互向量状态机；原生视频工具用于剪辑；程序化动效需独立 Registry profile 与重建证据。
- **使用证据**：安装/版本/许可、真实项目调用、可编辑 composition、素材链接、render log、关键帧与最终编码检查。不可用时 `human_handoff`。

## DAM 与 provenance

**Candidate id：`marketing-asset-provenance-pipeline`**

- **适用**：资产多、权利与批准会变化、跨渠道需要稳定 asset id、rendition lineage 或 Content Credentials/C2PA 记录。
- **原生模型**：DAM asset/version/metadata/approval/rendition 关系，加 provenance manifest 或签名链；具体供应商模型不能互相假定等价。
- **权威母版**：DAM/manifest 拥有资产身份、版本和权利 metadata；创作母版仍拥有布局或合成。
- **为何选择**：替换、撤回、到期、批准和派生关系必须可查询并传播 stale。
- **何时不选**：单一自有资产可由本地 manifest 完整治理；系统没有导出证据或只能保存链接。
- **可替代**：版本化本地 asset manifest；经核验的供应商 DAM；独立 C2PA 工具只负责 provenance concern。
- **使用证据**：供应商/包身份、版本/许可/权限、真实 ingest/query/export、asset/version ids、manifest、派生链和 channel output 引用。组合工具需登记 pipeline，不得用品牌名代替证据。

## 渠道合同与交付预检

**Candidate id：`marketing-delivery-preflight-pipeline`**

- **适用**：数字广告/社交规格、平台安全区与转码，以及 PDF 印前、字体、色彩、出血或供应商交付需要独立验证。
- **原生模型**：带日期的官方渠道合同与 preview/validator；印刷分支使用 PDF/X、preflight profile、报告和 proof。两类检查结果都不拥有创作母版。
- **权威母版**：渠道 spec snapshot 拥有验收条件；preflight 配置拥有检查 recipe；对应布局母版仍拥有设计。
- **为何选择**：发布成败取决于消费者系统，而非编辑画板看起来正确。
- **何时不选**：没有目标渠道/供应商、只拿通用屏幕截图冒充平台验证，或工具无法读取最终交付格式。
- **可替代**：平台官方上传预览和人工 handoff；Adobe Acrobat/callas 等经核验的 PDF 检查；供应商 proof。
- **使用证据**：规格来源与日期、validator/preflight 身份和版本/许可、最终文件 invocation、报告、平台预览或 proof、修复后复测。失败必须返回母版或 export recipe。

## 自定义能力边界

只有 Registry 中可运行的候选工具经过真实 capability probe 后，才能声明 gap。自定义记录必须同时包含 `capability_probe`、`gap`、`minimal_custom_boundary` 和 `coherence_evidence`，并明确谁拥有新增 concern、如何 import/export、如何重建以及如何与 campaign grammar 保持一致。

缺账户、缺许可、不会操作、偏好手写或只读到文档，都不是能力 gap。GUI/商业工具不可运行时，选择已验证替代或给出 `human_handoff`；不得用未经验证的自研实现偷偷替代专业母版。
