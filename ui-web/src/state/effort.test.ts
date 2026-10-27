// @vitest-environment happy-dom
/* The conversation's reasoning effort (state/effort.ts): what the server's
   answer becomes on this side -- whether there is a row to draw, the rungs and
   their words, what the default rung stands for -- and how a pick moves. The
   row that draws it is the model picker's (features/model/ModelPicker.tsx,
   its own test); nothing here touches a document. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import * as effort from './effort'
import { resetSources, setSources } from './sources'

import type { EffortReply, EffortSource } from './effort'

const toastWriter = vi.hoisted(() => ({ items: [] as string[] }))
vi.mock('./toast', () => ({
  show: (text: string) => { toastWriter.items.push(text) },
}))

const WORDS = ['minimal', 'low', 'medium', 'high']

let asked: Array<string | null | undefined>
/* What each read was about: the conversation and the switch it named. */
let reads: Array<[string | null | undefined, number | undefined]>
let answer: (effort: string | null | undefined) => Promise<EffortReply | null>

function source(): void {
  const src: EffortSource = {
    read: (sid, gen) => { asked.push(undefined); reads.push([sid, gen]); return answer(undefined) },
    set: (id) => { asked.push(id); return answer(id) as Promise<EffortReply> },
  }
  setSources({ effort: src })
}

beforeEach(() => {
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  effort._resetForTests()
  toastWriter.items = []
  asked = []
  reads = []
  answer = async (id) => ({ session_key: 'cli:one', effort: id === undefined ? null : id, default: 'low', options: WORDS })
  source()
})

afterEach(() => {
  resetTranslator()
  effort._resetForTests()
  resetSources()
})

const names = (): string[] => effort.options().map((r) => r.name)
const ticked = (): string[] => effort.options().filter((r) => r.ticked).map((r) => r.id)

describe('the reasoning effort', () => {
  it('offers nothing until the server answers', () => {
    expect(effort.offered()).toBe(false)
    expect(effort.options()).toEqual([])
  })

  it('offers the default rung first, then every word the server lists, in the reader\'s words', async () => {
    await effort.load()
    expect(effort.offered()).toBe(true)
    expect(names()).toEqual(['gui.effort.default', 'gui.effort.minimal', 'gui.effort.low', 'gui.effort.medium', 'gui.effort.high'])
    expect(ticked()).toEqual([''])
    expect(effort.current()).toBe('')
    expect(effort.fallback()).toBe('low')
  })

  it('says what the default rung stands for on this install', async () => {
    await effort.load()
    expect(effort.scopeNote()).toBe('gui.effort.scope gui.effort.now {"name":"gui.effort.low"}')
    answer = async () => ({ effort: null, default: null, options: WORDS })
    await effort.load()
    /* No configured effort: the provider's own, which has no rung. */
    expect(effort.scopeNote()).toBe('gui.effort.scope gui.effort.now {"name":"gui.effort.provider"}')
  })

  it('reads the conversation it was given under the switch it was given, not the one on screen', async () => {
    /* A switch asks for the conversation being opened before the page's
       current session moves over to it: the source is told which, so it does
       not ask about the one being left. */
    await effort.load('cli:two', 7)
    expect(reads).toEqual([['cli:two', 7]])
    await effort.load(null, 8)
    expect(reads).toEqual([['cli:two', 7], [null, 8]])
    await effort.load()
    expect(reads).toEqual([['cli:two', 7], [null, 8], [undefined, undefined]])
  })

  it('paints nothing from a read the source dropped, and keeps what it had', async () => {
    await effort.load()
    expect(ticked()).toEqual([''])
    /* The source answers null for a read whose switch was overtaken (the live
       one, features/model/source.ts): nothing to paint, nothing lost. */
    answer = async () => null
    await effort.load('cli:gone', 9)
    expect(effort.offered()).toBe(true)
    expect(ticked()).toEqual([''])
  })

  it('offers nothing when the read fails, which is what an older gateway answers', async () => {
    answer = async () => { throw new Error('-32601') }
    await effort.load()
    expect(effort.offered()).toBe(false)
  })

  it('offers nothing for an answer with no words', async () => {
    answer = async () => ({ effort: null, default: null, options: [] })
    await effort.load()
    expect(effort.offered()).toBe(false)
  })

  it('shows a word this build does not know as the server spelt it', async () => {
    answer = async () => ({ effort: 'xhigh', default: null, options: [...WORDS, 'xhigh'] })
    await effort.load()
    expect(names()).toContain('xhigh')
    expect(ticked()).toEqual(['xhigh'])
  })

  it('takes the effort from the reply, not from the rung that was clicked', async () => {
    await effort.load()
    answer = async () => ({ effort: 'medium', default: 'low', options: WORDS })
    effort.pick('high')
    await Promise.resolve()
    expect(asked).toEqual([undefined, 'high'])
    expect(ticked()).toEqual(['medium'])
  })

  it('clears the pick through the default rung', async () => {
    answer = async (id) => ({ effort: id === undefined ? 'high' : id, default: 'low', options: WORDS })
    await effort.load()
    expect(ticked()).toEqual(['high'])
    effort.pick('')
    await Promise.resolve()
    expect(asked).toEqual([undefined, null])
    expect(ticked()).toEqual([''])
  })

  it('keeps the effort it had when the switch fails, and says so', async () => {
    await effort.load()
    answer = async () => { throw new Error('refused') }
    effort.pick('high')
    await Promise.resolve()
    await Promise.resolve()
    expect(ticked()).toEqual([''])
    expect(toastWriter.items).toEqual(['gui.effort.failed {"name":"gui.effort.high"}'])
  })

  it('asks for nothing when the rung already in force is picked, or before a reply', () => {
    effort.pick('high')
    expect(asked).toEqual([])
  })

  it('asks for nothing when the rung already in force is picked', async () => {
    await effort.load()
    effort.pick('')
    expect(asked).toEqual([undefined])
  })
})
