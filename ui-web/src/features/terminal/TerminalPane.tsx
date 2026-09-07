/** Interactive xterm.js pane backed by the hosted terminal RPC surface. */

import '@xterm/xterm/css/xterm.css'

import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import { useEffect, useRef } from 'react'

import * as store from './store'

import type { TerminalRow, TerminalSource } from './types'
import type { JSX } from 'react'

const ACK_BYTES = 64 * 1024
const FINAL_ACK_DELAY_MS = 100

export function TerminalPane({ terminal, active }: { terminal: TerminalRow; active: boolean }): JSX.Element {
  const hostRef = useRef<HTMLDivElement>(null)
  const runtimeRef = useRef<{ fit: () => void } | null>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const source = store.source()
    const styles = getComputedStyle(document.documentElement)
    const xterm = new Terminal({
      cursorBlink: true,
      fontFamily: styles.getPropertyValue('--mono').trim(),
      fontSize: 13,
      theme: {
        background: styles.getPropertyValue('--stage-bg').trim(),
        foreground: styles.getPropertyValue('--text').trim(),
        cursor: styles.getPropertyValue('--amber').trim(),
        selectionBackground: styles.getPropertyValue('--line').trim(),
      },
    })
    const fitAddon = new FitAddon()
    xterm.loadAddon(fitAddon)
    xterm.open(host)

    const fit = (): void => {
      if (host.hidden || host.offsetParent === null) return
      fitAddon.fit()
      if (xterm.cols > 0 && xterm.rows > 0) {
        void source.resize({ handle: terminal.handle, cols: xterm.cols, rows: xterm.rows })
      }
    }
    runtimeRef.current = { fit }

    let lastAck = 0
    let pendingAck = 0
    let ackTimer: ReturnType<typeof setTimeout> | null = null
    const acknowledge = (): Promise<unknown> => {
      if (ackTimer) {
        clearTimeout(ackTimer)
        ackTimer = null
      }
      if (pendingAck <= lastAck) return Promise.resolve()
      lastAck = pendingAck
      return source.subscribe({ handle: terminal.handle, ack: lastAck })
    }
    const consumed = (seq: number): void => {
      pendingAck = Math.max(pendingAck, seq)
      if (pendingAck - lastAck >= ACK_BYTES) {
        void acknowledge()
        return
      }
      if (ackTimer) clearTimeout(ackTimer)
      ackTimer = setTimeout(() => void acknowledge(), FINAL_ACK_DELAY_MS)
    }

    const input = xterm.onData((data) => void source.input({ handle: terminal.handle, data }))
    const unlisten = store.onOutput(terminal.handle, (frame) =>
      xterm.write(frame.data, () => {
        if (frame.replay) {
          lastAck = Math.max(lastAck, frame.seq)
          pendingAck = Math.max(pendingAck, frame.seq)
        } else {
          consumed(frame.seq)
        }
      }),
    )
    void source.subscribe({ handle: terminal.handle }).then((reply) => {
      const baseline = reply.subscription.seq
      if (typeof baseline === 'number') lastAck = Math.max(lastAck, baseline)
    })

    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit)
    observer?.observe(host)
    fit()

    return () => {
      runtimeRef.current = null
      observer?.disconnect()
      unlisten()
      input.dispose()
      xterm.dispose()
      void acknowledge().finally(() => void source.subscribe({ handle: terminal.handle, enabled: false }))
    }
  }, [terminal.handle])

  useEffect(() => {
    if (active) runtimeRef.current?.fit()
  }, [active])

  return <div className="terminal-xterm" ref={hostRef} />
}
