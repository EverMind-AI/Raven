# 启动 WebUI

通过 Raven WebUI，你可以在浏览器中与 Agent 对话、协同处理任务并管理工作区。
在同一界面中跟踪任务进度、查看文件和输出，以及浏览记忆与技能。

```bash
raven web
```

该命令会在浏览器中打开 WebUI，并在后台启动 Raven。关闭浏览器后，Raven 仍会继续运行。
如需停止服务，请运行 `raven web --stop`。

## 开发者启动 { #developer-launch }

```bash
raven web --dev
```

`raven --dev` 与 `raven web --dev` 启动同一个服务，但会启用轨迹视图：当会话有内容后，
聊天页头部工作区面板按钮的右侧会出现一个切换按钮。轨迹视图按时间列出该会话的每一步
（输入、模型调用、工具调用、回复）及其耗时，可打开任意一步的详情，并在底部绘制耗时构成条。
普通的 `raven web` 不显示该按钮。如果已有普通服务在运行，`--dev` 会提示它没有轨迹视图并
保持其运行；请先执行 `raven web --stop` 再启动。

安装及模型服务商配置请参阅[快速开始](quick-start.md)；Docker 和源码部署请参阅
[自托管](self-hosting.md)。
