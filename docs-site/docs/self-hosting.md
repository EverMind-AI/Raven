# Self-Hosting

Run Raven from a local source checkout or deploy it as a Docker Compose service.
The Compose deployment serves the WebUI through nginx, runs the Raven engine
and its child services in one container, and stores persistent data in a named
volume.

## <span class="em-section-icon em-section-icon--prerequisites" aria-hidden="true"></span>Prerequisites

Docker deployment requires Docker Engine and Docker Compose v2. To run from
source, install Python 3.12, `uv`, Node.js, and npm, then install the project
dependencies before starting the engine.

## <span class="em-section-icon em-section-icon--compose" aria-hidden="true"></span>Start with Docker Compose { #compose }

The Compose configuration builds the WebUI and Python environment into the
image. You do not need to build them separately on the host:

```bash
cd docker
docker compose up
```

Open <http://127.0.0.1:18793>. The container runs the full `gateway` engine.
Model providers added under **Settings > Model providers** are available on the
next turn without a restart.

See [Docker Deployment](docker.md) for container services, sign-in, provider
setup, and operational guidance.

## <span class="em-section-icon em-section-icon--configuration" aria-hidden="true"></span>Configuration

Compose loads the defaults in `docker/.env`, then applies overrides from the
optional `docker/.env.local` file. Store credentials and deployment-specific
settings in `.env.local`, which Git ignores.

Raven stores configuration, sessions, workspace files, logs, and memory under
`RAVEN_HOME`. In the Compose deployment, this directory is `/data`, backed by
the `raven-data` volume. Preserve the volume during upgrades and restarts.
`docker compose down -v` deletes the volume and all data it contains.

## <span class="em-section-icon em-section-icon--build" aria-hidden="true"></span>Build a Docker image

Build the image using the Makefile target:

```bash
make docker-build
```

The default image tag is `raven:local`. You can set the tag explicitly or choose
which optional dependencies to include:

```bash
make docker-build DOCKER_IMAGE=raven:local
docker build -t raven:local --build-arg RAVEN_EXTRAS="channels,tools,sandbox" .
```

To run the local image with Compose, set `RAVEN_IMAGE=raven:local` and run
`docker compose up` from `docker/`. You can export the variable or prefix the
command with the assignment. The equivalent Makefile command is
`RAVEN_IMAGE=raven:local make docker-up`. Stop the services with `make docker-down`.

## <span class="em-section-icon em-section-icon--source" aria-hidden="true"></span>Start the server from source { #from-source }

From the repository root:

```bash
make install-deps
make build-ui
uv run raven web
```

`raven web` opens the WebUI and keeps the engine running after the
terminal exits. It defaults to `http://127.0.0.1:18792`. Use
`uv run raven web --foreground` when debugging, or `uv run raven web --stop` to
stop the background engine. Raven can start before a model is configured;
add a provider under **Settings > Model providers** or run `uv run raven onboard`.

To start the engine without opening a browser, use
`uv run raven gateway`.
