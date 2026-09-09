// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { useEffect, useRef, useState } from 'react'

import type { CompletionItem } from '../app/interfaces.js'
import type { SlashCommand } from '../app/slash/types.js'
import type { GatewayClient } from '../gatewayClientStub.js'
import type { CompletionResponse } from '../gatewayTypes.js'

import { SLASH_COMMANDS } from '../app/slash/registry.js'
import { looksLikeSlashCommand } from '../domain/slash.js'
import { asRpcResult } from '../lib/rpc.js'

const TAB_PATH_RE = /((?:["']?(?:[A-Za-z]:[\\/]|\.{1,2}\/|~\/|\/|@|[^"'`\s]+\/))[^\s]*)$/

export function slashCompletions(input: string, commands: SlashCommand[]): CompletionItem[] {
  if (!looksLikeSlashCommand(input)) {
    return []
  }

  // Once the user types past the command name (a space → arguments), there is
  // nothing left to complete — close the palette instead of lingering over
  // `/cmd arg`.
  if (/\s/.test(input.slice(1))) {
    return []
  }

  const token = input.slice(1).split(/\s/)[0]!.toLowerCase()
  const seen = new Set<string>()
  const items: CompletionItem[] = []

  for (const cmd of commands) {
    if (seen.has(cmd.name)) {
      continue
    }

    if (cmd.supported === false) {
      continue
    }

    const nameMatch = cmd.name.startsWith(token)
    const aliasMatch = !nameMatch && (cmd.aliases ?? []).some(a => a.startsWith(token))

    if (nameMatch || aliasMatch) {
      seen.add(cmd.name)

      const item: CompletionItem = { display: `/${cmd.name}`, text: `/${cmd.name}` }

      if (cmd.help) {
        item.meta = cmd.help
      }

      items.push(item)
    }
  }

  return items
}

export function completionRequestForInput(
  input: string
): { method: 'complete.path'; params: { word: string }; replaceFrom: number } | null {
  const isSlashCommand = looksLikeSlashCommand(input)
  const pathWord = isSlashCommand ? null : (input.match(TAB_PATH_RE)?.[1] ?? null)

  if (!pathWord) {
    return null
  }

  return {
    method: 'complete.path',
    params: { word: pathWord },
    replaceFrom: input.length - pathWord.length
  }
}

export function useCompletion(input: string, blocked: boolean, gw: GatewayClient) {
  const [completions, setCompletions] = useState<CompletionItem[]>([])
  const [compIdx, setCompIdx] = useState(0)
  const [compReplace, setCompReplace] = useState(0)
  const ref = useRef('')

  useEffect(() => {
    const clear = () => {
      setCompletions(prev => (prev.length ? [] : prev))
      setCompIdx(prev => (prev ? 0 : prev))
      setCompReplace(prev => (prev ? 0 : prev))
    }

    if (blocked) {
      ref.current = ''
      clear()

      return
    }

    if (input === ref.current) {
      return
    }

    ref.current = input

    if (looksLikeSlashCommand(input)) {
      const items = slashCompletions(input, SLASH_COMMANDS)

      if (items.length) {
        setCompletions(items)
        setCompIdx(0)
        setCompReplace(1)
      } else {
        clear()
      }

      return
    }

    const request = completionRequestForInput(input)

    if (!request) {
      clear()

      return
    }

    const t = setTimeout(() => {
      if (ref.current !== input) {
        return
      }

      gw.request<CompletionResponse>(request.method, request.params)
        .then(raw => {
          if (ref.current !== input) {
            return
          }

          const r = asRpcResult<CompletionResponse>(raw)

          setCompletions(r?.items ?? [])
          setCompIdx(0)
          setCompReplace(request.replaceFrom)
        })
        .catch((e: unknown) => {
          if (ref.current !== input) {
            return
          }

          setCompletions([
            {
              text: '',
              display: 'completion unavailable',
              meta: e instanceof Error && e.message ? e.message : 'unavailable'
            }
          ])
          setCompIdx(0)
          setCompReplace(request.replaceFrom)
        })
    }, 60)

    return () => clearTimeout(t)
  }, [blocked, gw, input])

  return { completions, compIdx, setCompIdx, compReplace }
}
