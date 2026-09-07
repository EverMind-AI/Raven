# 营销传播领域模式

本文件保存 `create-marketing-graphics` 的领域数据结构与诊断表。七个 gate、共享 assurance 和 promotion 语义以主 Skill、`visual-artifact-design` 与 `CONTRACT.md` 为准；这里不复制它们。

## 子型路由

| 主型 | 主要任务 | 原生母版重点 | 常见越界 |
| --- | --- | --- | --- |
| Campaign system | 建立跨触点的共同承诺与视觉角色 | campaign record 加各媒介母版 | 把规范展示板当 campaign 本体 |
| 社交与数字广告 | 在短停留、裁切与平台控件下促成明确动作 | 平台族模板、内容变量和导出 recipe | 同图缩放、伪造平台规格或假 CTA |
| 零售与促销 | 让价格、资格、日期、产品与行动在现场成立 | 价签、货架、门店屏或促销版式母版 | 条款微字化、产品与价格错配 |
| 产品或服务发布 | 把新价值、证明、可获得性和下一步连成一条路径 | 发布主张记录与触点母版 | 用气氛代替产品真相 |
| 海报、户外与活动 | 在距离、速度、场地和制作约束下传递一个任务 | 印刷、屏幕或现场尺寸的原生母版 | 把社交帖放大、忽略现场遮挡 |
| 内容与议题传播 | 以可核验事实建立注意、理解和行动 | 系列版式、来源与更新关系 | 把复杂论证压成未经支持的口号 |
| 动效传播 | 用时间组织承诺、证明、品牌和行动 | 时间线、场景或状态机母版 | 动起来但信息顺序与静态系统断裂 |

混合任务仍选一个主型，其余写成受同一 campaign record 约束的 channel output。若独立插画、长期品牌身份、编辑叙事、空间导视或产品操作是主要价值，应转交对应领域并继承其批准输出。

## Campaign record

Campaign record 是传播事实的唯一 owner，推荐包含以下稳定字段；字段可以映射到项目格式，但不可分散到画板文本中分别维护。

```yaml
campaign_id:
revision:
audience:
  segment:
  stage:
  barrier:
promise:
  statement:
  evidence_refs: []
action:
  label:
  destination:
  availability:
conditions:
  price:
  eligibility:
  timing:
  legal_refs: []
visual_roles:
  primary_subject:
  hierarchy:
  type_roles:
  color_roles:
  evidence_role:
  action_role:
assets:
  - asset_id:
    role:
    manifest_ref:
channels:
  - channel_output_id:
    consumer_ref:
    job:
    native_spec_ref:
    content_policy: {must: [], adapt: [], omit: []}
    master_ref:
    export_recipe_ref:
```

主张来源、价格、日期、资格、行动入口和 asset id 引用只能在 record 中改；asset manifest 解析每个 id 的文件版本、权利和批准状态。母版引用这些值并拥有本媒介的布局；export recipe 拥有转换，derived deliverable 不拥有事实或布局。

## Dependency 与 stale

变更前先按 concern 标记受影响 rendition 或 channel output 为 `stale`，再从权威 owner 向下重建：

| 变更 | 必须失效的下游 | 返回位置 |
| --- | --- | --- |
| 承诺、证明或限制 | 引用该 claim 的全部母版与导出 | campaign contract |
| 价格、日期、资格或法务 | 显示或省略该条件的 channel output | campaign contract |
| CTA、链接或承接端 | 所有行动组件、二维码与路径证据 | campaign contract |
| asset 批准、权利或版本 | 使用该 asset id 的母版与导出 | provenance / rights |
| 视觉角色或主锚点 | 所有依赖该角色的媒介母版 | campaign grammar |
| 平台规格或现场条件 | 对应渠道母版、export recipe 与证据 | channel adaptation |
| 母版或导出 recipe | 由其生成的全部 rendition | 对应母版或导出链 |

不得在派生 PNG、PDF、视频或 Gallery 拼图上修真值。重建后以新 build 复跑相关渠道证据；未重新检查的 channel output 保持 `stale`。

## 跨比例 campaign grammar

必须保持的是语义关系，不是像素坐标：

- 同一受众阶段面对同一核心承诺，证明不会换成更强但无依据的说法；
- 主对象、证据、品牌和行动的角色稳定，但显著性可随观看任务改变；
- 同一 asset id 可使用批准的裁切或状态变体，不得悄悄换人物、产品、包装或权益；
- `must/adapt/omit` 决定内容变化，省略不等于改写事实；
- 语气、摄影、插画、图形和动效节奏属于同一表达制度，但不要求所有尺寸同一构图。

