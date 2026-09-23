/* Where a gate starts walking, and how it spells what it finds.
 *
 * Two facts about paths that every tree-walking gate needs and that neither
 * `node:url` nor `node:path` gives it on its own.
 *
 * A file URL's `pathname` is `/D:/repo/ui-web/src/` on Windows -- the leading
 * slash is part of the URL grammar, not of the path. `join` reads that as a
 * path relative to the current drive, so the walk lands on `D:\D:\repo\...`
 * and the gate fails with ENOENT against a tree that is plainly there. Every
 * gate below the repo's own scripts/ already takes its root the other way
 * (scripts/check-class-namespace.mjs, check-css.mjs); `root` is that way.
 *
 * And `relative` hands back the platform's separator, while every list a gate
 * pins -- `features/desk/store.ts`, `state/ws.ts -> features/desk/store.ts` --
 * is written with forward slashes, as the import specifiers they mirror are.
 * A backslash spelling does not match any of them and does not error either:
 * the ratchet silently counts nothing, reports nothing pinned as found, and
 * passes or fails on the wrong evidence. `relPath` is the one spelling they
 * all compare against.
 */

import { relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

/** The directory a `new URL(..., import.meta.url)` names, as a real path. */
export const root = (url) => fileURLToPath(url)

/** `path` under `from`, spelled the way every pinned list spells it. */
export const relPath = (from, path) => relative(from, path).split(sep).join('/')
