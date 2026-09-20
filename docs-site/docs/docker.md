# Docker Deployment

Raven's Compose deployment builds the WebUI and Python environment into one
image. nginx serves the WebUI, while the Raven engine handles RPC, messaging
channels, scheduled tasks, and other gateway services.

## Start the container

From the repository root:

```bash
cd docker
docker compose up
```

Open `http://127.0.0.1:18793` after the container is ready. The default local
configuration signs you in automatically.

## What runs in the image

The deployment runs two processes in a single container:

| Process | Responsibility |
| --- | --- |
| nginx on port 80 | Serves the built WebUI and proxies browser requests to the engine. |
| Raven engine | Runs the RPC gateway on `127.0.0.1:18793` inside the container. |

The engine binds to loopback inside the container, so nginx and the engine stay
together. The WebUI and Python environment are built from the same source
checkout.

## Sign in

Compose sets `RAVEN_AUTO_LOGIN=1` by default, so browsers accessing the local
WebUI receive a session automatically. This setting is intended for a port
accessible only from the local machine.

For a remotely exposed deployment, put `RAVEN_AUTO_LOGIN=0` in `.env.local`.
The entrypoint prints a one-time sign-in URL when the container starts. The
session cookie is valid for 30 days and survives container restarts because
its credential is retained in the persistent data volume.

To create another sign-in URL inside the running container:

```bash
docker compose exec raven docker-entrypoint.sh signin
```

## Configure a provider

Unless a provider is preconfigured, add one under **Settings > Model providers**.
It becomes available on the next turn without a restart.

To configure a provider before startup, put these values in `.env.local`:

```bash
RAVEN_PROVIDER=anthropic
RAVEN_API_KEY=sk-ant-...
```

For a local provider that does not require an API key:

```bash
RAVEN_PROVIDER=ollama-chat
RAVEN_API_KEY=
RAVEN_API_BASE=http://host.docker.internal:11434
```

The entrypoint applies these provider values to Raven's configuration on each
startup. Values set here take precedence when the container restarts.

## Configure environment values

The `docker/.env` file contains committed defaults. The optional
`docker/.env.local` file loads second and overrides them. Store credentials in
`.env.local`, which Git ignores.

## Persistent data

Raven stores configuration, workspace files, sessions, logs, and memory under
`/data` in the `raven-data` volume. Removing the container keeps this volume.
The command below deletes the volume and all data in it:

```bash
docker compose down -v
```

## Run a CLI command

Arguments other than `run` and `signin` are passed to the Raven CLI:

```bash
docker compose exec raven docker-entrypoint.sh provider test anthropic
docker compose run --rm raven status
```

## Deployment limits

- The default Compose page grants a browser session to anyone who can reach
  the published port. Keep the default binding on `127.0.0.1`, or set
  `RAVEN_AUTO_LOGIN=0` and add a proxy with its own access control before
  exposing the service remotely.
- The image does not terminate TLS. Use an HTTPS reverse proxy in front of the
  container for remote deployments.
- The TUI is not built into the image. `raven tui` needs the bundle from
  `ui-tui/`.
- The `sandbox` extra is disabled by default; commands run within the container.
  Include it through `RAVEN_EXTRAS` if you also need Boxlite.
- LibreOffice is included for office-file previews. Build with
  `RAVEN_OFFICE=0` if your deployment does not need office-file previews.
