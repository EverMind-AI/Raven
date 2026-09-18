# 自托管

Raven 可以直接从源码仓库运行，也可以作为单个 Docker Compose 服务运行。Compose 部署通过 nginx 提供已构建的页面，在同一个容器中运行 Raven 引擎及其子服务，并将持久化状态保存到命名卷中。

## 📝 前置条件

使用 Docker 部署时，请安装 Docker Engine 和 Docker Compose v2。使用源码部署时，请安装 Python 3.12、`uv`、Node.js 和 npm，并在启动引擎前安装仓库依赖。

## 🐳 使用 Docker Compose 启动

仓库中的 Compose 配置会在构建镜像时完成页面和 Python 环境的构建，因此无需在宿主机上单独构建：

```bash
cd docker
docker compose up
```

打开 <http://127.0.0.1:18793>。Compose 容器运行完整的 `gateway` 引擎，因此在 **设置 > 模型服务商（Settings > Model providers）** 中添加模型服务商后，无需重启即可在下一轮使用。

容器布局、登录流程、模型服务商配置和运维说明详见 [`docker/README.md`](https://github.com/EverMind-AI/Raven/blob/main/docker/README.md)。

## ⚙️ 配置

Docker 先读取 [`docker/.env`](https://github.com/EverMind-AI/Raven/blob/main/docker/.env) 中已提交的默认值，再加载可选的、被 Git 忽略的 `docker/.env.local` 覆盖这些默认值。请将凭据和部署相关的覆盖项写入 `.env.local`，不要写入已提交的文件。

Raven 将配置、会话、工作区、日志和记忆保存在 `RAVEN_HOME` 下。Compose 镜像通过 `raven-data` 卷将其映射到 `/data`。升级或重启时请保留该卷；`docker compose down -v` 会删除卷及其中的数据。

## 🛠️ 构建 Docker 镜像

使用 Makefile 目标构建镜像：

```bash
make docker-build
```

默认标签是 `raven:local`。如需选择其他标签或可选依赖集：

```bash
make docker-build DOCKER_IMAGE=raven:local
docker build -t raven:local --build-arg RAVEN_EXTRAS="channels,tools,sandbox" .
```

要通过 Compose 运行本地构建的镜像，请设置 `RAVEN_IMAGE=raven:local`（也可以在命令前直接设置该变量），然后在 `docker/` 目录运行 `docker compose up`。对应的 Makefile 快捷方式是 `RAVEN_IMAGE=raven:local make docker-up`。使用 `make docker-down` 停止服务。

## 🚀 从源码启动服务

在仓库根目录运行：

```bash
make install-deps
make build-ui
uv run raven web
```

`raven web` 会打开本地页面，并在终端退出后保持引擎运行。默认地址是 `http://127.0.0.1:18792`。调试时可以使用 `uv run raven web --foreground`，使用 `uv run raven web --stop` 停止常驻引擎。首次启动时可以暂时不配置模型，之后在 **设置 > 模型服务商（Settings > Model providers）** 中添加，或运行 `uv run raven onboard`。

如果只需要启动引擎而不打开浏览器页面，请使用 `uv run raven gateway`。
