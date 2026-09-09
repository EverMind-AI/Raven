# 技术图解工具能力档案

本文件提供任务到工具的候选路由，不声明任何环境已经安装或任何项目已经使用。`candidate_id` 采用符合共享 Registry 命名规则的稳定候选 ID；执行时必须查询 Registry 和 environment probe。

若 Registry 没有对应 ID，在选择记录中写 `candidate_unavailable`，tool-use evidence 按治理 schema 写 `availability: unavailable`；可由具备席位的人完成时写 `human_handoff`。只有 dependency、version、license、真实 invocation、editable master、rebuild/export 和目标消费者最终验收全部存在时，才可写 `used`。

下文“目标消费者最终验收”始终按媒介分流：视觉消费者检查与最终交付同一 `build_hash` 的最终像素；原生或非视觉消费者检查该 profile 所需的可重开、可编辑、字段查询、关系/状态一致、往返或执行等价终态，不强迫生成像素。辅助浏览器预览不能替代非 Web 消费者证据。

## 目录

- 规则拓扑与文本图编译
- 结构化 diagram 编辑
- 技术矢量说明
- BPMN 流程建模
- PLC/SFC 工程
- 电气 ECAD/EDA
- PFD/P&ID 工程
- 机械 CAD 与关联工程图
- MBSE 系统建模
- 技术文档发布

## 1. 规则拓扑与文本图编译

- **任务子型**：说明图、架构、网络、依赖图、低到中复杂度流程/状态/时序。
- **需要能力**：文本版本化、稳定 ID、有向边、cluster/port、自动布局、可重复矢量导出。
- **候选工具 / candidate id**：Graphviz DOT `graphviz-dot`；Mermaid CLI `mermaid-cli`；PlantUML `plantuml`；D2 CLI `d2-cli`。
- **原生设计语言/模型**：节点、边、子图、端口、属性和布局约束的文本 DSL；布局结果是派生投影。
- **权威母版**：DSL 源文件与固定 renderer/config bundle；图像/PDF 不拥有关系事实。
- **为何选**：关系是主事实、需要 diff/批量生成或自动布局，且不要求领域标准元件和精密人工排线。
- **何时不选**：端子/导体、P&ID tag、尺寸/公差、可执行 SFC、严格工程分页或人工精排决定正确性。
- **替代**：`drawio-desktop`；复杂系统追溯改 `capella-arcadia`；领域工程图改相应专业工具。
- **真实使用证据**：二进制/包版本与许可、真实 CLI invocation、DSL master、解析/端点断言、重建命令、输出 hash 和目标消费者最终验收；记录自动布局造成的顺序或交叉修订。

## 2. 结构化 diagram 编辑

- **任务子型**：培训说明、功能接口、责任边界、人工精排架构/网络和非工程级流程。
- **需要能力**：对象/容器、attached connector、固定连接点、图层、metadata、多页、可编辑矢量导出。
- **候选工具 / candidate id**：diagrams.net Desktop `drawio-desktop`；Microsoft Visio `microsoft-visio`。
- **原生设计语言/模型**：`.drawio` XML cell/tree 或 `.vsdx` shape/connector/page 模型；样式和 metadata 附着于原生对象。
- **权威母版**：原生 diagram 文件；外部 canonical 存在时只拥有视图几何和对应 metadata 投影。
- **为何选**：需要人工可控布局、连接吸附、分层和普通读者可交接编辑，但没有更强领域约束。
- **何时不选**：把 connector 外观当作电气/管线语义；真实尺寸、SFC 执行、网表、端子和标准符合性是 claim。
- **替代**：规则图用 `graphviz-dot`；矢量插图用 `inkscape-svg`；工程语义转 ECAD/P&ID/CAD/MBSE。
- **真实使用证据**：应用版本/许可、原生文件、连接点与 layer/metadata 检查、GUI 或 CLI 操作轨迹、重开修改、导出 hash 和目标消费者最终验收；商用 GUI 无自动席位时 `human_handoff`。

