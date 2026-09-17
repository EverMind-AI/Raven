/* The page-scoped owner of which conversation is open. A draft has no id, so
 * null is a real state rather than an absent initialization.
 */

let value: string | null = null
const listeners = new Set<(id: string | null) => void>()

export const current = (): string | null => value

export function setCurrent(id: string | null): void {
  if (value === id) return
  value = id
  for (const listener of listeners) listener(id)
}

export function onChange(listener: (id: string | null) => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function _resetForTests(): void {
  value = null
  listeners.clear()
}
