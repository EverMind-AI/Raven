# Raven render worker

Build from the repository root:

```bash
docker build -f containers/render-worker/Dockerfile -t raven-render-worker:local .
```

Configure Raven with the resulting immutable image digest:

```yaml
tools:
  render:
    backend: auto
    workerImage: raven-render-worker@sha256:<digest>
```

The worker image is separate from the Agent sandbox. Each render job receives
only its staging directory at `/workspace`, and Raven disables VM networking
unless the render configuration explicitly enables it.