## 3. 技术矢量说明

- **任务子型**：科学机制、非比例局部说明、出版技术插图、带精确引线和文字的培训图。
- **需要能力**：命名图层/组、可编辑文本、marker/clip、符号复用、精确引线、印刷色彩和矢量导出。
- **候选工具 / candidate id**：Inkscape `inkscape-svg`；Adobe Illustrator `adobe-illustrator`；Affinity Designer `affinity-designer`。
- **原生设计语言/模型**：SVG/XML 或应用原生图层、group、path、text、symbol 和 effect 模型；不自动拥有领域连接或尺寸约束。
- **权威母版**：命名原生矢量文件与本地资产/字体 bundle；事实和机制来源由 canonical/source ledger 拥有。
- **为何选**：专业判断依赖清楚构图、注释与形态，但没有 CAD、ECAD、网表或执行模型要求。
- **何时不选**：需要真实装配、端口连通、标准符号、状态执行或数值尺度；不能以工程图外观抬高 claim。
- **替代**：结构化关系用 `drawio-desktop`；剖面用 `autodesk-inventor-drawing`；电气用 `qelectrotech` 等 ECAD。
- **真实使用证据**：安装/版本/许可、源文件与资产、真实编辑/导出、分组/文本可编辑性、来源映射、重开一致和目标消费者最终验收；自绘符号另需 capability probe 与偏差记录。

## 4. BPMN 流程建模

- **任务子型**：业务流程、跨角色工作流、消息、异常、补偿和人工/系统交接。
- **需要能力**：BPMN 元素与语义、泳道、事件、网关、消息、子流程、校验和 XML 往返。
- **候选工具 / candidate id**：Camunda Modeler `camunda-modeler`；bpmn.io toolkit `bpmn-io`。
- **原生设计语言/模型**：BPMN 2.0 XML 的 process、event、gateway、task、flow、participant 与 extension；diagram interchange 保存视图。
- **权威母版**：BPMN XML/process application；执行配置只有真实部署时才进入 authority。
- **为何选**：读者判断是业务责任、事件和流程语义，且需要标准交换、lint 或执行链。
- **何时不选**：PLC 扫描周期内的工业控制顺序、硬接线保护、物理网络或仅为展示的线性步骤。
- **替代**：控制顺序用 `codesys-sfc`；简单说明用 `mermaid-cli` 或 `drawio-desktop`。
- **真实使用证据**：安装版本/许可、BPMN XML、真实建模/导入、lint、重开往返和目标消费者最终验收；只有部署/执行证据存在时才声明 executable。

## 5. PLC/SFC 工程

- **任务子型**：工业顺序控制、联锁、模式、报警、超时、复位、安全状态和维护诊断。
- **需要能力**：IEC 61131-3 SFC 或目标平台等价模型、变量/I/O、step/transition/action、编译、模拟/trace、项目交换。
- **候选工具 / candidate id**：CODESYS SFC `codesys-sfc`；Siemens TIA Portal `siemens-tia-portal`；Beckhoff TwinCAT 3 `beckhoff-twincat-3`。
- **原生设计语言/模型**：PLC project/POU 中的步骤、转换、动作、变量、任务配置和目标设备语义；平台项目而非截图是母版。
- **权威母版**：目标 PLC 原生工程或受控项目 bundle；培训派生图由其导出/对账。
- **为何选**：claim 涉及可执行或维护级控制行为、目标平台诊断或 I/O 交叉引用。
- **何时不选**：仅解释高层正常路径、没有目标 PLC 生态或不需要执行语义；不要为外观套 SFC。
- **替代**：非执行说明用 `plantuml`/`drawio-desktop`；业务流程用 `camunda-modeler`。
- **真实使用证据**：平台版本/席位/许可、项目与库、真实 GUI/CLI 操作、编译结果、模拟/trace 路径、导出再导入、I/O/变量对账和目标消费者最终验收；无席位时 `human_handoff`。

## 6. 电气 ECAD/EDA

