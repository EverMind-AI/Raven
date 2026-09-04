# 交互解释器专业工具能力档案

这些是领域选择档案，不是安装清单、默认技术栈或使用声明。每个任务只选择能拥有明确 concern 的最小工具组合；先在共享 Tool Registry 解析 candidate id、availability、版本/许可来源与 probe 入口，再建立任务级 tool-use evidence。

当前共享 Registry 尚未登记下列领域 candidate id。未登记只表示需要协调，既不证明工具无能力，也不允许写 `used`。若执行依赖 GUI、商业席位或外部人员，记录 `human_handoff`；环境探针失败时记录 `unavailable` 并返回替代候选。

所有 `used` 记录都必须覆盖七类事实：dependency、resolved version、license、real invocation、editable master、rebuild/export、current final pixels or consumer evidence。

## 选择接口

```text
task subtype → required capability → registry candidates → capability comparison/probe
→ selected role → native model/language → authoritative concern/master
→ derived export → usage evidence → failure return
```

一个 pipeline 可以有多个工具，但每个 concern 只能有一个 authoritative owner。Production 与 oracle 必须职责独立；可视化引擎不能冒充 solver，GUI 导出和 Web 查看器不能取代上游科学母版。

## 1. 符号与闭式模型

- **任务子型：**闭式 calculator、代数/微积分 explainer、可解析动力学的 reference model 或 oracle。
- **需要能力：**符号假设、精确化简/求解、极限/导数、公式导出和可复现 reference vectors。
- **候选 / stable id：**SymPy；`sympy-symbolic-modeling`。官方入口：`https://docs.sympy.org/`。
- **原生设计语言 / 数据模型：**Python 中的符号表达式树、函数、方程、集合与 assumptions；精确对象优先于格式化浮点数。
- **权威母版：**版本化 `.py` 模块或不可拆分的符号模型 bundle，拥有 `reference_model` 或 `oracle` concern；渲染公式是 derived。
- **为何选择：**目标关系可解析，符号形式能直接暴露分支、定义域和精确递推，减少不必要的数值误差。
- **何时不选：**复杂 PDE/CFD、实时刚体、仅靠数值数据定义的模型；若它生成 production 核心，不得同时充当唯一 oracle。
- **替代：**Mathematica、Maple、SageMath，或低风险任务的独立手算/真值表。
- **真实使用证据：**环境锁与许可、实际 import/solve 轨迹、可编辑表达式母版、重跑命令、精确/数值 vectors 与 hash、公式/结果 consumer evidence。

## 2. 代数与几何直接操纵

- **任务子型：**函数、几何、坐标关系、构造依赖和参数轨迹的教学 explainer。
- **需要能力：**依赖对象图、约束构造、滑块、轨迹、可逆编辑和可分享的交互 worksheet。
- **候选 / stable id：**GeoGebra；`geogebra-construction-authoring`。格式参考：`https://geogebra.github.io/docs/reference/en/File_Format/`。
- **原生设计语言 / 数据模型：**点、线、函数、数值、约束、依赖对象、工具与脚本组成的 construction graph。
- **权威母版：**可编辑 `.ggb`/`.ggt`，拥有构造与交互 concern；截图或嵌入页是 derived。
- **为何选择：**学习目标依赖几何/代数对象的直接操纵，原生依赖关系比手写坐标与拖拽更可信。
- **何时不选：**一般事件仿真、复杂 solver、空间场、重型应用状态；无法自动操作且任务需要代理执行时转 `human_handoff`。
- **替代：**Desmos、Observable reactive model，或已验证的项目几何引擎。
- **真实使用证据：**产品/运行环境版本与许可、真实打开/编辑操作、`.ggb` 母版、导出或嵌入轨迹、重开验证及目标 consumer 像素。

## 3. 反应式 Web 解释与图形

- **任务子型：**轻量参数 explainer、模型输出叙事、少量状态的浏览器最终交付。
- **需要能力：**reactive dataflow、数据 loader、可组合 marks/scales、直接标注和静态构建。
- **候选 / stable id：**Observable Framework + Observable Plot；`observable-framework-plot`。官方入口：`https://observablehq.com/framework/`、`https://observablehq.com/plot/`。
- **原生设计语言 / 数据模型：**Markdown/JavaScript reactive cells、文件化数据快照，以及 Plot 的 tidy data、marks、channels、scales 与 transforms。
- **权威母版：**项目 Markdown/JS、loader 与数据文件 bundle，拥有 Web delivery/representation concern；构建目录是 derived。
- **为何选择：**关系可由小型反应图和标准定量编码表达，最终消费者本身就是 Web。
- **何时不选：**把它当科学 solver、复杂持久化产品、重型 3D/物理场或只需单个原生 worksheet 的任务。
- **替代：**项目既有框架 + Vega-Lite/Plot，或更适合子型的原生教学工具。
- **真实使用证据：**package lock、版本/许可、真实 import/build、可编辑 reactive 源、离线重建、数据到 mark/scale 的映射和最终 URL/像素证据。

