# File delivery tool for the main agent — design

**Date:** 2026-07-21
**Status:** Approved (design)
**Builds on:** the app service toolkit assembly (`src/agentscope/app/_service/_toolkit.py`), the sub-agent tool factory (`src/agentscope/subagent/_agent_tools.py`), the session router (`src/agentscope/app/_router/_session.py`), and the web UI tool-renderer / panel patterns (`examples/web_ui/frontend/`).

## 1. Motivation

When the main (leader) agent produces output files — a report, a dataset, a
chart — the user currently has no first-class way to *receive* them. Files live
on the server's per-session workspace dir, but nothing serves them over HTTP and
nothing in the chat UI surfaces them as downloadable artifacts (verified: there
is no `FileResponse`, `StaticFiles` mount, or download route anywhere in
`src/agentscope/`). The user must know the path and fetch it out of band.

This design adds a **file-delivery tool** the main agent calls with the list of
files it wants to hand to the user, plus the web-UI plumbing to **visualize the
deliverables and download them in one click** — both inline where the tool was
called and in a persistent, session-wide **Deliverables** panel.

## 2. Scope

**In scope:**
- A new leader-only `DeliverFiles` tool that takes a list of
  `{ path, title?, description? }` entries (workspace-relative paths), validates
  them, and emits a structured delivery *manifest*.
- New session-scoped HTTP download route(s) that serve workspace-confined files
  with a path-traversal guard, plus an archive ("Download all") route.
- Frontend: a dedicated tool renderer (inline delivery card), an aggregated
  Deliverables right-dock panel, and an authenticated download helper.
- Tests for the tool, the endpoints, the derive helper, and an end-to-end smoke.

**Out of scope / unchanged:**
- Delivering whole directories (rejected in v1; possible future enhancement).
- Snapshotting/copying delivered files into a staging dir — v1 uses **live
  references** (the download route reads current bytes at fetch time).
- Any change to sub-agent tools, DAG orchestration, team tools, or the event /
  message-bus / SSE machinery. We ride existing rails (`ToolResponse.metadata`
  → `ToolResultEndEvent.metadata` → reconstructed `tool_result` block).
- Cross-session or arbitrary-host-path delivery (workspace-confined only).

## 3. Architecture

Four cooperating pieces (three new, one small factory edit):

1. **`DeliverFiles` tool** (backend) — validates paths, builds the manifest,
   returns a `TextBlock` summary + `metadata.ravenx_delivery`.
2. **Download route(s)** (backend) — `GET /sessions/{session_id}/files/download`
   and `.../files/download-archive`, workspace-confined `FileResponse`/zip.
3. **Inline tool renderer** (frontend) — reads the manifest off the
   `tool_result` block, draws the grouped delivery card.
4. **Deliverables panel** (frontend) — derives the union of all delivery
   manifests across the session's messages and lists them with download buttons.

**Data flow:**

```
agent calls DeliverFiles
  → tool validates paths under workspace root, builds manifest (paths, not bytes)
  → ToolResponse(content=[TextBlock summary], metadata={ravenx_delivery: {...}})
  → ToolResultEndEvent.metadata  (live SSE)  /  ToolResultBlock.metadata (history)
  → frontend appendEvent copies metadata onto the tool_result block
  → renderer + panel read manifest, render cards + download buttons
  → user clicks → authenticated GET (X-User-ID) to download route
  → FileResponse bytes (re-validated under workspace root) → browser save
```

**Verified round-trip:** `ToolResponse.metadata` reaches
`ToolResultEndEvent.metadata` at `src/agentscope/agent/_agent.py:1798-1803`;
`ToolResultEndEvent.metadata` is a field (`src/agentscope/event/_event.py:379`);
the frontend SDK copies it onto the block
(`examples/web_ui/frontend/node_modules/@agentscope-ai/agentscope/src/message/message.ts:456`);
`ToolResultBlock.metadata` is a serialized field
(`src/agentscope/message/_block.py:167-184`) so it survives `GET /messages`.