适配动作可包括重排、换批准裁切、改变行长和字号关系、拆分时序、移动证明、压缩次要文案。若要改变承诺、证据、行动或资产身份，必须回 campaign record，而不是在本地“优化文案”。

优先证明差异最大的触点：最窄与最宽、最短停留与最完整说明、近距与远距、静态与动态。只有其 grammar 都成立，才批量生成同族 rendition。

## CTA 与渠道合同

每个 channel output 记录：对应最终消费者、用户此刻的任务、必须看到的承诺、必要证明、行动文案、真实 destination、条件、观看上下文、平台/制作规格来源、遮挡与裁切区、文件合同和验收方式。

CTA 必须满足：

- 动词与到达后的真实动作一致；不存在的购买、报名、下载或导航不得伪装成可用；
- 时间、价格、资格或地域会改变决定时，与行动处于可感知关系；
- 二维码、短链、按钮或地址使用最终值，并在最终媒介中实测；
- 认知型触点可以没有立即转化 CTA，但要明确下一认知任务，不能用无意义按钮填空。

平台规格是带日期和来源的外部依赖，不把易变尺寸硬编码成领域规则。平台更新后，引用旧规格的 channel output 进入 `stale`。

## Rights 与 provenance

每个可见资产按 asset id 记录：来源或创建者、许可/同意、品牌批准、媒体/地域/期限、编辑与生成链、必要披露、允许的变体、替换 owner、证据位置和失效条件。

- 占位资产必须显式标记，并与发布导出隔离；“之后替换”不能通过 rights gate。
- 真实产品、包装、人物、价格或认证不得由合成内容冒充实拍事实。
- 字体、图库、音乐、配音、数据和引语分别核验；一个来源链接不能替代许可范围。
- 资产到期、撤回同意或品牌更新时，沿 dependency graph 失效全部相关 rendition。

## 消费者证据模式

最终消费者是实际接收并使用交付物的人、设备、软件、平台或制作链，不是为了方便 review 临时搭建的查看器。先从渠道合同确定消费者与最终验收方法，再选择证据：数字图像检查目标平台转码后的最终像素，印刷与实体媒介检查样张/成品和适格制作结论，动效检查目标播放器中的最终编码；只有消费者确为 Web 时，浏览器才是目标 renderer，并组合 `$build-polished-visual-frontends` 检查网页实现。

| 媒介 | 应保留的领域证据 |
| --- | --- |
| Web campaign / 落地传播页 | 目标浏览器与设备上的最终像素、响应式状态、内容/CTA 路径及必要交互；build 与前端证据同版 |
| 社交与数字广告 | 平台安全区/裁切下的最终帧、转码后像素、落地动作与规格来源 |
| 门店屏与数字户外 | 现场比例、遮挡、循环上下文、观看距离模拟和播放文件检查 |
| 印刷、包装附属与户外 | 原生母版、预检报告、出血/色彩/字体处理、样张或适格制作方确认 |
| 动效 | 起始/中间/结束关键帧、完整时序、循环与静音语境、最终编码检查 |
| 模板化套系 | 锁定与可变字段、极值内容、批量导出日志和抽检 channel output |

证据必须绑定当前 build、消费者、检查环境和实际输入/输出 hash。总览图可帮助 review，但不能替代单个消费者证据；非 Web 交付中的 HTML 查看器、浏览器截图或 Gallery iframe 也不能替代目标 renderer、播放器、平台验证器、样张或实体成品。

## 失败返回表

| 信号 | 最早返回 |
| --- | --- |
| 漂亮但说不清向谁承诺什么 | campaign contract |
| 工具只被提及，没有可编辑母版或重建证据 | toolchain / master |
| 渠道之间只剩同色，主张或 CTA 已漂移 | campaign grammar |
| 所有尺寸像机械裁切，关键内容被遮挡或微缩 | channel adaptation |
| 资产来源、人物同意或使用范围未知 | provenance / rights |
| 只检查源码、拼图或理想画板 | consumer / channel evidence |
| 用自检声称受众理解、渠道有效或广告效果 | claim-bound release |
| 修改后仍引用旧导出、旧 review 或旧规格 | 对应 owner，并保持 `stale` |

领域 gate 的 `status` 只使用 `pending / passed / failed / blocked / not_applicable`：缺关键输入或授权时记 `blocked`，证据明确不通过时记 `failed`，只有确实不在 scope 且理由可审计时才用 `not_applicable`。`unknown` 与 `waived` 只属于相应 evidence check；waiver 必须带责任人、理由、范围与到期条件，且不能把 evidence 或 gate 变成通过。