## 4. 数值实验与独立 ODE oracle

- **任务子型：**数值动力学、教学计算实验、production 的职责独立参考实现。
- **需要能力：**可复现 notebook、成熟 ODE integration、事件、稠密输出、误差控制和批量 reference vectors。
- **候选 / stable id：**Jupyter + SciPy/NumPy；`jupyter-scipy-numerics`。官方入口：`https://docs.jupyter.org/`、`https://docs.scipy.org/doc/scipy/reference/integrate.html`。
- **原生设计语言 / 数据模型：**`.ipynb` 的代码/说明/输出/metadata，或版本化 Python 模块；solver method、tolerance、events 与 sample grid 是模型配置。
- **权威母版：**notebook 加环境锁，或 Python reference bundle，拥有 `oracle`/实验 concern；导出的 JSON/CSV 和 HTML 是 derived。
- **为何选择：**模型必须数值求解，或 production 需要不同实现路径的参考结果与收敛检查。
- **何时不选：**解析/精确递推已足够；把 notebook 直接冒充高完成度公共产品；oracle 与 production 共享同一核心函数。
- **替代：**Pluto + DifferentialEquations.jl、MATLAB Live Script，或领域专用 solver。
- **真实使用证据：**环境/内核锁、版本/许可、从干净内核重跑、solver invocation、母版、tolerance/事件/收敛记录、vectors/hash 与消费端对照。

## 5. 声明式多领域动态系统

- **任务子型：**机械、电气、热、控制等耦合连续系统与可交换 simulation model。
- **需要能力：**声明式方程、组件/连接器、事件、初始化、solver 配置及 FMI 导出。
- **候选 / stable id：**OpenModelica + FMI；`openmodelica-fmi-simulation`。官方入口：`https://openmodelica.org/doc/OpenModelicaUsersGuide/latest/`。
- **原生设计语言 / 数据模型：**Modelica class、equations、algorithms、connectors、annotations 与 experiment configuration。
- **权威母版：**版本化 `.mo` package 拥有模型 concern；FMU、结果文件和 Web wrapper 是 derived。
- **为何选择：**系统由跨域组件和方程耦合组成，需要保留原生组件拓扑和可交换模型。
- **何时不选：**简单闭式关系、纯离散算法、PDE/CFD 或只需图形说明；不要把 FMU 二进制当唯一可编辑母版。
- **替代：**Dymola、Simscape/Simulink 或符合任务生态的 FMI authoring tool；商业候选按许可转 handoff。
- **真实使用证据：**明确发行版与库许可、编译/模拟命令、`.mo` 母版、solver/初始化日志、FMU export/hash、独立结果核对与最终 consumer。

## 6. PDE/FEM 空间模型

- **任务子型：**扩散、热、结构、波动及其他由弱式与网格定义的空间 simulation。
- **需要能力：**mesh、function space、variational form、boundary condition、linear/nonlinear solve、误差与场输出。
- **候选 / stable id：**FEniCSx + ParaView/PyVista；`fenicsx-paraview-pde`。官方入口：`https://docs.fenicsproject.org/`、`https://docs.paraview.org/`。
- **原生设计语言 / 数据模型：**UFL 弱式、mesh/topology、functions、boundary markers、solver options；可视化使用 field data 与 source/filter pipeline。
- **权威母版：**模型脚本、网格/边界与 solver config 拥有 computation；可编辑 visualization state 另拥有 representation concern。
- **为何选择：**claim 依赖真实坐标场、边界和 PDE，而不是聚合量或装饰性空间效果。
- **何时不选：**0D/网络模型、单纯教学曲线、专门的高雷诺 CFD；GUI-only 操作无法由代理复现时登记 handoff。
- **替代：**Firedrake、deal.II、COMSOL；按方程类型、许可和部署选择。
- **真实使用证据：**环境与许可、真实 solve/visualize 调用、模型/mesh 母版、网格或阶次收敛、残差/守恒、场文件/管线重建和最终像素。

## 7. CFD 与局部流场

- **任务子型：**局部速度、压力、湍流、传热或输运场决定结论的 spatial simulation。
- **需要能力：**mesh、finite-volume schemes、边界/初值、solver control、field output、守恒与后处理。
- **候选 / stable id：**OpenFOAM + ParaView；`openfoam-paraview-cfd`。发行分支必须在 Registry 中明确。官方入口：`https://doc.cfd.direct/openfoam/user-guide-v14/cases`。
- **原生设计语言 / 数据模型：**case 的 `system`、`constant`、initial-time fields、mesh 和 dictionaries；表示使用真实 vector/scalar fields。
- **权威母版：**完整 case bundle 拥有 CFD model/computation；可编辑 ParaView state 拥有 representation；截图、流线导出是 derived。
- **为何选择：**学习或分析 claim 确实依赖局部场，且聚合/网络模型无法回答。
- **何时不选：**聚合或 well-mixed 模型、快速闭式 explainer、没有网格/边界依据的“科学感”动画。
- **替代：**SU2、Fluent、COMSOL 或经验证的专用 CFD 工具；商业工具按实际席位记录 handoff。
- **真实使用证据：**具体发行版/许可、case 与 mesh、solver invocation、scheme/边界、网格质量、收敛/守恒、field-to-visual mapping、rebuild 与 consumer pixels。

