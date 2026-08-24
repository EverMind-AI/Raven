/** Draggable floating navigation palette for workspace content. */

import { Fragment, useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { AgentList } from '../subagents/SubagentsPage'
import * as agents from '../subagents/store'
import { t } from '../../shell/bridge'
import { DeskIcon } from './DeskIcon'
import {
  anchoredGeometry,
  clampGeometry,
  defaultGeometry,
  DESK_GEOMETRY_KEY,
  magnetGeometry,
} from './deskGeometry'
import * as desk from './deskStore'
import * as workspace from './store'

import type { DeskGeometry, DeskTab } from './deskTypes'
import type { FtEntry } from './types'
import type { CSSProperties, JSX, PointerEvent as ReactPointerEvent } from 'react'

function DeskTabs({ value, onChange }: { value: DeskTab; onChange: (tab: DeskTab) => void }): JSX.Element {
  return (
    <div className="desk-tabs" role="tablist">
      {(['diff', 'file', 'agents'] as DeskTab[]).map((tab) => {
        const label = tab === 'diff' ? 'Diff' : tab === 'file' ? t('gui.ws.files') : t('gui.ws.agents')
        return (
          <button key={tab} role="tab" aria-label={label} aria-selected={value === tab} onClick={() => onChange(tab)}>
            <DeskIcon kind={tab} />
            <span>{label}</span>
          </button>
        )
      })}
    </div>
  )
}

function DiffNav(): JSX.Element {
  const changes = workspace.shared().changes
  if (!changes.length) return <div className="desk-empty">{t('gui.ws.no_changes')}</div>
  return (
    <div className="desk-list">
      {changes.map((change) => (
        <button key={`${change.key}:${change.turn}`} className="desk-row desk-diff-row" onClick={() => desk.openDeskDiff(change)}>
          <i className={`chgc ${change.kind}`}>{t(`gui.ws.chip.${change.kind}`)}</i>
          <span className="desk-name" title={change.key}>{change.dir}<b>{change.name}</b></span>
          <span className="chgs">
            {change.add ? <i className="a">+{change.add}</i> : null}
            {change.del ? <i className="d">−{change.del}</i> : null}
          </span>
        </button>
      ))}
    </div>
  )
}

function DeskTree({ dir = '', depth = 0 }: { dir?: string; depth?: number }): JSX.Element | null {
  const kids = workspace.FT.kids.get(dir)
  if (!Array.isArray(kids)) return null
  return (
    <>
      {(kids as FtEntry[]).map((entry) => {
        const full = workspace.ftJoin(dir, entry.name)
        const open = Boolean(entry.dir) && workspace.FT.open.has(full)
        return (
          <Fragment key={full}>
            <button
              className="desk-row file-row"
              style={{ paddingLeft: `${10 + depth * 14}px` }}
              onClick={() => {
                if (entry.dir) {
                  if (open) workspace.FT.open.delete(full)
                  else {
                    workspace.FT.open.add(full)
                    workspace.ftLoad(full)
                  }
                  workspace.redraw()
                  desk.notifyDesk()
                } else desk.openDeskFile(workspace.ftAbs(full))
              }}
            >
              <span className="tree-chev">{entry.dir ? (open ? '⌄' : '›') : ''}</span>
              <span aria-hidden="true">{entry.dir ? '□' : '▯'}</span>
              <span className="desk-name" title={entry.name}>{entry.name}</span>
            </button>
            {entry.dir && open ? <DeskTree dir={full} depth={depth + 1} /> : null}
          </Fragment>
        )
      })}
    </>
  )
}

function FileNav(): JSX.Element {
  useEffect(() => {
    workspace.ftLoad('')
    workspace.ftLoadVisible()
  }, [])
  return (
    <div className="desk-list">
      <label className="desk-search">
        <span aria-hidden="true">⌕</span>
        <input
          type="search"
          value={workspace.FT.q}
          placeholder={t('gui.ws.search')}
          onChange={(event) => workspace.ftQuery(event.currentTarget.value)}
        />
      </label>
      {workspace.FT.q
        ? workspace.ftMatches().slice(0, 80).map(({ e, full }) => (
          <button
            key={full}
            className="desk-row file-row"
            onClick={() => {
              if (e.dir) workspace.ftReveal(full, true)
              else {
                workspace.ftOpenTo(full)
                desk.openDeskFile(workspace.ftAbs(full))
              }
            }}
          >
            <span className="tree-chev" />
            <span aria-hidden="true">{e.dir ? '□' : '▯'}</span>
            <span className="desk-name" title={full}>{e.name}</span>
          </button>
        ))
        : <DeskTree />}
    </div>
  )
}

function AgentsNav(): JSX.Element {
  const state = useSyncExternalStore(agents.subscribe, agents.getState)
  useEffect(() => {
    agents.refreshInstances()
    agents.refreshRoster()
  }, [])
  return <AgentList s={state} onOpen={desk.openDeskAgent} compact />
}

function storedGeometry(): DeskGeometry {
  try {
    const saved = JSON.parse(localStorage.getItem(DESK_GEOMETRY_KEY) || '') as Partial<DeskGeometry>
    if ([saved.x, saved.y, saved.w, saved.h].every(Number.isFinite)) {
      return clampGeometry({
        x: saved.x!, y: saved.y!, w: saved.w!, h: saved.h!, detached: saved.detached === true,
      })
    }
  } catch {}
  return defaultGeometry()
}

export function DeskPalette(): JSX.Element | null {
  const state = useSyncExternalStore(desk.subscribe, desk.getState)
  const pointerCleanup = useRef<(() => void) | null>(null)
  const [geom, setGeom] = useState(storedGeometry)
  useEffect(() => {
    try { localStorage.setItem(DESK_GEOMETRY_KEY, JSON.stringify(geom)) } catch {}
  }, [geom])
  useEffect(() => {
    const resize = (): void => setGeom((value) => clampGeometry(value))
    window.addEventListener('resize', resize)
    return () => {
      window.removeEventListener('resize', resize)
      pointerCleanup.current?.()
    }
  }, [])
  const drag = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if ((event.target as HTMLElement).closest('button')) return
    event.preventDefault()
    pointerCleanup.current?.()
    const palette = event.currentTarget.closest('.desk-palette')
    const rect = palette?.getBoundingClientRect()
    const start = {
      pointerX: event.clientX,
      pointerY: event.clientY,
      x: rect?.left ?? geom.x,
      y: rect?.top ?? geom.y,
    }
    const target = event.currentTarget
    const pointerId = event.pointerId
    const controller = new AbortController()
    target.dataset.dragging = 'true'
    target.setPointerCapture(pointerId)
    const move = (e: globalThis.PointerEvent): void => {
      if (e.pointerId !== pointerId) return
      setGeom((now) => magnetGeometry({
        ...now,
        x: start.x + e.clientX - start.pointerX,
        y: start.y + e.clientY - start.pointerY,
      }))
    }
    const finish = (e?: globalThis.PointerEvent): void => {
      if (e && e.pointerId !== pointerId) return
      target.dataset.dragging = 'false'
      if (target.hasPointerCapture(pointerId)) target.releasePointerCapture(pointerId)
      controller.abort()
      if (pointerCleanup.current === cleanup) pointerCleanup.current = null
    }
    const cleanup = (): void => finish()
    pointerCleanup.current = cleanup
    window.addEventListener('pointermove', move, { signal: controller.signal })
    window.addEventListener('pointerup', finish, { signal: controller.signal })
    window.addEventListener('pointercancel', finish, { signal: controller.signal })
    target.addEventListener('lostpointercapture', finish, { signal: controller.signal })
  }
  const resize = (event: ReactPointerEvent<HTMLDivElement>): void => {
    event.preventDefault()
    event.stopPropagation()
    pointerCleanup.current?.()
    const start = { pointerX: event.clientX, pointerY: event.clientY, ...geom }
    const target = event.currentTarget
    const pointerId = event.pointerId
    const controller = new AbortController()
    target.setPointerCapture(pointerId)
    const move = (e: globalThis.PointerEvent): void => {
      if (e.pointerId !== pointerId) return
      setGeom((now) => clampGeometry({
        ...now,
        w: start.w + e.clientX - start.pointerX,
        h: start.h + e.clientY - start.pointerY,
      }))
    }
    const finish = (e?: globalThis.PointerEvent): void => {
      if (e && e.pointerId !== pointerId) return
      if (target.hasPointerCapture(pointerId)) target.releasePointerCapture(pointerId)
      controller.abort()
      if (pointerCleanup.current === cleanup) pointerCleanup.current = null
    }
    const cleanup = (): void => finish()
    pointerCleanup.current = cleanup
    window.addEventListener('pointermove', move, { signal: controller.signal })
    window.addEventListener('pointerup', finish, { signal: controller.signal })
    window.addEventListener('pointercancel', finish, { signal: controller.signal })
    target.addEventListener('lostpointercapture', finish, { signal: controller.signal })
  }
  if (!state.paletteOpen) return null
  return (
    <div
      className="desk-palette"
      data-anchored={!geom.detached}
      style={{
        ...(geom.detached ? { left: geom.x, top: geom.y } : {}),
        width: geom.w,
        height: geom.h,
      } as CSSProperties}
    >
      <div className="desk-drag" onPointerDown={drag}>
        <DeskTabs value={state.tab} onChange={(tab) => desk.update({ tab })} />
      </div>
      <div className="desk-body">
        {state.tab === 'diff' ? <DiffNav /> : state.tab === 'file' ? <FileNav /> : <AgentsNav />}
      </div>
      <div className="desk-resize" onPointerDown={resize} />
    </div>
  )
}
