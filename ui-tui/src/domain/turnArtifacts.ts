// Normalize deliver_files metadata and file-tool arguments for TUI summaries.

import type { Msg, TurnArtifactFile, TurnArtifacts } from '../types.js'

const extOf = (name: string): string => {
  const at = name.lastIndexOf('.')
  return at > 0 ? name.slice(at + 1).toUpperCase() : 'FILE'
}

export const deliveryFiles = (metadata: unknown): TurnArtifactFile[] => {
  if (!metadata || typeof metadata !== 'object') {
    return []
  }
  const delivery = (metadata as Record<string, unknown>).raven_delivery
  if (!delivery || typeof delivery !== 'object') {
    return []
  }
  const files = (delivery as Record<string, unknown>).files
  if (!Array.isArray(files)) {
    return []
  }
  return files.flatMap(item => {
    if (!item || typeof item !== 'object') {
      return []
    }
    const row = item as Record<string, unknown>
    const path = String(row.path ?? '')
    const name = String(row.name ?? path.split('/').at(-1) ?? '')
    if (!name) {
      return []
    }
    return [{
      ext: extOf(name),
      missing: row.missing === true,
      name,
      size: Number(row.size) || 0,
      title: String(row.title || name)
    }]
  })
}

export const changedFile = (name: string, args: unknown): TurnArtifactFile | null => {
  if (name !== 'write_file' && name !== 'edit_file') {
    return null
  }
  if (!args || typeof args !== 'object') {
    return null
  }
  const values = args as Record<string, unknown>
  const path = String(values.path || values.file_path || '')
  if (!path) {
    return null
  }
  const file = path.split('/').at(-1) || path
  return { change: name === 'write_file' ? 'new' : 'edit', ext: extOf(file), name: file }
}

export const addUnique = (rows: TurnArtifactFile[], row: TurnArtifactFile): void => {
  const at = rows.findIndex(item => item.name === row.name)
  if (at >= 0) {rows[at] = row}
  else {rows.push(row)}
}

export const artifactMessage = (artifacts: TurnArtifacts): Msg | null =>
  artifacts.deliveries.length || artifacts.changes.length
    ? { artifacts, kind: 'artifacts', role: 'system', text: '' }
    : null
