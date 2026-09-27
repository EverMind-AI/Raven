# git-story-film

[English](README.md) | 简体中文

把一个 GitHub 仓库的成长，拍成一部两分钟左右的手绘动画短片。

给助手一个仓库链接，它会担任导演：从 Git 历史里挑出真实的提交、版本和回滚，编成有起承转合的故事，设计主角，写提纲和分镜，制作动画，最后渲染成 4K/60fps MP4。影片面向第一次接触产品的人，讲清楚它是怎么成长的、现在能帮用户做什么。

附带完整的 128 秒 Raven 示例片 `examples/raven-story/`。顶层是英文版，`zh/` 保存中文版的源文件。

## 快速开始

1. **加载技能**：当前副本位于 Raven 项目的 `skills/git-story-film/`，可以让助手读取这里的 `SKILL.md`。若希望 Claude Code 自动发现技能，把整个目录复制到 `~/.claude/skills/` 或项目的 `.claude/skills/` 下。
2. **描述需求**，例如：
   - “帮我给 https://github.com/owner/repo 做一个视频，先构思故事方向。”
   - “讲讲这个项目是怎么长大的，做成动画。”
   - 在 Claude Code 中，也可以调用 `/git-story-film https://github.com/owner/repo`。
3. **按七步推进**：助手每一步都会展示具体产物，等你确认或提出修改意见后再继续。

## 七步流程

| 步骤 | 你会看到 | 你要做的 |
|---|---|---|
| 1. 故事方向 | `OUTLINE.md`：故事、重点功能及事实依据 | 确认方向、语言、画幅和 Logo 使用方式 |
| 2. 角色设计 | HTML 角色设定表：表情、动作姿势、材质和尺寸检查 | 选择外观，提出修改意见 |
| 3. 完整提纲 | 约两分钟的结构、画风、节拍、结尾及待核实表述 | 调整文案和顺序 |
| 4. 前 30 秒分镜 | 每个镜头的关键帧、字幕和提交依据 | 确定口吻、节奏和画风 |
| 5. 完整制作 | 剩余分镜和动画，必要时补充角色姿势 | 随时提出反馈 |
| 6. Studio 预览 | 在浏览器中查看整部影片，此时还不渲染 MP4 | 观看并提出修改意见 |
| 7. 成片交付 | 4K/60fps MP4，附时长、分辨率、大小和验证结果 | 确认交付 |

## Studio：在浏览器里看片

影片是 HTML 页面，配套脚本齐全时可以直接打开，不需要服务器。打开示例 `examples/raven-story/raven.html` 时，请保留技能包的目录结构。若把示例复制到独立影片目录，并把引擎放到同一层，需要把 HTML 中的 `../../engine/` 脚本路径改成同层文件名。

- **上方**：影片和同宽进度条。每幕是一段色带，每个镜头是编号格子；悬停可查看时间和镜头信息。
- **播放**：从当前位置开始。点击 `♪ sound` 开启配乐，准备好后会接上当前播放位置。
- **快捷键**：空格播放或暂停；左右键逐帧移动；Shift+左右键移动一秒；`[` 和 `]` 切换镜头；Home/End 跳到头尾。
- **下方**：每个镜头一张分镜图，由当前影片代码直接绘制。改代码后刷新即可同步；点击图片跳转，播放时高亮当前镜头。

附带的 `examples/raven-story/storyboard/storyboard.html` 是含双语字幕的纯文字分镜参考。技能包不包含预渲染图片，影片 Studio 会直接用代码绘制分镜帧。制作自己的影片时，可以用 `scripts/storyboard.py` 加入生成的截图，并通过 `--embed` 导出便于分享的单文件页面。

## 渲染成片

在 Studio 中确认后，于准备好的影片目录执行：

```bash
node render-hq.mjs film.html --width 3840 --fps 60              # 4K/60fps delivery
node render-hq.mjs film.html --width 1920 --fps 24 --no-cover   # Quick 1080p preview
```

- **封面帧**：默认把片尾卡同时写成第 0 帧，供播放器和信息流用作封面，因此开头会闪一帧。社交媒体发布可保留；网页内嵌或循环播放时加 `--no-cover`。
- **一次只渲染一部**：并行渲染多部 4K 影片可能导致 Chrome 工作进程崩溃。

## 不止一种讲法

吉祥物破壳、沿时间线向右走只是其中一种。按产品特点选择，也可以组合或自行设计：

- **旅程**：角色沿时间线前进，一镜到底，Raven 示例采用这种形式。
- **生长**：树木或城市在原地成长。
- **建造**：机器逐步组装。
- **闯关**：横版游戏，版本是关卡，Bug 是敌人。
- **陪伴**：产品伴随用户的日常逐渐成长。

详见 `references/forms.md`。

## 目录结构

```text
git-story-film/
  README.md       English overview
  README.zh-CN.md Chinese overview
  SKILL.md        Director instructions, workflow and constraints
  references/     Directing, story forms, craft, fonts and production
  engine/         Drawing and animation engine, including studio-ui.js
  scripts/        Git statistics, fonts, frames, storyboards and rendering
  examples/       Raven film; Chinese variants live in zh/
```

## 环境要求

- Node 22+
- Google Chrome
- ffmpeg，Homebrew 安装可能需要把 `/opt/homebrew/bin` 加入 PATH
- Python 3.8+

## 基本约定

- **事实有依据**：哈希、日期和提交数来自仓库；示意数字要标明；区分开发期工具和运行时功能；只展示产品确实支持的集成。
- **字体有授权**：使用 LXGW WenKai（霞鹜文楷）、Comic Neue 等 SIL OFL 字体，把授权文件一起放入影片目录。
- **没有录制旁白**：字幕承担叙述，配乐由 Web Audio 合成。如果后续需要配音，可以预留时段。
- **有意识地保留本地化**：技能指令和默认示例说明使用英文；中文影片版本、中文界面标签和双语字幕参考保留原文，避免破坏中文功能。

## 致谢

`engine/` 中的 `core.js`、`cels.js`、`materials.js`、`studio.js` 和 `render.mjs` 改编自：

- **hand-drawn-canvas-animation**，Alexey Fateev，MIT 许可
  https://github.com/alesha-pro/tools/tree/main/skills/hand-drawn-canvas-animation

修改包括 60fps 输出和播放控件。复制引擎时请保留 `engine/LICENSE.hand-drawn-canvas-animation`。Studio（`engine/studio-ui.js`）、辅助脚本和示例影片是为本技能编写的。
