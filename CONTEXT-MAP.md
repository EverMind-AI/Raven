# Context Map

## Contexts

- [Raven Runtime](./CONTEXT.md) — the Python agent runtime: channels, spine, agent loop, engines, providers
- [TUI](./ui-tui/CONTEXT.md) — the terminal frontend (`ui-tui/`, React/Ink); talks to the Runtime only via the RPC protocol
- [Products over ACP](./agents/README.md) -- the product-serving vocabulary: rendered config, state root, ACP home, tool-face pin, seed-once

## Relationships

- **TUI ↔ Runtime**: communicate exclusively over the RPC protocol (`raven/rpc/`); the TUI never imports Runtime internals
- **UI ↔ Runtime**: `ui-web/` is the served page, over the same protocol via `raven serve`'s WebSocket. Not a second front end for the desktop window: that window is a browser view of this page
- **bridge/ (WhatsApp TS)**: part of the Runtime context's channel boundary, not a separate context


## Architecture terms (routing)

The five-layer vocabulary lives in `CONTEXT.md`; look these up there:
**Kernel** (spine + contracts + tracing), **Paper** and the two tiers, **Assembly Root**
(`core/`, `build_runtime`), **Admission** and **Config-with-cargo**, **Channel Socket**,
**Generation** (the swap model), **Live preference** (`config/live.py`, the pull lane),
**Control Plane** (`rpc/control.py` / `gateway/live_probe.py`),
**Wire Schema** (`rpc-schema/openrpc.json`), **Layer Seats** (where every package sits and which
are deliberately unseated).