## 8. 高投入 STEM 教学仿真栈

- **任务子型：**需要完整模型—视图架构、多模态可访问性、instrumentation 与真实学习者迭代的公共教学 simulation。
- **需要能力：**scene graph、成熟教学控件、模型/视图分离、可访问语义、状态 instrumentation、可复现实验与研究流程。
- **候选 / stable id：**PhET HTML5 ecosystem；`phet-html5-simulation-stack`。官方入口：`https://github.com/phetsims`、`https://phet.colorado.edu/en/research`。
- **原生设计语言 / 数据模型：**项目模型层、Scenery scene graph、SUN components、Tandem/phet-io instrumentation 与 Parallel DOM。
- **权威母版：**符合该生态的多仓或锁定 workspace bundle；不能只复制一个控件、动画或视觉外观。
- **为何选择：**任务愿意承担完整 STEM simulation 工程、可访问性和学习者研究，而非一次性网页演示。
- **何时不选：**不能采用完整依赖/构建/研究流程，或只想模仿卡通风格；此时它只能是 benchmark，不能声称使用。
- **替代：**较轻量 Web/worksheet 工具加独立教学研究，或组织已有的成熟教育仿真框架。
- **真实使用证据：**真实仓库依赖与许可、构建命令、模型/Scenery/SUN/Tandem 调用、可编辑 workspace、instrumentation/Parallel DOM、重建及当前 consumer/像素；learner evidence 另归学习 gate 与 promotion coverage。

## 9. 科学 3D 与体数据表示

- **任务子型：**体数据、网格、切片、等值面、张量/向量场或科学 3D consumer。
- **需要能力：**科学 dataset、array semantics、source/filter/mapper/actor pipeline、transfer function、相机与交互 widget。
- **候选 / stable id：**VTK.js；`vtkjs-scientific-visualization`。官方入口：`https://kitware.github.io/vtk-js/docs/index.html`。
- **原生设计语言 / 数据模型：**ImageData/PolyData 等 datasets、scalar/vector arrays、filters、mappers、actors、lookup/transfer functions 和 widget state。
- **权威母版：**有语义的 field dataset 加可编辑 pipeline 源/状态；它拥有 representation，不拥有上游物理求解。
- **为何选择：**最终 Web consumer 需要忠实检查科学场、体或网格结构，通用 scene graph 不足。
- **何时不选：**把 VTK.js 当 solver、从零生成无依据场、简单 2D 图或只需通用 3D 叙事。
- **替代：**ParaViewWeb；若无科学数据模型需求可评估 Three.js，但必须另证 representation mapping。
- **真实使用证据：**package/version/license、真实 reader/pipeline import、dataset 与 pipeline 母版、field array/transfer mapping、构建、非空 WebGL/交互状态与最终像素。

## 10. 刚体、接触与控制物理

- **任务子型：**机器人、关节、刚体、接触、执行器、传感器和控制策略 simulation。
- **需要能力：**动力学模型、kinematic tree、joint/contact/actuator/sensor、integrator、state/control 与确定性 replay。
- **候选 / stable id：**MuJoCo；`mujoco-rigid-body-simulation`。官方入口：`https://mujoco.readthedocs.io/en/latest/modeling.html`。
- **原生设计语言 / 数据模型：**MJCF 的 body/joint/geom/site/actuator/sensor tree，编译后的 model 与运行时 data/state。
- **权威母版：**可编辑 MJCF XML 或 MJZ bundle；版本相关、不可逆的编译二进制不能成为长期母版。
- **为何选择：**claim 依赖成熟刚体/接触/控制计算，手写近似会破坏动力学与复现。
- **何时不选：**流体、PDE、一般软体、纯几何动画或不需要真实动力学的解释。
- **替代：**Drake、Bullet、Rapier、PhysX；按物理范围、浏览器部署和许可选择。
- **真实使用证据：**engine/version/license、真实 load/step 调用、MJCF 母版、integrator/timestep、state/control 与 replay、benchmark/不变量、render 由模型 state 驱动及最终 consumer。

## 未登记 candidate 的失败返回

若任务需要的 candidate 尚未进入共享 Registry：

1. 不能凭本文件名称写 `used`、`available` 或 license 已验证；
2. 先请求 Registry owner 登记官方来源、许可、环境需求、availability probe、原生模型、母版格式和七类使用证据；
3. 等待期间可以比较能力并记录 `considered`；确需 GUI/商业操作时提交 `human_handoff`；
4. 若当前任务不能等待，选择已登记且能力充分的替代；不能因未安装而手写替代并声称成熟工具有 gap。
