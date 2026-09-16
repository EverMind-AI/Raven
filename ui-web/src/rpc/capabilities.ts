/* Where "this gateway is older than this page" is decided.
 *
 * Two different questions used to be answered in eleven scattered places.
 *
 *   - A METHOD the gateway does not serve AT ALL. It answers -32601 and the
 *     page must stop asking: `gone()` records the name, `has()` reads it back.
 *     That set was private to src/legacy/live/220-browser.js, which is why
 *     three other parts had to import the browser source to ask about
 *     sub-agents.
 *   - A FIELD an older gateway does not carry. Each is one condition at one
 *     site. Every one of them is named below and moved here unchanged, so the
 *     tolerances read as a list instead of being found by grep.
 *
 * `system.hello` is the third source and the coarsest: `server_capabilities`
 * announces "jsonrpc-2.0", "subscriptions" and "cli-dispatch" and nothing
 * finer (raven/rpc/methods/system.py), so `absorb()` records it and `serves()`
 * reads it -- and none of the field questions below can be answered from it.
 */

/** Names a call answered -32601 with. Absent, not merely unusable right now. */
const absent = new Set<string>()

/** What the last `system.hello` announced. */
let announced: ReadonlySet<string> = new Set()

/** Take the handshake's list. Called again on every reconnect's handshake. */
export function absorb(serverCapabilities: readonly string[] | null | undefined): void {
  announced = new Set(serverCapabilities ?? [])
}

/** Whether the handshake announced a capability by name. */
export function serves(capability: string): boolean {
  return announced.has(capability)
}

/**
 * Record a rejection against a name and answer whether that name is now known
 * absent. Only -32601 registers: a call that merely failed says nothing about
 * whether the surface exists, and treating a dropped socket as a missing
 * surface is what repaints a live run as "no delegated work".
 */
export function gone(name: string, err: unknown): boolean {
  if (err !== null && typeof err === 'object' && (err as { code?: unknown }).code === -32601) {
    absent.add(name)
  }
  return absent.has(name)
}

/** Whether the gateway may still be asked for a name. True until it refuses. */
export function has(name: string): boolean {
  return !absent.has(name)
}

/** Back to the state a fresh page starts in. For tests. */
export function resetCapabilities(): void {
  absent.clear()
  announced = new Set()
}

const field = (holder: unknown, name: string): unknown =>
  holder !== null && typeof holder === 'object' ? (holder as Record<string, unknown>)[name] : undefined

/* ── the eleven version tolerances, each one site's own condition ────── */

/**
 * The runtime timed the turn and sent `duration_ms` on `message.complete`.
 * Site: `duration` in src/state/session/runtime.ts, whose local subtraction is
 * the fallback for a gateway too old to send it (and for a stop, which ends a
 * turn without a `message.complete` at all).
 */
export const hasTurnDuration = (durationMs: unknown): boolean => durationMs != null

/**
 * A `tool.complete` frame carries the emit site's own verdict. Site: the
 * `tool.complete` stage in src/state/session/stages.ts, where the text
 * heuristic `okOf` survives only as the backstop for a gateway that omits the
 * field.
 */
export const hasToolOk = (ok: unknown): boolean => typeof ok === 'boolean'

/**
 * The gateway still sends `subagent.delivered`.
 *
 * Site: the `subagent.delivered` stage in src/state/session/stages.ts, which
 * is a deliberate no-op -- the row it would draw belongs to the `turn.started`
 * that follows. There is no condition at that site to move here: the page
 * accepts the frame from every gateway, and it always will while the stage does
 * nothing. Named so the tolerance is on this list and so the stage has one
 * place to ask. Always true today, because a notification never produces the
 * -32601 that `gone()` records.
 */
export const hasSubagentDelivered = (): boolean => has('subagent.delivered')

/**
 * The gateway sends `session.naming_ended`, so a quiet ending settles as fast
 * as a title does.
 *
 * Site: the 12s naming backstop in src/state/session/runtime.ts. The timer arms
 * for every gateway today and must go on doing so -- it covers a connection
 * that drops mid-turn as well as a gateway too old to send the event, and only
 * the first half of that is a version question -- so nothing consults this
 * yet. Narrowing the timer is a behaviour change, not a cleanup.
 */
export const hasNamingEnded = (): boolean => has('session.naming_ended')

/**
 * A `turn.send` answer carries the `naming` verdict at all. `=== false` is the
 * verdict and stays at the site: a gateway too old to carry the field says
 * nothing, and reading that as "declined" tears down a placeholder while a
 * title really is on its way. Sites: `dispatchSend` and the draft path of
 * `send` in src/state/session/runtime.ts.
 */
export const hasNamingFlag = (answer: unknown): boolean => field(answer, 'naming') !== undefined

/**
 * A `session.delete` answer carries `still_on_disk` at all. Whether it says
 * the file survived stays at the site, for the same reason as `naming`:
 * "nothing at all" is not "nothing was there", and reading it as the second
 * drops a row whose file may still exist. Sites: `removeSession` and
 * `deleteAllSessions` in src/features/rail/leave.ts, which must not disagree
 * about one answer.
 */
export const hasStillOnDisk = (answer: unknown): boolean => field(answer, 'still_on_disk') !== undefined

/**
 * A `system.version` answer says a newer build exists. Absent until the
 * gateway carries the field, and the notice row simply stays hidden -- an
 * older gateway degrades to no notice rather than to a broken one. Sites: the
 * boot check and its unawaited re-check in src/state/boot.ts, and the
 * settings page's own check button in src/legacy/live/120-settings.js.
 */
export const hasUpdateFlag = (version: unknown): boolean => !!field(version, 'update_available')

/**
 * The gateway has the `browser.frame` surface.
 *
 * Site: src/state/install.ts registers BOTH frame paths -- the
 * binary `RVF1` sink and the base64 `browser.frame` notify an older gateway
 * pushes instead -- because a page cannot know which one it will be sent until
 * a frame arrives. So the registration is unconditional today and nothing
 * consults this yet.
 */
export const hasBinaryFrames = (): boolean => has('browser.frame')

/**
 * A `subagents.list` row carries `building`. Always false from a current
 * gateway (the fork-era venv build is gone); carried for an older one, where
 * the row was the only place the page learned a build was in flight. Site:
 * `xaRowOf` in src/features/xa/source.ts.
 */
export const hasBuildFlag = (row: unknown): boolean => !!field(row, 'building')

/**
 * A `subagents.instance.history` answer carries its turns. An older gateway
 * answers with nothing at all, which is why `InstanceChat.turns` is optional.
 * Site: `toInstanceCtx` in src/features/subagents/history.ts.
 */
export function hasInstanceTurns<T>(turns: T[] | null | undefined): turns is T[] {
  return !!turns
}

/**
 * The gateway speaks `channels.*`. Site: the QR read in
 * src/features/connections/source.ts, which answers null when it does not, and
 * the island shows the same waiting frame the old panel kept.
 */
export const servesChannels = (): boolean => has('channels')
