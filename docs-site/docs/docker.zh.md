# Docker 部署

Raven 的 Compose 部署将 WebUI 和 Python 环境构建到同一个镜像中。nginx 提供 WebUI，
Raven 引擎负责 RPC、消息渠道、定时任务和其他网关服务。

## 启动容器 { #start-the-container }

如需构建并运行当前源码，请在仓库根目录运行：

```bash
cd docker
docker compose up --build
```

如需运行已配置的镜像而不构建当前源码，请在 `docker/` 中使用
`docker compose up --no-build`。仅运行 `docker compose up` 可能复用或拉取镜像，
并不保证重新构建本地改动。

容器就绪后打开 `http://127.0.0.1:18793`。本地默认配置会自动完成登录。

## 镜像中运行的服务 { #what-runs-in-the-image }

此部署在一个容器中运行两个进程：

| 进程 | 职责 |
| --- | --- |
| nginx，端口 80 | 提供构建好的 WebUI，并将浏览器请求代理给引擎。 |
| Raven 引擎 | 在容器内的 `127.0.0.1:18793` 上运行 RPC 网关。 |

引擎只绑定容器内的回环地址，因此 nginx 与引擎运行在同一个容器中。WebUI 和 Python 环境
均从同一份源码构建。

## 登录 { #sign-in }

Compose 默认设置 `RAVEN_AUTO_LOGIN=1`，浏览器访问本地 WebUI 时会自动获得会话。
这一设置适用于仅允许本机访问的端口。

如果要远程暴露服务，请在 `.env.local` 中设置 `RAVEN_AUTO_LOGIN=0`。容器启动时会打印一次性
登录地址。会话 Cookie 有效期为 30 天，其凭据保存在持久化数据卷中，因此容器重启后仍然有效。

在运行中的容器内生成新的登录地址：

```bash
docker compose exec raven docker-entrypoint.sh signin
```

## 配置模型服务商 { #configure-a-provider }

如果尚未预配置模型服务商，请在 **设置 > 模型服务商（Settings > Model providers）** 中添加。
配置会在下一轮生效，无需重启。

如果希望容器启动前就完成配置，请将以下内容写入 `.env.local`：

```bash
RAVEN_PROVIDER=anthropic
RAVEN_API_KEY=sk-ant-...
```

使用不需要 API 密钥的本地模型服务时，可以配置：

```bash
RAVEN_PROVIDER=ollama-chat
RAVEN_API_KEY=
RAVEN_API_BASE=http://host.docker.internal:11434
```

Docker Desktop 提供 `host.docker.internal`。使用 Linux Docker Engine 时，
需为该名称添加 host-gateway 映射，或改用容器可访问的宿主机地址。
模型服务也必须监听该地址，并限制允许访问的客户端。

入口脚本会在每次启动时将这些服务商参数写入 Raven 配置，因此容器重启时以此处的值为准。

## 配置环境变量 { #configure-environment-values }

`docker/.env` 保存已提交的默认值。对于 `RAVEN_PROVIDER`、`RAVEN_API_KEY`、
`RAVEN_AUTO_LOGIN` 等容器运行时变量，服务会将可选的 `docker/.env.local` 作为
`env_file` 后加载。凭据应写入该文件，Git 会忽略它。

Compose 插值是另一层配置。`RAVEN_IMAGE`、`RAVEN_WEB_PORT`、`RAVEN_EXTRAS`、
`RAVEN_PLUGINS` 和 `RAVEN_OFFICE` 在容器启动前决定镜像、发布端口或构建参数。
仅将这些变量写入服务的 `.env.local`，不会覆盖这些设置。
可以在 `docker/` 中通过 Shell 环境变量传入，例如：

```bash
RAVEN_IMAGE=raven:local RAVEN_WEB_PORT=18893 docker compose up --build
```

也可以先创建 `.env.local`，再显式将其用于插值：

```bash
docker compose --env-file .env --env-file .env.local up --build
```

插值时，后一个文件覆盖前一个文件；Shell 环境变量的优先级高于这两个文件。

## 持久化数据 { #persistent-data }

Raven 将配置、工作区文件、会话、日志和记忆存储在 `raven-data` 卷的 `/data` 下。删除容器
不会删除该卷。下面的命令会同时删除卷及其中的全部数据：

```bash
docker compose down -v
```

## 运行 CLI 命令 { #run-a-cli-command }

除了 `run` 和 `signin` 之外的参数都会传给 Raven CLI：

```bash
docker compose exec raven docker-entrypoint.sh provider test anthropic
docker compose run --rm raven status
```

## 部署限制 { #deployment-limits }

- 默认 Compose 页面会向所有能访问发布端口的浏览器授予会话。请保持默认的
  `127.0.0.1` 绑定；如需远程暴露，请设置 `RAVEN_AUTO_LOGIN=0`，并在服务前配置访问控制代理。
- 镜像不负责 TLS 终止。远程部署时，请在容器前配置 HTTPS 反向代理。
- 镜像不包含 TUI 构建产物。`raven tui` 需要 `ui-tui/` 中的构建包。
- `sandbox` 可选依赖默认关闭，命令在容器内执行。如需同时使用 Boxlite，可通过
  `RAVEN_EXTRAS` 添加该依赖。
- 镜像包含用于办公文件预览的 LibreOffice。如果不需要预览办公文件，可以用
  `RAVEN_OFFICE=0` 构建。