- **任务子型**：工业单线图、控制原理、端子/接线、PLC I/O，以及板级电子原理图。
- **需要能力**：设备/符号、pin/terminal、net/conductor、参考代号、交叉引用、库、ERC/检查、BOM/线表/端子表和多页。
- **候选工具 / candidate id**：QElectroTech `qelectrotech`；EPLAN Electric P8 `eplan-electric-p8`；AutoCAD Electrical `autocad-electrical`；KiCad Schematic `kicad-schematic`。
- **原生设计语言/模型**：工业候选保存 device/function/terminal/conductor/cross-reference；EDA 候选保存 symbol/pin/net/hierarchy/ERC。二者不能因都叫 schematic 而互换。
- **权威母版**：原生 project/library bundle；若 canonical model 拥有系统事实，ECAD/EDA 仍拥有领域连接投影和视图几何。
- **为何选**：正确性依赖机器可查询的电气对象、连接、引用或网表，而不是线条邻近关系。
- **何时不选**：业务流程、PLC SFC、物理剖面、P&ID 或只有概念关系；QET 可用于轻量/培训不等于自动满足 L2/L3。
- **替代**：工业复杂度和组织生态决定 EPLAN/AutoCAD/同级工具；板级电路选 KiCad/同级 EDA；功能接口可转 MBSE。
- **真实使用证据**：版本/许可/库、真实调用、原生工程、非空设备/端子/导体/net 字段、规则检查与清单、重命名/移动/跨页测试、洁净环境重开、重建输出 hash 和目标消费者最终验收；GUI 商业工具可 `human_handoff`。

## 7. PFD/P&ID 工程

- **任务子型**：工艺流程、管线、阀门、仪表、控制回路、介质和 off-page connection。
- **需要能力**：对象 class、equipment/line/instrument tag、管线组、nozzle/port、流向、数据管理、清单和跨页引用。
- **候选工具 / candidate id**：AutoCAD Plant 3D P&ID `autocad-plant3d-pid`；AVEVA Diagrams `aveva-diagrams`；EPLAN Fluid `eplan-fluid`。
- **原生设计语言/模型**：项目数据库与受控图纸中的 class、property、tag、line group、instrument loop、connector 和 report。
- **权威母版**：P&ID project/database bundle；PDF/DWG 导出只有在保留原生项目关系时才是派生消费者。
- **为何选**：claim 需要 PFD/P&ID 名称、设备/管线标识、仪控回路或清单可追踪。
- **何时不选**：来源不足的概念流向、普通培训机制或不需要 tag/line data；不能用符号外观补造工程事实。
- **替代**：概念流向用 `drawio-desktop`；有系统接口但无工艺管线时用 `capella-arcadia`；商业不可用则 `human_handoff` 或降级 claim。
- **真实使用证据**：版本/许可、项目设置与符号库、真实操作、tag/line/instrument 数据导出、跨页解析、规则/报告、重开往返、发布文件 hash 和目标消费者最终验收。

## 8. 机械 CAD 与关联工程图

- **任务子型**：真实剖面、装配、爆炸图、尺寸/公差、BOM、安装或维护空间关系。
- **需要能力**：参数化 part/assembly、单位、约束、基准、剖切面、关联 drawing、尺寸/公差、部件表和导出。
- **候选工具 / candidate id**：Autodesk Inventor Drawing `autodesk-inventor-drawing`；SOLIDWORKS Drawing `solidworks-drawing`；FreeCAD TechDraw `freecad-techdraw`。
- **原生设计语言/模型**：参数化实体/装配、约束、属性和从模型派生的 base/projected/section/detail/exploded views。
- **权威母版**：part/assembly model 拥有几何和装配；关联 drawing master 拥有注释与页布局，不反向改造模型事实。
- **为何选**：剖切、真实方位、装配关系、尺寸或公差决定正确性。
- **何时不选**：只需功能接口、非比例机制或抽象拓扑；用 CAD 方框不会自动产生系统语义。
- **替代**：非比例说明用 `inkscape-svg`/`drawio-desktop`；复杂系统接口用 `capella-arcadia`。
- **真实使用证据**：应用版本/许可、原生模型与 drawing、约束/单位/来源、剖切定义、模型变更后的关联刷新、BOM/尺寸对账、重开、派生输出 hash 和目标消费者最终验收。

