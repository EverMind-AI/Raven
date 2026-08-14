# Raven-side `deliver_files` tool — design

**Date:** 2026-07-30
**Status:** Draft (design)
**Supersedes, on the Raven side:** `2026-07-21-file-delivery-tool-design.md` (the
RavenX/AgentScope implementation). That spec stays as the record of the
AgentScope-side tool and of the frontend that already shipped; this one replaces
its backend half with a Raven-native design and lists the frontend contract
changes that follow.

## 1. Motivation

When the main agent produces an output file — a report, a dataset, a chart — the
user has no first-class way to receive it. The web UI half of this feature is
already in the repo (inline delivery card, session-wide Deliverables panel,
download helper), but its backend is the AgentScope `DeliverFiles` tool: there is
no `deliver_files` in `raven/agent/tools/`, and `ui-webui/service/` has no
download route. So in the current gateway-backed web app the feature is dead
code.

This design adds the Raven-native tool plus the transport that makes the existing
UI work again.

## 2. Scope

**In scope**

- A `deliver_files` tool in the Raven core, available **only on the `web`
  channel**.
- An optional structured-metadata channel from a tool to the turn stream
  (`ToolEvent.metadata`), so a manifest can reach the web UI and persist in
  message history.
- Two streaming HTTP routes on the gateway's existing aiohttp app, plus a
  persisted token registry that is the download trust boundary.
- A stream-proxy route in `ui-webui/service/` so the browser only ever talks to
  its own origin.
- The frontend contract changes required by the above.
- Tests for the tool, the store, the routes, the event plumbing, and the
  registration gates.

**Out of scope**

- Directory delivery (whole folders). Rejected here as it was in the 2026-07-21
  spec: recursive expansion, unbounded size, and a different frontend row model.
- Staging snapshots. Delivery stays a **live reference**: the download route
  reads current bytes at fetch time.
- A maximum delivery size. Deliberate (decision 6) — the manifest carries the
  size so the user decides before clicking.
- TUI delivery in any form. The tool is not registered outside the web surface.
- Any change to the DAG subsystem, sub-agent tools, or cron/sentinel.

## 3. Decisions

