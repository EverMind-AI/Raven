# 自托管

Raven 支持从本地源码运行，也支持通过 Docker Compose 部署。Compose 部署通过 nginx 提供 WebUI，在同一个容器中运行 Raven 引擎及其子服务，并将持久化数据保存在命名卷中。

## <span class="em-section-icon em-section-icon--prerequisites" aria-hidden="true"></span>前置条件 { #prerequisites }

Docker 部署需要 Docker Engine 和 Docker Compose v2。从源码运行需要 Python 3.12、`uv`、Node.js 和 npm；启动引擎前，还需安装项目依赖。

## <span class="em-section-icon em-section-icon--compose" aria-hidden="true"></span>使用 Docker Compose 启动 { #compose }

Compose 配置会将 WebUI 和 Python 环境一并构建到镜像中，无需在宿主机上分别构建：

```bash
cd docker
docker compose up
```

打开 <http://127.0.0.1:18793>。容器运行完整的 `gateway` 引擎。在 **设置 > 模型服务商（Settings > Model providers）** 中添加的模型服务商会在下一轮生效，无需重启。

容器服务、登录方式、模型服务商配置和运维说明，请参阅 [Docker 部署](docker.md)。

## <span class="em-section-icon em-section-icon--configuration" aria-hidden="true"></span>配置 { #configuration }

Compose 先加载 `docker/.env` 中的默认值，再应用可选文件 `docker/.env.local` 中的覆盖配置。凭据和部署专用配置应写入 `.env.local`，该文件会被 Git 忽略。

Raven 将配置、会话、工作区文件、日志和记忆保存在 `RAVEN_HOME` 下。Compose 部署中，该目录为 `/data`，由 `raven-data` 卷提供持久化存储。升级或重启时请保留该卷。`docker compose down -v` 会删除卷及其中的全部数据。

## <span class="em-section-icon em-section-icon--build" aria-hidden="true"></span>构建 Docker 镜像 { #build-a-docker-image }

使用 Makefile 目标构建镜像：

```bash
make docker-build
```

默认镜像标签为 `raven:local`。你也可以显式指定标签，或选择要包含的可选依赖：

```bash
make docker-build DOCKER_IMAGE=raven:local
docker build -t raven:local --build-arg RAVEN_EXTRAS="channels,tools,sandbox" .
```

要通过 Compose 运行本地镜像，请设置 `RAVEN_IMAGE=raven:local`，然后在 `docker/` 目录运行 `docker compose up`。可以先导出该环境变量，也可以将赋值写在命令前。对应的 Makefile 命令为 `RAVEN_IMAGE=raven:local make docker-up`。运行 `make docker-down` 可停止服务。

## <span class="em-section-icon em-section-icon--source" aria-hidden="true"></span>从源码启动服务 { #from-source }

在仓库根目录运行：

```bash
make install-deps
make build-ui
uv run raven web
```

`raven web` 会打开 WebUI，并在终端退出后保持引擎运行。默认地址为 `http://127.0.0.1:18792`。调试时可使用 `uv run raven web --foreground`，停止后台引擎可使用 `uv run raven web --stop`。Raven 可以在尚未配置模型时启动；之后在 **设置 > 模型服务商（Settings > Model providers）** 中添加配置，或运行 `uv run raven onboard`。

如需启动引擎但不打开浏览器，请使用 `uv run raven gateway`。
