# Raven in Docker

```bash
cd docker
docker compose up --build
```

Then open <http://localhost:18793>. The local Compose default signs that browser
in automatically so Settings > Models is immediately available.

## What comes up

One container, two processes:

| process | what it does |
|---|---|
| nginx (`:80`) | serves the built page from `/app/ui-web/dist`, proxies `/rpc`, `/auth*`, `/health`, `/file`, `/files/`, `/oauth/callback` to the engine |
| the Raven engine | the RPC gateway on `127.0.0.1:18793` inside the container |

They share a container because the engine binds loopback only
(`web.TCPSite(runner, "127.0.0.1", port)`), so a proxy in a second container
would have nothing to dial. The image builds the page in a Node stage and the
Python environment with `uv sync --frozen`, so both halves come from the same
commit.

## Signing in

The engine mints a session credential per boot and the page needs a cookie for
it. Compose defaults `RAVEN_AUTO_LOGIN=1`, so nginx gives a browser loading the
page that credential automatically. This is intended for a locally published
port.

For a remotely exposed deployment, put `RAVEN_AUTO_LOGIN=0` in `.env.local`.
The entrypoint prints a one-time link on startup:

```
raven-docker: sign in once with this one-time link (replace localhost if you are remote):

    http://localhost:18793/auth#<nonce>
```

The cookie it sets lasts 30 days, and survives a restart -- the engine reloads
the same cookie from `/data` rather than minting a new one.

The page's `raven web` advice is for a host install. In a container, mint
another one-time link with:

```bash
docker compose exec raven docker-entrypoint.sh signin
```

## Configuring a provider

The engine starts with nothing configured. It says so on the way up:

```
! no provider is configured yet -- run `raven onboard` for guided setup
  Starting anyway. Add a provider in Settings > Models on the page.
```

Add one in the page's **Settings > Models** and the next turn uses it -- the
provider reads its credentials per call, so nothing needs restarting.

To have a deployment come up already configured, declare one in `.env.local`
instead:

```bash
RAVEN_PROVIDER=anthropic
RAVEN_API_KEY=sk-ant-...
```

The entrypoint writes that into config on every boot, so where it is set it is
the source of truth.

Local providers can be seeded by address without an API key:

```bash
RAVEN_PROVIDER=ollama-chat
RAVEN_API_KEY=
RAVEN_API_BASE=http://host.docker.internal:11434
```

The container always runs `raven gateway`, including the page, RPC, IM
channels, cron, and the proactivity stack. Its resolving provider re-reads
credentials on every call, which is what makes a provider added in Settings
work on the next turn.

## Settings

`.env` is committed and holds defaults; `.env.local` sits beside it, is
git-ignored, is loaded second and wins. Put every credential in `.env.local`.

## Data

Everything durable is under `/data` on the `raven-data` volume: `config.json`,
the workspace, sessions, logs and the memory store. Removing the container
keeps it; `docker compose down -v` does not.

## Running a CLI command

Any argument that is not `run` or `signin` is handed to the `raven` CLI:

```bash
docker compose exec raven docker-entrypoint.sh provider test anthropic
docker compose run --rm raven status
```

## Notes and limits

- **Exposure.** The default Compose page automatically grants a Raven browser
  session to anyone who can reach its port, so the published port binds
  `127.0.0.1` only. A remote deployment must choose its own bind or proxy and
  set `RAVEN_AUTO_LOGIN=0`. nginx also rewrites the `Origin` header so the
  engine accepts a browser that reached it through a published port. That
  drops a defence-in-depth layer; the session cookie is `SameSite=Strict`, so
  an unauthenticated socket is still refused. Publish this port only where you
  would have published the engine's own.
- **HTTPS.** Not configured here. Terminate TLS in front of this container; the
  page picks `wss://` from `location.protocol` on its own.
- **The TUI is not in this image.** `raven tui` needs the Node bundle from
  `ui-tui/`, which the image does not build.
- **`sandbox`.** The extra is off by default: inside a container the container
  is the boundary. Turn it on with `RAVEN_EXTRAS` if you want boxlite as well.
