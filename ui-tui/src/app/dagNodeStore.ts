// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// What each watched DAG node has said so far, as the wire returned it.
//
// A module store for the same reason `dagOpenNodes` is one: the panel has two
// render sites and a work segment re-renders its graph from a different branch
// once the turn settles, so state held in the component would be dropped at the
// moment a run finished.
//
// The wire messages are stored, not the folded rows, because the two readers of
// this want different reductions of them -- a line of characters and a slice of
// messages -- and folding here would make one of them reconstruct what folding
// threw away.

import { atom } from 'nanostores'

import type { TranscriptMessage } from '../rpc/index.js'

import { DAG_TRACE_READ_FAILURE_CAP } from '../config/limits.js'

/**
 * `settled` is the word that a node can never change again: either `dag.node`
 * reported a status other than `pending`/`running`, or its read failed
 * `DAG_TRACE_READ_FAILURE_CAP` times in a row. It is recorded here, not just
 * read off the live poll target, because it has to survive the poll's effect
 * rearming, which a closure-local flag would not.
 *
 * `failures` is the consecutive-read-failure count backing that cap, kept
 * here for the same reason: the poll increments and persists it on every
 * failed read and resets it to zero on every successful one, so a streak
 * already partway to the cap is still there after the effect rearms.
 */
export interface DagNodeTraceEntry {
  failures: number
  messages: readonly TranscriptMessage[]
  settled: boolean
}

export const $dagNodeTraces = atom<ReadonlyMap<string, DagNodeTraceEntry>>(new Map())

// Enough of a trace to tell "it moved" from "the agent is between updates". The
// collector republishes its whole transcript on every frame, so an idle agent
// returns a byte-identical snapshot twice a second and a store that published it
// would re-render every graph in the transcript for nothing. `settled` leads the
// signature so a completion whose last message happens to match the prior read
// byte for byte still counts as a change and publishes. `failures` is left out
// on purpose: nothing renders it (`dagNodeTrace.tsx` only reads `.messages`), so
// a retry streak ticking up underneath an otherwise-unchanged trace would
// publish a re-render for a number no view draws.
const signature = (entry: DagNodeTraceEntry) => {
  const { messages, settled } = entry
  const last = messages.at(-1)
  const toolCallSignature = (last?.tool_calls ?? [])
    .map(call => `${call.name}:${call.arguments}`)
    .join('|')

  return [
    settled ? 1 : 0,
    messages.length,
    last?.role ?? '',
    last?.text?.length ?? 0,
    last?.reasoning_content?.length ?? 0,
    toolCallSignature
  ].join(':')
}

export const getDagNodeTrace = (key: string) => $dagNodeTraces.get().get(key)

const publish = (key: string, entry: DagNodeTraceEntry) => {
  const current = $dagNodeTraces.get()
  const held = current.get(key)
  const changed = !held || signature(held) !== signature(entry)
  // `$dagNodeTraces` types its read side as a `ReadonlyMap` so callers cannot
  // write to it, but this function is the map's own writer. It always writes
  // `entry` through here, even when `changed` is false, so a failure count
  // below the cap is never lost even though nothing about it re-renders.
  const map = current as Map<string, DagNodeTraceEntry>

  map.set(key, entry)

  if (changed) {
    $dagNodeTraces.set(new Map(map))
  }
}

export const setDagNodeTrace = (key: string, messages: readonly TranscriptMessage[], settled: boolean) => {
  publish(key, { failures: 0, messages, settled })
}

/** A successful read with nothing new to show: keeps whatever trace is held, and resets the failure streak. */
export const settleDagNodeTrace = (key: string, settled: boolean) => {
  publish(key, { failures: 0, messages: getDagNodeTrace(key)?.messages ?? [], settled })
}

/** One more consecutive failed read: increments and persists the count, settling the node once it reaches the cap. */
export const markDagNodeReadFailure = (key: string) => {
  const held = getDagNodeTrace(key)
  const failures = (held?.failures ?? 0) + 1

  publish(key, { failures, messages: held?.messages ?? [], settled: failures >= DAG_TRACE_READ_FAILURE_CAP })
}

export const resetDagNodeTraces = () => $dagNodeTraces.set(new Map())
