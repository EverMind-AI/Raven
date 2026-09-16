/* Types for the page-state part, for the TypeScript modules that read it.
 *
 * Half of what this part exports is bound during its `install()` -- one
 * `let` statement destructured from the composer island's bag -- so the
 * compiler cannot infer a type for any of them from the JavaScript. The names
 * and shapes are the island's own; this declares them so a caller outside the
 * layer is type-checked against the island rather than against `any`.
 *
 * A shim with the same life as the layer: the C stage deletes the part, and
 * every caller reaches the island directly.
 */

import type * as turnMachine from '../../features/composer/turn'
import type { SessRow } from '../../features/rail/types'

export declare function stop_(): void
export declare function later(ms: number, fn: () => void): void
export declare function down(): void
export declare function sess(id: string | null | undefined): SessRow | undefined
export declare function confirmAsk(title: string, body: string, label: string, fn: () => void): void
export declare function ctxMenu(el: Element, items: () => unknown[]): void

/* The composer island's turn machine, as this part republishes it. */
export declare const turn: typeof turnMachine

export declare const sheetSession: () => string
export declare const sheetsForget: (key: string) => void
export declare const approveSheet: (
  prompt: string, yes: () => void, no: () => void, key?: string | null,
) => void
export declare const approvalSheet: (
  ask: { approvalId: string; command: string; description: string; suggestedPattern: string },
  answer: (choice: string, feedback: string, pattern: string) => void,
  key?: string | null,
) => void
export declare const approvalClose: (approvalId: string) => void
export declare const clarifySheet: (p: unknown, answer: (text: string) => void) => void
export declare const clarifyClose: (requestId: string) => void

export declare const queueDraw: () => void
export declare const queuePush: (text: string) => void
export declare const queueShift: () => string | undefined
export declare const queueClear: () => void
export declare const queueSnapshot: () => string[]
export declare const queueRestore: (items: string[]) => void

export declare const parkDraft: () => void
export declare const loadDraft: (id: string) => void
export declare const dropDraft: (id: string) => void
export declare const claimDraft: (id: string | null) => void

export declare const runState: { use: unknown }
export declare const upShade: (on: boolean) => void

export declare function install(): void