**Known constraint (drives a design rule):** when a tool result is large enough
to trigger context offload, the *persisted* block is rebuilt **without**
`metadata` (`src/agentscope/agent/_agent.py:2240-2251`); the live SSE event is
unaffected. **Rule:** the manifest carries only paths + short strings +
`download_path`s — never file bytes — so it stays far under
`context_config.tool_result_limit` and round-trips to history intact.

## 4. Component 1 — the `DeliverFiles` tool

**Location:** `src/agentscope/app/_tool/_deliver_files.py`, exported from
`src/agentscope/app/_tool/__init__.py` (first-class app tool, sibling to the
team tools).

**Attach point:** constructed inside `make_subagent_tool_factory._factory`
(`src/agentscope/subagent/_agent_tools.py`, inner `_factory` at ~line 82) bound
to `(storage, workspace_manager, user_id, agent_id, session_id)` and appended to
the returned tool list — so it is leader/main-agent-only, and only the reference
service's leader session gets it (least-invasive attachment). The factory
already resolves the workspace there (`_resolve_workspace`, ~line 89), so the
tool can reuse or re-resolve the workdir.

**Flags:** `is_read_only = True`, `is_concurrency_safe = True` (it only `stat`s
files; never mutates the workspace). `check_permissions` returns an ALLOW
decision (benign, read-only, workspace-confined) — same pattern as
`CliSubAgentTool.check_permissions` (`src/agentscope/subagent/_tool.py:433`).

**`name`:** `DeliverFiles`. **`description`:** instructs the model to call it
with the final output files the user should receive; notes the UI renders a
downloadable card + Deliverables panel.

**`input_schema`:**

```jsonc
{
  "type": "object",
  "properties": {
    "files": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object",
        "properties": {
          "path":        { "type": "string", "description": "Workspace-relative path of the file to deliver." },
          "title":       { "type": "string", "description": "Optional human-friendly title shown in the UI." },
          "description": { "type": "string", "description": "Optional one-line note about the file." }
        },
        "required": ["path"]
      }
    },
    "message": { "type": "string", "description": "Optional note accompanying the whole delivery." }
  },
  "required": ["files"]
}
```

**`call()` behavior** (yields one terminal `ToolResponse`):

1. Resolve the session workspace root lazily:
   `session = storage.get_session(user_id, agent_id, session_id)` →
   `workspace_id = session.config.workspace_id`,
   `work_dir = session.config.work_dir` →
   `workspace = workspace_manager.get_workspace(user_id, agent_id, session_id, workspace_id, workdir=work_dir)` →
   `root = os.path.realpath(workspace.workdir)`. (Same lookup as
   `src/agentscope/subagent/_agent_tools.py:44-54`.) If the session/workspace
   cannot be resolved → terminal `state=ERROR` with a clear message.
2. For each entry, **validate + enrich**:
   - Reject absolute `path`.
   - `abs = os.path.realpath(os.path.join(root, path))`; require
     `os.path.commonpath([root, abs]) == root` (traversal + symlink-escape
     guard). Fail → invalid entry with reason.
   - Require `abs` exists and is a regular file (`os.path.isfile`). Fail →
     invalid with reason.
   - Derive `name = basename`, `size = os.path.getsize`,
     `media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"`.
   - `rel` = normalized workspace-relative path (POSIX-style).
   - `download_path = f"/sessions/{session_id}/files/download?path={quote(rel)}&agent_id={quote(agent_id)}"`.
   - Valid entry:
     `{ path: rel, name, title, description, size, media_type, download_path }`.
   - Dedup repeated `path`s within one call.
3. **Output:**
   - `content = [TextBlock(text=summary)]` — e.g.
     `"Delivered 3 file(s) to the user: report.pdf, data.csv, chart.png."`;
     if any failed, append `"Could not deliver: bad.txt (not found)."` so the
     model can self-correct.
   - `metadata = { "ravenx_delivery": { "message": message or None,
     "files": [valid...], "invalid": [{ path, reason }...] } }`.
   - `state = SUCCESS` if ≥1 delivered, else `ERROR`.

## 5. Component 2 — download endpoint(s) + security guard