1. **Web-only, enforced twice** (2026-07-30) — registration is gated on the
   presence of the token store (absent outside the gateway's web channel), and
   the tool additionally refuses any turn whose `source.channel != "web"`. The
   first gate keeps the tool out of the model's schema list on the TUI/CLI; the
   second covers IM / cron / sentinel turns served by the same gateway
   `AgentLoop`.
2. **Bytes over a gateway HTTP route** (2026-07-30) — not over the WS RPC (1 MiB
   frame cap at `raven/web_rpc/server.py:28`, plus base64 inflation) and not by
   letting the web service read the filesystem directly (that would hard-code a
   shared-filesystem assumption that `RAVEN_GATEWAY_WS_URL` explicitly allows to
   be false).
3. **Manifest rides `ToolEvent.metadata`** (2026-07-30) — so it lands on the
   persisted `ToolResultBlock.metadata` and the Deliverables panel survives a
   page reload. The DAG-style out-of-band custom event was rejected because
   `_publish_custom` is SSE-only and does not enter history
   (`ui-webui/service/raven_gateway_agent.py:250`).
4. **The download endpoint serves only what was delivered** (2026-07-30) — the
   URL carries an opaque token, never a path. There is therefore no path to
   traverse and the endpoint is not a general file-read primitive. Path
   *selection* stays consistent with `read_file`/`write_file` (it honors
   `tools.restrict_to_workspace`), because the endpoint no longer depends on that
   setting for its safety.
5. **The token registry is persisted** (2026-07-30) — a gateway restart must not
   break download buttons that the (persisted) manifest still shows. Stateless
   signed tokens were the alternative; a store was chosen.
6. **No size cap, size always shown** (2026-07-30) — every hop is streaming, so
   large files are safe server-side; the manifest reports the size and the user
   decides. This is what makes the plain-anchor download (§8) mandatory rather
   than optional.
7. **`download_path` carries no host** (2026-07-30) — the manifest is persisted
   into message history, and a host baked in at delivery time can be wrong later
   (loopback-bound gateway, reverse proxy, browser on another machine). The
   origin is composed at click time.

## 4. Architecture

Five pieces: one new tool, one additive event field, one new HTTP surface, one
proxy route, one set of frontend contract edits.

```
model calls deliver_files(files=[{path, title?, description?}], message?)
  |
  |  channel != "web"  -> refuse immediately, touch nothing
  v
validate each entry via the same _resolve_path() read_file/write_file use
  |
  v
register each file in the persisted store -> opaque token
  |
  +--> return value to the model: a short text summary (names + sizes)
  |
  +--> manifest via Tool.take_metadata() -> ToolEvent.metadata
         -> tool.complete wire payload   (raven/rpc/spine.py)
         -> ToolResultEndEvent.metadata  (ui-webui/service/)
         -> persisted ToolResultBlock.metadata
         -> inline card + Deliverables panel (survives reload)
  |
  v
user clicks -> HEAD then GET  service:/raven/files/download?token=...
                 -> stream-proxy -> gateway:/files/download?token=...
                 -> aiohttp FileResponse (sendfile) -> browser saves
```

**Invariant: bytes never enter the manifest and never cross the WS.** The
manifest holds paths, names, sizes, media types and tokens only, so it stays far
below any truncation threshold and round-trips into history intact. Bytes travel
exclusively on the HTTP path, streamed end to end.

## 5. Component 1 — the `deliver_files` tool

**Location:** `raven/agent/tools/deliver.py`. **Name:** `deliver_files`
(snake_case, consistent with `read_file` / `run_subagent_dag`).

### 5.1 Registration gate

`AgentLoop` gains one optional collaborator:

```python
AgentLoop(..., deliverables: DeliverableStore | None = None)
```

`None` (the default) means the tool is not registered at all. Only
`raven/cli/gateway_commands.py` constructs a store, and only when
`config.gateway.web.enabled` is true. `raven/cli/tui_commands.py`,
`raven/cli/agent_commands.py` and every other host pass nothing.

This mirrors `_dag_progress_sink`, documented as `None` in non-web contexts
(`raven/agent/loop/main.py:480`). It is deliberately a collaborator rather than a
boolean: there is no way to switch the tool on without the store that makes it
work, so the assembly site is the single fact source. Registration happens in the same
pass that registers the filesystem tools (`raven/agent/loop/main.py:586-588`),
guarded by `self._deliverables is not None`.

Note this is independent of `tools.disabled_tools`; that list still removes the
tool by name if an operator wants it off inside the web app too.

### 5.2 Channel gate

`"deliver_files"` is added to the whitelist tuple in `_set_tool_context`
(`raven/agent/loop/main.py:1224`), in the branch that also passes the session key
(`raven/agent/loop/main.py:1230-1231`, alongside `spawn` / `deep_research` /
`run_subagent_dag`), so the tool receives
`set_context(channel, chat_id, session_key)` once per turn. It needs all three:
the channel for the gate below, and the session key as the `conversation` that
keys token reuse (§5.4) and the store record (§7.4). `_set_tool_context` runs before
the model call on **every** surface: `run_turn` — the spine-native entry used by
the web, the TUI and IM — delegates to `_process_message`
(`raven/agent/loop/main.py:2547`), which calls it at
`raven/agent/loop/main.py:2166`.

The channel is stored in a per-turn `ContextVar` (the `spawn.py:41` pattern), not
an instance attribute, so concurrent turns cannot read each other's context.

`execute()` checks it first and returns, without touching the filesystem:

```
Error: deliver_files is only available on the web UI channel (current channel:
whatsapp). Give the user the file path instead.
```

The wording lets the model degrade gracefully instead of retrying.

### 5.3 Input schema

```jsonc
{
  "type": "object",
  "properties": {
    "files": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object",
        "properties": {
          "path":        { "type": "string" },
          "title":       { "type": "string" },
          "description": { "type": "string" }
        },
        "required": ["path"]
      }
    },
    "message": { "type": "string" }
  },
  "required": ["files"]
}
```

### 5.4 Per-entry validation

Resolution reuses `_resolve_path(path, workspace, allowed_dir)` from
`raven/agent/tools/filesystem.py:10-21` — the same function `read_file` and
`write_file` use, with the same `allowed_dir` the loop computes at
`raven/agent/loop/main.py:586` (`self.workspace` when
`tools.restrict_to_workspace`, else `None`). One fact source for "what the agent
may touch".

1. `PermissionError` from the containment check, or an unresolvable path →
   `invalid` with the reason.
2. Not an existing regular file → `invalid` with the reason.
3. Derive `name` (basename), `size` (`stat().st_size` — the file is never read),
   `media_type` (`mimetypes.guess_type`, default
   `application/octet-stream`).
4. Register in the store, obtaining a token. Re-delivering the same path within
   the same conversation **reuses the existing token** (idempotent); otherwise a
   new one is minted.
5. Duplicate paths inside one call are de-duplicated.

**Tokens must be random**, `secrets.token_urlsafe(32)`. A token derived from the
path would be guessable from a guessable input, which would dissolve the trust
boundary of decision 4.

### 5.5 Outputs

Return value to the model (short, includes sizes since there is no cap):

```
Delivered 3 file(s): report.pdf (2.1 MB), data.csv (48 KB), chart.png (310 KB).
Could not deliver: draft.md (not found).
```

Manifest handed back via the hook of §6:

```jsonc
{ "raven_delivery": {
    "message": "<optional>",
    "files": [{ "path", "name", "title", "description", "size", "media_type",
                "token", "download_path" }],
    "invalid": [{ "path", "reason" }] } }
```

`download_path` is `"/files/download?token=<token>"` — origin-less per decision
7. `token` is also a first-class field so the frontend never has to parse the
URL.

**Flags:** read-only (`stat` only, never mutates), not a blocking interaction,
default `timeout_seconds`.

## 6. Component 2 — the manifest channel

`Tool.execute()` returns `str` (`raven/agent/tools/base.py:65`) and `ToolEvent`
has no metadata field (`raven/spine/events.py:64-76`), so a structured payload
has nowhere to go today. Encoding it in the return string is not an option: the
wire preview is hard-truncated to 200 characters at
`raven/agent/loop/main.py:1720`.

Three additive changes:

1. **`raven/agent/tools/base.py`** — an optional hook:

   ```python
   def take_metadata(self) -> dict[str, Any] | None:
       """Structured payload for the turn stream, consumed once per call."""
       return None
   ```

   Default `None`, so no existing tool changes. `deliver_files` overrides it,
   returning (and clearing) the manifest it stashed in a per-turn `ContextVar`
   during `execute()`.

2. **`raven/spine/events.py`** — `ToolEvent` gains
   `metadata: dict[str, Any] | None = None`. Additive on a frozen dataclass with
   a default, so every existing construction site keeps working.

3. **`raven/agent/loop/main.py:1727-1735`** — after `self.tools.execute(...)`,
   fetch the tool instance (`self.tools.get(name)`, the pattern already used for
   `cron` / `deep_research`, `raven/agent/tools/registry.py:33`) and, if it
   exposes `take_metadata`, put the result on the emitted tool event.

Then the two transport hops:

- `raven/rpc/spine.py:151-161` — the single `tool.complete` serialization
  site, shared by the TUI and the web (`build_web` reuses `build_rpc_spine`,
  `raven/web_rpc/spine.py:33`) — adds `"metadata": out.metadata` to the payload.
- `ui-webui/service/raven_gateway_agent.py:367-371` — merges it into the
  existing `ToolResultEndEvent(metadata=...)`, which today carries only
  `truncated`.

From there the manifest lands on `ToolResultBlock.metadata` and survives
`GET /messages`, which is what keeps the Deliverables panel populated after a
reload.

The TUI is unaffected: it receives a `metadata` key it does not read.

## 7. Component 3 — gateway HTTP routes and the token store

### 7.1 Routes

Added to the aiohttp app the gateway already builds
(`raven/web_rpc/server.py:141-142`, currently `/ws` only). Both must be
registered for **GET and HEAD** (the frontend pre-checks with HEAD, §9):

```
/files/download?token=<t>                    -> one file, streamed
/files/download-archive?token=<t>&token=<t>  -> zip of several
```

### 7.2 Authorization

The token is the capability: 32 random bytes, unguessable, and the only handle
that exists. No additional `auth_token` is required — that is a long-lived shared
secret and putting it in a URL would leak it into browser history, a strictly
worse position. The gateway remains loopback-bound, which
`raven/config/schema.py:460-472` states is by design ("Single-user by design:
bound to loopback, not exposed off-box").

### 7.3 Request handling

1. Missing or unknown token → **404** (never disclose existence).
2. Known token whose file no longer exists → **404**, and the entry is pruned in
   passing.
3. Otherwise → `web.FileResponse(path, headers={"Content-Disposition":
   'attachment; filename="<name>"'})`, which streams via sendfile.
4. Archive: every token runs the same checks; missing members are skipped, and
   if none survive → **404**. The zip is spooled to a temp file under
   `get_cache_dir()` and then streamed, then deleted — for an unbounded-size
   feature, one disk copy is the right trade against assembling a zip in memory.

### 7.4 The store

- Path: `raven/config/paths.py` gains `get_deliverables_path()`, same shape as
  the existing `get_cron_dir()` / `get_sentinel_dir()` helpers.
- Record: `{ token: { path, name, media_type, size, conversation, created_at } }`.
- Writes: temp file plus `os.replace`, mirroring
  `raven/agent/subagent/instances.py:67` (a local 3-line pattern rather than a
  cross-package import of `raven/evolver/launch/state.py`).
- Startup: load into memory, then **prune entries whose file no longer exists**.
  No TTL — a deliverable stays fetchable for as long as the file does, which is
  the live-reference stance of §2.
- Lookup by `(conversation, path)` supports the token reuse of §5.4.
- Only the single gateway process writes, so there is no cross-process race.

## 8. Component 4 — the service stream-proxy

The browser talks to the service origin it is already configured with, and the
service relays to the gateway:

```
browser -> GET <service>/raven/files/download{,-archive}?token=...
        -> streaming proxy -> <gateway http>/files/download{,-archive}?token=...
```

The gateway HTTP base is derived from the `RAVEN_GATEWAY_WS_URL` the service
already holds (`ws`/`wss` -> `http`/`https`, drop the `/ws` suffix).

Why a proxy rather than the browser hitting the gateway directly: the gateway is
loopback-bound by design, so `http://127.0.0.1:8765/...` resolves to the
*browser's* localhost whenever the browser is not on the gateway host. Fixing
that by exposing the gateway off-box plus configuring CORS contradicts the
config's own stance. The proxy also means there is no cross-origin request at
all, so no CORS surface is added anywhere.

This does not weaken decision 2: the gateway still owns file access and
authorization; the service resolves no paths and reads no files, it forwards a
token and relays a stream. Both hops stream, so memory stays bounded regardless
of file size.

The route mounts on the existing `/raven` router, which
`ui-webui/service/main.py` already installs only when the gateway is enabled —
matching the tool's own availability.

## 9. Component 5 — frontend changes

The existing UI cannot be reused untouched: it is written against the AgentScope
contract (PascalCase tool name, `ravenx_delivery` key, and a `download_path` it
reverse-engineers with a regex).

| File | Change |
|---|---|
| `frontend/src/components/delivery/deriveDeliverables.ts` | `DELIVER_FILES_TOOL`: `'DeliverFiles'` -> `'deliver_files'`; `readManifest` reads `raven_delivery`; add `token` to `DeliveredFile`; **delete** `parseDeliveryContext`; `downloadAllDeliverables` collects tokens |
| `frontend/src/api/files.ts` | `downloadFromPath`: fetch+blob -> service base + plain anchor (reuse the existing `triggerDownload`); `downloadArchive(sessionId, agentId, paths)` -> `downloadArchive(tokens)`; add the HEAD pre-check |
| `frontend/src/components/chat/tool-renderers/index.tsx:31` | literal key `DeliverFiles:` -> computed `[DELIVER_FILES_TOOL]:`, so the constant is the single fact source |
| `frontend/src/components/delivery/DeliveredFileRow.tsx` | download click uses the new helper |
| `frontend/src/i18n/locales/{en,zh}.json` | one new download-failure string; the three `deliverFiles*` keys already exist. Targeted edits only, never a full re-dump |

`MessageBubble.tsx` needs no edit: it imports the constant (`:23`) and uses it in
`INLINE_TOOL_NAMES` (`:78`), so changing the constant's value is enough.

**Why the plain anchor, and its cost.** A blob download would have to buffer the
whole file in the tab, which decision 6 (no size cap) makes untenable; an anchor
streams to disk natively and, being same-origin with a token in the URL, needs no
custom header. The cost is losing `fetch` error handling — a 404 would surface as
a blank tab. Mitigation: on click, issue a same-origin `HEAD` first; a non-200
raises a toast and no download is triggered.

## 10. Error handling and edge cases

**Tool:** non-web channel → refusal string, no filesystem access. All entries
invalid → an error-shaped result listing every reason. Some invalid → success
plus a populated `invalid` list, noted in the text so the model can self-correct.
Empty `files` → schema `minItems: 1` plus a code guard. Directory or non-regular
file → invalid. Store unavailable → the tool is not registered, so the call
cannot happen.

**Routes:** unknown token → 404. File deleted after delivery → 404 and the entry
is pruned. Archive with every member missing → 404. Malformed query (no token) →
400.

**Live-reference consequence:** a file modified between delivery and download is
served with its current bytes, and a deleted one 404s while the panel still lists
it. Accepted; the HEAD pre-check turns the second case into a toast rather than a
broken tab.

**Frontend:** missing manifest (a tool result that predates this feature, or an
error result) → the renderer falls back to a plain text summary, and the panel
omits the entry.

## 11. Testing and acceptance

Python, `uv run pytest`, naming per repo section 5.1:

| File | Covers |
|---|---|
| `tests/test_deliver_files_tool.py` | full manifest shape on the happy path; missing file / non-regular file / outside `allowed_dir` when `restrict_to_workspace` → `invalid` with reason; duplicate paths de-duplicated; re-delivery in one conversation reuses the token; two different paths get different tokens **not derivable from the path**; `channel != "web"` → refusal **and no filesystem access even for a path that exists** |
| `tests/test_deliverable_store.py` | atomic write; a token still resolves after a reload (the core promise of decision 5); startup pruning drops entries whose file is gone |
| `tests/test_web_rpc_files_download.py` | unknown token → 404; known token with a deleted file → 404 and entry pruned; happy path → 200 with the right `Content-Disposition` and byte-identical body; HEAD agrees with GET; archive → zip with the expected members, missing members skipped, all-missing → 404 |
| `tests/test_rpc_tool_events.py` (existing) | `ToolEvent.metadata` reaches the `tool.complete` payload |
| `tests/test_cli_tui_commands.py` (existing) | no store → the TUI does **not** register `deliver_files` |
| `tests/test_cli_gateway_commands.py` (existing) | web channel enabled → registered; disabled → not registered |
| `tests/integration/test_file_delivery_smoke.py` | real aiohttp app plus a real temp file: register, download, compare bytes |

Frontend (no JS test runner): `pnpm -C frontend lint` with 0 errors, and
`pnpm -C frontend build`.

Manual acceptance, with `RAVEN_GATEWAY=1` via `../start_webapp.sh`: drive the
agent to produce a file and call `deliver_files`; the inline card appears with
names and sizes; download returns correct bytes; **reload the page — the panel is
still populated and the buttons still work** (the joint acceptance point for
decisions 3 and 5); **restart the gateway — old buttons still work**; "download
all" yields a valid zip; and on a non-web channel the tool refuses with the
guidance string.

## 12. Files touched

**New (Raven core)**

- `raven/agent/tools/deliver.py` — the tool.
- the `DeliverableStore` (alongside the tool, or `raven/agent/tools/_deliverables.py`).
- gateway HTTP handlers (in `raven/web_rpc/`, next to the WS server).

**Edited (Raven core)**

- `raven/agent/tools/base.py` — the `take_metadata` hook.
- `raven/spine/events.py` — `ToolEvent.metadata`.
- `raven/agent/loop/main.py` — `deliverables` ctor param, registration guard,
  `_set_tool_context` whitelist, metadata pickup on the tool event.
- `raven/rpc/spine.py` — serialize `metadata` on `tool.complete`.
- `raven/web_rpc/server.py` — register the two routes (GET + HEAD).
- `raven/cli/gateway_commands.py` — build the store when the web channel is on
  and pass it to `AgentLoop`.
- `raven/config/paths.py` — `get_deliverables_path()`.

**Edited (ui-webui)**

- `ui-webui/service/raven_gateway_agent.py` — merge wire metadata into
  `ToolResultEndEvent.metadata`.
- `ui-webui/service/` — the stream-proxy route on the `/raven` router.
- the five frontend files of section 9.

**New (tests)** — the four new files of section 11, plus additions to the three
existing ones.

Unchanged: the DAG subsystem, sub-agent tools, cron/sentinel, the IM channels,
and the TUI (which receives one wire key it ignores).

## 13. Known gaps, as merged

Recorded here because they were adjudicated during implementation and are not
visible from the code or the git history.

**Never exercised against a live stack.** No part of this feature has run
against a real gateway, a real web service, or a browser. Everything below the
manifest is covered by unit and in-process HTTP tests only; `ui-webui/service/`
has no test runner at all, and the frontend has none either, so the proxy and
the delivery UI are covered by type-checking and lint alone. Two of the defects
found in final review — a non-ASCII filename returning 500 through the proxy,
and a `server_url` with a trailing slash saving a 404 body as the file — lived
in exactly that unexercised region. Both are fixed; neither fix has been seen
working in a browser.

**Deferred defects.**

- `raven/agent/tools/deliver.py` — the non-web-channel and empty-`files` early
  returns leave `_pending` untouched, so a manifest stranded by an aborted turn
  could still be popped by a later such call. Real, but far narrower than the
  forwarder case that was fixed.
- The concurrency test in `tests/test_deliver_files_tool.py` **documents** the
  session-keyed manifest handoff; it does not guard it. On Python 3.13
  `asyncio.wait_for` no longer wraps a coroutine in a Task and `execute` never
  suspends, so the two turns run strictly sequentially and no child-context copy
  occurs. Breaking `_pending` to a single slot leaves the test green. Do not
  cite it as coverage. The property itself was verified correct independently.
- Cosmetic: `MessageBubble.tsx` and `DeliverFilesRenderer.tsx` still name the
  old PascalCase `DeliverFiles` in doc comments; `_attachment` emits a redundant
  `filename*` for a name with leading or trailing spaces.

**Security surface to revisit if the service is ever exposed off-host.** The
download token is the only capability check: the routes carry no auth, the
service allows any origin, and the token travels in a query string, so it
reaches access logs and browser history. `tools.restrict_to_workspace` defaults
to `False`, which means the tool can register any file the agent can read and
the gateway will then stream it over HTTP — not a new capability for the model,
but a new network surface. All of this is coherent under the single-user,
loopback-bound assumption this design was built on, and only under it.

## 14. Related work

`tools.disabled_tools` was not forwarded to the TUI's `AgentLoop`, so a
configured blacklist was silently inert there. Fixed separately on
`fix/tui_disabled_tools_honored` (`raven/cli/tui_commands.py`, plus a regression
test in `tests/test_cli_tui_commands.py`). This design does not depend on that
fix — its gates are the store's presence and the channel check — but the two were
found together.