## 9. MBSE 系统建模

- **任务子型**：跨层系统架构、功能分配、组件/端口/交换、状态、需求追踪和多视图一致性。
- **需要能力**：单一模型对象、typed ports/exchanges、分解、allocation、scenario/state、requirements/trace 和多 diagram view。
- **候选工具 / candidate id**：Capella/Arcadia `capella-arcadia`；Cameo Systems Modeler `cameo-systems-modeler`；Enterprise Architect `enterprise-architect`。
- **原生设计语言/模型**：系统模型中的 operational/function/component/port/exchange/state/requirement 与其 diagram projections。
- **权威母版**：版本化 MBSE project/repository；diagram 是模型视图，不拥有独立事实。
- **为何选**：同一对象跨多个工程视图、责任域和需求保持一致，且追溯价值超过建模成本。
- **何时不选**：低风险一次性说明、纯网络布局、板级电路或真实机械尺寸；不为“专业感”强上 MBSE。
- **替代**：图关系用 `graphviz-dot`/`drawio-desktop`；电气/机械细节交给对应工具并通过稳定接口 ID 连接。
- **真实使用证据**：版本/许可/插件、真实项目操作、model query/export、对象与接口追踪、修改传播到多视图、交换/重开、派生文档 hash 和目标消费者最终验收；商业 GUI 可 `human_handoff`。

## 10. 技术文档发布

- **任务子型**：多页技术说明、图册、培训手册、受控标题栏、目录、交叉引用和印刷 PDF 组合。
- **需要能力**：结构化章节/页模板、编号、引用、字体/资产嵌入、revision 呈现、矢量保留、PDF preflight 和可重复构建。
- **候选工具 / candidate id**：LuaLaTeX `lualatex`；Scribus `scribus-desktop`；Asciidoctor PDF `asciidoctor-pdf`；Typst `typst-cli`。
- **原生设计语言/模型**：结构化 source/template 或 `.sla` 页面模型；它只拥有分页、排版和发布呈现，不拥有嵌入图的领域事实。
- **权威母版**：文档 source、template、字体/资产与 release metadata bundle；嵌入图必须引用其受控派生输出和 hash。
- **为何选**：多个专业视图需要一致编号、标题栏、目录、声明和可重建发布包。
- **何时不选**：单张图可由原生工具直接交付；不能把排版源或 PDF 变成 ECAD/CAD/SFC 的事实母版。
- **替代**：简单直接导出使用原生专业工具；需要人工印前且自动栈不满足时 `scribus-desktop` 或 `human_handoff`。
- **真实使用证据**：编译器/应用版本与许可、source/template/assets、真实构建/GUI 轨迹、链接资源 hash、可重复 PDF、字体/矢量/preflight、release metadata 对账和目标消费者最终验收。

## 选择与自定义共同门槛

1. 从任务子型和 claim 写 required capabilities，再比较 candidate；不得先选品牌再改需求。
2. 以 Registry 的 version、license、availability、native model、master formats、operations 和 probe 为事实；本文件不覆盖 Registry。
3. 选定工具后，其原生对象、字段、连接、状态、库和导出成为该 concern 的工作语言；自由文字只承担解释，不能替代专业字段。
4. `unavailable` 返回其他 candidate 或 `human_handoff`。只有对可运行候选执行所需能力 probe 且结果为 `fail`，才可证明 capability gap。
5. 自定义只拥有缺失的最小 concern，列出不得重实现的成熟能力，并提供数据、交互、视觉、往返和导出一致性证据。
6. 多工具 pipeline 为每个 concern 指定唯一 owner、输入/输出和 handoff；不得出现 PDF/截图回流为上游事实或未披露的 authority cycle。