**Location:** two routes added to `session_router`
(`src/agentscope/app/_router/_session.py`, prefix `/sessions`); a
`from fastapi.responses import FileResponse, StreamingResponse` import is added
(`StreamingResponse` already used for SSE at `_session.py:839`; `FileResponse`
is new). Dependencies mirror `list_messages`: `get_current_user_id`,
`get_storage`, `get_workspace_manager`
(`src/agentscope/app/deps.py:25,50,155`).

**Route 1 — single file:**
```
GET /sessions/{session_id}/files/download?path=<rel>&agent_id=<aid>
→ FileResponse(abs, filename=<basename>, media_type=<guessed>)
   # Content-Disposition: attachment; filename=... (set by FileResponse)
```

**Route 2 — archive ("Download all"):**
```
GET /sessions/{session_id}/files/download-archive?paths=<rel>&paths=<rel>&agent_id=<aid>
→ StreamingResponse(zip, media_type="application/zip",
                    headers={Content-Disposition: attachment; filename="deliverables.zip"})
```

**`agent_id` in the URL:** the workspace is resolved from the session record,
whose lookup is keyed by `(user_id, agent_id, session_id)`. The tool holds
`agent_id` and bakes it into every `download_path`. `user_id` **always** comes
from the `X-User-ID` header, never the URL — a caller cannot reach another
tenant's session by editing the query string.

**The security guard (applied per path, in both routes — the trust boundary):**
1. **Tenancy** — `user_id` from header (401 if absent);
   `storage.get_session(user_id, agent_id, session_id)` must exist for this
   user, else **404** (don't disclose existence).
2. **Resolve root** — `root = realpath(workspace.workdir)` for that session.
3. **Containment** — reject absolute `path`; `abs = realpath(join(root, path))`;
   require `commonpath([root, abs]) == root`, else **403**. Because it is
   `realpath`-based, a symlink inside the workspace pointing outward resolves
   externally and fails this check — symlink escape is covered.
4. **Existence/type** — `abs` must be an existing regular file, else **404**.
   Empty/missing `path` → **400**.
5. **Serve** — `FileResponse(abs, filename=basename, media_type=guess)`.

The tool already validated identically when building the manifest; the endpoint
**re-validates on every fetch** and trusts nothing from the query string.

**Archive semantics:** each requested path runs the full guard; missing/invalid
members are skipped and the rest zipped; if none remain → **404**.

## 6. Component 3 — frontend inline renderer + download helper

**Authenticated download helper** — new `src/api/files.ts`, barrel-exported from
`src/api/index.ts`:
- `downloadFromPath(downloadPath, filename)` — single-file download; uses the
  manifest's canonical `download_path` **directly** (the backend owns the URL
  contract, incl. the baked-in `agent_id` and URL-quoting; the frontend never
  reconstructs it).
- `downloadArchive(sessionId, agentId, relPaths[])` — builds the archive URL
  (`.../files/download-archive?agent_id=…&paths=…&paths=…`) from session context.
- Both perform an **authenticated `fetch`** (reusing `client`'s base URL from
  `localStorage.server_url` + the `X-User-ID` header from `api/client.ts`) →
  read `Blob` → `URL.createObjectURL` → programmatic `<a download>` click →
  revoke. A plain anchor is not used because the API requires the `X-User-ID`
  header, which anchor navigation cannot send.
- Failures (403/404/network) surface as a non-fatal error toast; never crash.

**Manifest type + extractor** — new `deriveDeliverables.ts` (mirrors the
existing `deriveInstances.ts`):
- `DeliveredFile { path, name, title?, description?, size, media_type, download_path }`,
  `DeliveryManifest { message?, files, invalid? }`.
- `readManifest(toolResultBlock)` → `block.metadata?.ravenx_delivery`.
- `deriveDeliverables(msgs)` → scans every `tool_result` block whose tool name is
  `DeliverFiles`, unions the files, **dedups by `path` (latest-wins)**.

**Inline tool renderer** — register `DeliverFiles` in the tool-renderer registry
(`examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx:21`).
Reads the manifest from the tool_result block and renders the grouped card:
- Header: `📦 Delivered N file(s)` + optional `message`.
- One `DeliveredFileRow` per file — media-type icon, `title || name`, `name` +
  humanized `size`, optional `description`, and a **Download** button →
  `downloadFromPath(file.download_path, file.name)`.
