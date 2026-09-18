# Self-Hosting

Raven can run directly from a checkout or as a single Docker Compose service. The
Compose deployment serves the built page through nginx, keeps the Raven engine
and its child services in one container, and stores durable state in a named
volume.

## 📝 Prerequisites

For a Docker deployment, install Docker Engine and Docker Compose v2. For a
source deployment, install Python 3.12, `uv`, Node.js, and npm. A source
checkout also needs the repository dependencies installed before starting the
engine.

## 🐳 Start with Docker Compose

The repository Compose setup builds the page and Python environment as part of
the image, so no separate host-side build is required:

```bash
cd docker
docker compose up
```

Open <http://127.0.0.1:18793>. The Compose container runs the full `gateway`
engine so providers added from **Settings > Model providers** are available on the next
turn without restarting.

For the detailed container layout, sign-in flow, provider setup, and operational
notes, see [`docker/README.md`](https://github.com/EverMind-AI/Raven/blob/main/docker/README.md).

## ⚙️ Configuration

Docker reads committed defaults from [`docker/.env`](https://github.com/EverMind-AI/Raven/blob/main/docker/.env), then loads
the optional, git-ignored `docker/.env.local` over them.
Put credentials and deployment-specific overrides in `.env.local`, not in the
committed file.

Raven stores its configuration, sessions, workspace, logs, and memory under
`RAVEN_HOME`. The Compose image maps this to `/data` through the `raven-data`
volume. Keep that volume for upgrades and restarts; `docker compose down -v`
deletes it and its data.

## 🛠️ Build a Docker image

Build the image using the Makefile target:

```bash
make docker-build
```

The default tag is `raven:local`. To select a different tag or optional
dependency set:

```bash
make docker-build DOCKER_IMAGE=raven:local
docker build -t raven:local --build-arg RAVEN_EXTRAS="channels,tools,sandbox" .
```

Run the locally built image through Compose by exporting
`RAVEN_IMAGE=raven:local` (or prefixing the command with that assignment) and
running `docker compose up` from `docker/`. The Makefile shortcut is
`RAVEN_IMAGE=raven:local make docker-up`. Stop the stack with `make docker-down`.


## 🚀 Start the server from source

From the repository root:

```bash
make install-deps
make build-ui
uv run raven web
```

`raven web` opens the local page and leaves the engine running after the
terminal exits. It defaults to `http://127.0.0.1:18792`. Use
`uv run raven web --foreground` when debugging, or `uv run raven web --stop` to
stop the resident engine. The first run can start without a configured model;
add one from **Settings > Model providers** or run `uv run raven onboard`.

To run only the engine without the browser launcher, use
`uv run raven gateway`.
