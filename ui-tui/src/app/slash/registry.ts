import type { SlashCommand } from './types.js'

import { slashAliases } from '../../i18n/index.js'
import { coreCommands } from './commands/core.js'
import { dagCommands } from './commands/dag.js'
import { debugCommands } from './commands/debug.js'
import { opsCommands } from './commands/ops.js'
import { sessionCommands } from './commands/session.js'
import { setupCommands } from './commands/setup.js'

export const SLASH_COMMANDS: SlashCommand[] = [
  ...coreCommands,
  ...sessionCommands,
  ...opsCommands,
  ...setupCommands,
  ...debugCommands,
  ...dagCommands
]

// Localized names are permanent aliases, not a replacement: a Chinese user can
// still type /model, and an English user's muscle memory never breaks when the
// language flips.
const byName = new Map<string, SlashCommand>(
  SLASH_COMMANDS.flatMap(cmd =>
    [cmd.name, ...(cmd.aliases ?? []), ...slashAliases(cmd.name)].map(name => [name.toLowerCase(), cmd] as const)
  )
)

export const findSlashCommand = (name: string) => byName.get(name.toLowerCase())