- **Download all** button (`downloadArchive`) when >1 file.
- `invalid` entries shown as a subdued "couldn't deliver: …" note.
- If the manifest is absent (e.g. the offload-strip edge case), fall back to a
  plain text summary of the tool call.

`DeliveredFileRow` is a new shared component (not the existing `FileAttachment`,
whose button uses a plain href and cannot send `X-User-ID`); it is reused by
both the card and the panel.

## 7. Component 4 — the Deliverables panel

- Add `'delivery'` to the `PanelKey` union
  (`examples/web_ui/frontend/src/components/panel/PanelDock.tsx:16`).
- In `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx`: add a
  `PanelDescriptor` (title "Deliverables", a `Package`/`Download` lucide icon),
  a dropdown checkbox toggle (alongside the existing panel toggles ~line 605),
  and register the content in the `panels` `useMemo` (~line 232).
- Content component `DeliverablesPanel` derives entirely from `msgs` via
  `deriveDeliverables` (the `SubagentInstanceMonitor`-over-`msgs` pattern — no
  new API call), listing every `DeliveredFileRow` across the session plus a
  **Download all**. Empty state: "No deliverables yet."
- A **count badge** on the panel toggle reflects the number of deliverables so
  the user sees them accrue. No forced auto-open (non-intrusive).

## 8. Error handling & edge cases

**Tool:** all-invalid → `state=ERROR` with per-path reasons; some-invalid →
`SUCCESS` with `invalid` populated + noted in text; empty `files` → schema
`minItems:1` (plus a code guard); directory/non-regular-file → invalid;
duplicate paths → deduped; session/workspace unresolvable → `state=ERROR`.

**Endpoint:** empty/missing `path` → 400; absolute / `../` / symlink-out → 403;
unknown-or-wrong-tenant session → 404; missing `X-User-ID` → 401; file missing
at fetch time → 404; archive with all members missing → 404.

**Live-reference consequence:** a file deleted/modified between delivery and
download yields a 404 or serves current bytes; the panel still lists it and the
UI shows a download error. Accepted and documented.

**Frontend:** download fetch failure → error toast, no crash; missing manifest →
text fallback (renderer) / entry omitted (panel); long lists → panel scrolls.

## 9. Testing & acceptance

**Backend (pytest, `tests/*_test.py`; whole-structure assertions per CLAUDE.md,
`AnyString`/`AnyValue` only where truly nondeterministic):**
- *Tool* (`tests/deliver_files_tool_test.py`): valid set → `ToolResponse`
  SUCCESS with the full expected manifest (`size`/`download_path` are
  deterministic against temp files); `../` traversal → excluded + invalid
  reason; absolute path → rejected; missing file → invalid; all-invalid →
  `state=ERROR`; mixed → SUCCESS + populated `invalid`; assert the serialized
  result stays small (won't trip offload).
- *Endpoint* (`tests/session_files_download_test.py`, FastAPI `TestClient`):
  happy path → 200, correct `Content-Disposition` + byte-for-byte body; `../` →
  403; absolute → 403/400; missing `X-User-ID` → 401; wrong-tenant/unknown
  session → 404; missing file → 404; **symlink-escape → 403**; archive → 200 zip
  with the expected members (+ a missing-member case).

**Frontend:** unit-test the pure `deriveDeliverables` helper (manifest
extraction, dedup/latest-wins) if the web_ui has a test runner (vitest);
otherwise rely on the end-to-end smoke.

**End-to-end acceptance smoke:** boot per the RavenX web-UI setup (ravenx env,
redis, service on :8001, `pnpm dev`); drive the leader agent to produce a file
and call `DeliverFiles`; confirm the inline card renders with title/size, the
**Download** button returns correct bytes, the Deliverables panel aggregates
across turns with a live count badge, and **Download all** yields a valid zip.
Run via the `verify` skill during implementation.

## 10. Files touched

**Backend (new):**
- `src/agentscope/app/_tool/_deliver_files.py` — the `DeliverFiles` tool.
- `tests/deliver_files_tool_test.py`, `tests/session_files_download_test.py`.

**Backend (edit):**
- `src/agentscope/app/_tool/__init__.py` — export `DeliverFiles`.
- `src/agentscope/subagent/_agent_tools.py` — construct + append `DeliverFiles`
  in `_factory`.
- `src/agentscope/app/_router/_session.py` — add the two download routes +
  `FileResponse` import.

**Frontend (new):**
- `src/api/files.ts` — authenticated download helper.
- `src/components/chat/tool-renderers/DeliverFiles.tsx` — inline card renderer.
- `src/components/delivery/DeliveredFileRow.tsx` — shared file row.
- `src/components/delivery/DeliverablesPanel.tsx` — panel content.
- `src/lib/deriveDeliverables.ts` (or alongside `deriveInstances.ts`) — types +
  derive helper.

**Frontend (edit):**
- `src/api/index.ts` — barrel-export files API.
- `src/components/chat/tool-renderers/index.tsx` — register `DeliverFiles`.
- `src/components/panel/PanelDock.tsx` — add `'delivery'` to `PanelKey`.
- `src/pages/chat/ChatViewport.tsx` — descriptor, toggle, badge, wiring.

No change to: event/SSE/message-bus machinery, sub-agent tools, DAG, team tools,
or the workspace managers.

## 11. Decisions

1. **Manifest in `ToolResponse.metadata` + a dedicated renderer** (Approach B,
   user choice) — the only option that carries agent-authored `title`/
   `description`, produces one grouped card + aggregated panel, and gets the
   auth header right on download. Fallback if the metadata round-trip ever fails:
   a `CustomEvent`-carried manifest, same overall shape. (2026-07-21)
2. **Inline card + Deliverables panel** (user choice) — contextual card where
   the tool was called plus a persistent, session-wide aggregation. (2026-07-21)
3. **Workspace-confined only** (user choice) — smallest attack surface; the
   endpoint is the trust boundary with a realpath-containment guard. (2026-07-21)
4. **Entry = `{ path, title?, description? }`** (user choice) — agent controls
   presentation; backend derives `name`/`size`/`media_type`. (2026-07-21)
5. **Live references, not staging** — v1 serves current bytes at fetch time; no
   snapshot copy. Simpler; per-session workspace persistence makes it adequate.
   Staging is a possible future enhancement. (2026-07-21)
6. **Manifest stays byte-free** — forced by the offload metadata-strip
   constraint (§3); bytes are fetched on demand via the download route only.
   (2026-07-21)
7. **Backend-abstracted file I/O, not host `os`** (user choice, 2026-07-21,
   from Task 1 review) — the tool and the download route resolve file
   existence/size and read bytes through `workspace.get_backend()`
   (`BackendBase.file_exists`/`is_dir`/`read_file`/`basename`/`isabs`/
   `normpath`/`abspath`), the same abstraction `Read`/`Write`/`Bash` use, so
   the feature works for every workspace backend (Local/Docker/E2B/K8s/
   Daytona/OpenSandbox) rather than only `LocalWorkspaceManager`. **This
   supersedes §5's `FileResponse`-from-host-path and `os.path.realpath`
   containment specifics:** the download route returns an in-memory
   `Response`/zip built from backend-read bytes, and containment is *logical*
   (reject absolute paths + normalized `..`-escape via `BackendBase.normpath`)
   rather than symlink-resolving for the *logical* first pass. Symlink escape
   is then closed by a targeted recheck (`_escapes_via_symlink`): for the
   host-local backend (`LocalBackend`, which reads the host filesystem and
   follows symlinks) the resolved `os.path.realpath` must stay under the real
   workspace root; sandbox backends confine symlinks to their container, so no
   host recheck is needed there. This restores the symlink defense the
   `os`-based design had, in both the tool and the download route. Reading a
   file to derive its `size`
   (no backend size primitive exists) is acceptable for typical deliverables.
   See the plan's "Design Amendment" for the corrected Task 1 + Task 2 code.
