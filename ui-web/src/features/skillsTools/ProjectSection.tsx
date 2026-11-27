/* Project-scoped skills controls for the Skills tab: context bar showing the
 * selected repository directory, anchored recent-folders / local-folder picker
 * (native host dialog with in-page `fs.dirs` browser fallback), and the empty
 * guidance card when no project directory is chosen yet.
 */
import {
  ArrowDown01Icon,
  ArrowUp01Icon,
  Folder01Icon,
  HardDriveIcon,
  Home01Icon,
  Tick01Icon,
} from '@hugeicons/core-free-icons'
import { useEffect, useRef } from 'react'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { IconTile, Pill, Switch } from './Parts'
import * as store from './store'

import type { DirListing } from './types'
import type { JSX } from 'react'

function showEnd(el: HTMLInputElement | null): void {
  if (el) el.scrollLeft = el.scrollWidth
}

function FolderBrowser({ at, loading, err, asked }: {
  at: DirListing
  loading: boolean
  err: string
  asked: number
}): JSX.Element {
  const roots = at.roots || []
  const field = useRef<HTMLInputElement>(null)
  return (
    <div className="skillsTools-proj-browser">
      <div className="skillsTools-proj-bnav">
        <button
          type="button"
          className="skillsTools-proj-navbtn"
          title={t('gui.skillsTools.skills.dir_home')}
          aria-label={t('gui.skillsTools.skills.dir_home')}
          onClick={() => void store.browseProjectDir(at.home)}
        >
          <Icon icon={Home01Icon} size={14} />
        </button>
        <button
          type="button"
          className="skillsTools-proj-navbtn"
          title={t('gui.skillsTools.skills.dir_up')}
          aria-label={t('gui.skillsTools.skills.dir_up')}
          disabled={!at.parent}
          onClick={() => { if (at.parent) void store.browseProjectDir(at.parent) }}
        >
          <Icon icon={ArrowUp01Icon} size={14} />
        </button>
        {roots.length ? (
          <button
            type="button"
            className="skillsTools-proj-navbtn"
            title={t('gui.skillsTools.skills.dir_drives')}
            aria-label={t('gui.skillsTools.skills.dir_drives')}
            onClick={() => store.pickerDrives()}
          >
            <Icon icon={HardDriveIcon} size={14} />
          </button>
        ) : null}
        <input
          key={`${at.path}#${asked}`}
          ref={(el) => {
            field.current = el
            showEnd(el)
          }}
          className="skillsTools-proj-binput mono"
          type="text"
          defaultValue={at.path}
          title={at.path}
          aria-label={t('gui.skillsTools.skills.dir_typed')}
          placeholder={t('gui.skillsTools.skills.dir_typed')}
          spellCheck={false}
          onKeyDown={(e) => {
            if (e.key !== 'Enter') return
            e.preventDefault()
            void store.goProjectDir(e.currentTarget.value)
          }}
        />
      </div>
      <div className="skillsTools-proj-blist" role="list">
        {at.entries.map((entry) => (
          <button
            key={entry.path}
            type="button"
            role="listitem"
            className={entry.ok ? 'skillsTools-proj-brow' : 'skillsTools-proj-brow skillsTools-proj-brow-off'}
            title={entry.ok ? entry.path : t('gui.skillsTools.skills.dir_blocked')}
            onClick={() => void store.browseProjectDir(entry.path)}
          >
            <Icon icon={Folder01Icon} size={14} />
            <span className="skillsTools-proj-bname">{entry.name}</span>
          </button>
        ))}
        {!at.entries.length && !loading ? <div className="skillsTools-proj-bnote">{t('gui.skillsTools.skills.dir_empty')}</div> : null}
        {loading ? <div className="skillsTools-proj-bnote">{t('gui.skillsTools.skills.dir_loading')}</div> : null}
        {err ? <div className="skillsTools-proj-berr" role="alert">{err}</div> : null}
      </div>
      <div className="skillsTools-proj-bfoot">
        <button type="button" className="skillsTools-btn skillsTools-btn-ghost" onClick={() => store.pickerBack()}>
          {t('gui.skillsTools.skills.dir_back')}
        </button>
        <button
          type="button"
          className="skillsTools-btn skillsTools-btn-primary"
          disabled={!at.ok || loading}
          title={at.ok ? at.path : t('gui.skillsTools.skills.dir_blocked')}
          onClick={() => void store.useBrowsedProjectDir(field.current?.value)}
        >
          {t('gui.skillsTools.skills.dir_use')}
        </button>
      </div>
    </div>
  )
}

function DrivesList({ roots, err }: {
  roots: readonly { name: string; path: string; ok: boolean }[]
  err: string
}): JSX.Element {
  return (
    <div className="skillsTools-proj-browser">
      <div className="skillsTools-proj-blist" role="list">
        {roots.map((root) => (
          <button
            key={root.path}
            type="button"
            role="listitem"
            className={root.ok ? 'skillsTools-proj-brow' : 'skillsTools-proj-brow skillsTools-proj-brow-off'}
            title={root.ok ? root.path : t('gui.skillsTools.skills.dir_blocked')}
            onClick={() => void store.browseProjectDir(root.path)}
          >
            <Icon icon={HardDriveIcon} size={14} />
            <span className="skillsTools-proj-bname">{root.name}</span>
          </button>
        ))}
        {err ? <div className="skillsTools-proj-berr" role="alert">{err}</div> : null}
      </div>
      <div className="skillsTools-proj-bfoot">
        <button type="button" className="skillsTools-btn skillsTools-btn-ghost" onClick={() => store.pickerBack()}>
          {t('gui.skillsTools.skills.dir_back')}
        </button>
      </div>
    </div>
  )
}

function PickerPopover({ currentDir }: { currentDir: string | null }): JSX.Element | null {
  const s = store.get()
  const popRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!s.pickerOpen) return
    const onPointer = (e: MouseEvent): void => {
      const target = e.target as Node | null
      const root = popRef.current?.closest('.skillsTools-proj-picker') ?? popRef.current
      if (root && target && !root.contains(target)) {
        store.closePicker()
      }
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        store.closePicker()
      }
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey, true)
    }
  }, [s.pickerOpen])

  if (!s.pickerOpen) return null
  const recents = store.recentProjectFolders()

  return (
    <div
      ref={popRef}
      className="skillsTools-proj-pop"
      role="dialog"
      aria-label={t('gui.skillsTools.skills.project_choose')}
    >
      {s.pickerView === 'browse' && s.pickerListing ? (
        <FolderBrowser at={s.pickerListing} loading={s.pickerLoading} err={s.pickerErr} asked={s.pickerAsked} />
      ) : s.pickerView === 'drives' && s.pickerListing ? (
        <DrivesList roots={s.pickerListing.roots || []} err={s.pickerErr} />
      ) : (
        <>
          {recents.length > 0 ? (
            <>
              <div className="skillsTools-proj-pop-kicker">{t('gui.skillsTools.skills.project_recent_title')}</div>
              {recents.map((item) => {
                const active = item.path === currentDir
                return (
                  <button
                    key={item.path}
                    type="button"
                    className={active ? 'skillsTools-proj-prow skillsTools-proj-prow-active' : 'skillsTools-proj-prow'}
                    onClick={() => void store.selectProjectDir(item.path)}
                  >
                    <Icon icon={Folder01Icon} size={14} />
                    <span className="skillsTools-proj-pmeta">
                      <span className="skillsTools-proj-pname-line">
                        <span className="skillsTools-proj-pname">{item.name}</span>
                        {item.current ? <Pill tone="teal">{t('gui.skillsTools.skills.project_current')}</Pill> : null}
                      </span>
                      <span className="skillsTools-proj-ppath mono">{item.path}</span>
                    </span>
                    {active ? <Icon icon={Tick01Icon} size={14} /> : null}
                  </button>
                )
              })}
              <div className="skillsTools-proj-hr" />
            </>
          ) : null}
          <button
            type="button"
            className="skillsTools-proj-prow skillsTools-proj-choose"
            disabled={s.pickerPicking}
            onClick={() => void store.chooseProjectFolder()}
          >
            <Icon icon={Folder01Icon} size={14} />
            <span>{t(store.nativeFolderPick() ? 'gui.skillsTools.skills.project_browse' : 'gui.skillsTools.skills.dir_open_remote')}</span>
          </button>
          {s.pickerErr ? <div className="skillsTools-proj-berr" role="alert">{s.pickerErr}</div> : null}
        </>
      )}
    </div>
  )
}

export function ProjectContextBar(): JSX.Element {
  const s = store.get()
  const dir = s.projectDir || ''
  const name = store.folderBaseName(dir)
  const isCurrent = dir === store.activeSessionWorkdir()
  const enabled = s.projectInfo ? s.projectInfo.enabled : s.snap.projectSkillsOn !== false

  return (
    <div className="skillsTools-proj-bar">
      <div className="skillsTools-proj-left">
        <IconTile tone="teal" size="sm">
          <Icon icon={Folder01Icon} size={15} />
        </IconTile>
        <div className="skillsTools-proj-info">
          <div className="skillsTools-proj-name-row">
            <span className="skillsTools-proj-name">{name}</span>
            <Pill tone="mono">.agents/skills</Pill>
            {isCurrent ? <Pill tone="teal">{t('gui.skillsTools.skills.project_current')}</Pill> : null}
          </div>
          <div className="skillsTools-proj-path mono" title={dir}>{dir}</div>
        </div>
      </div>

      <div className="skillsTools-proj-right">
        <div className="skillsTools-proj-toggle">
          <span>{t('gui.skillsTools.skills.project_switch')}</span>
          <Switch
            on={enabled}
            label={t('gui.skillsTools.skills.project_switch')}
            onChange={(next) => void store.toggleProjectSkillsEnabled(next)}
          />
        </div>
        <div className="skillsTools-proj-picker">
          <button
            type="button"
            className="skillsTools-btn"
            aria-expanded={s.pickerOpen}
            aria-haspopup="dialog"
            onClick={() => store.togglePicker()}
          >
            <Icon icon={Folder01Icon} size={14} />
            <span>{t('gui.skillsTools.skills.project_change')}</span>
            <Icon icon={ArrowDown01Icon} size={13} />
          </button>
          <PickerPopover currentDir={dir} />
        </div>
      </div>
    </div>
  )
}

export function ProjectEmptyCard(): JSX.Element {
  const s = store.get()
  const recents = store.recentProjectFolders()

  return (
    <div className="skillsTools-proj-empty">
      <IconTile tone="teal" size="lg">
        <Icon icon={Folder01Icon} size={20} />
      </IconTile>
      <div className="skillsTools-proj-empty-t">{t('gui.skillsTools.skills.project_empty_title')}</div>
      <div className="skillsTools-proj-empty-s">{t('gui.skillsTools.skills.project_empty_desc')}</div>
      <div className="skillsTools-proj-picker skillsTools-proj-empty-act">
        <button
          type="button"
          className="skillsTools-btn skillsTools-btn-primary"
          disabled={s.pickerPicking}
          onClick={() => void store.chooseProjectFolder()}
        >
          <Icon icon={Folder01Icon} size={14} />
          <span>{t('gui.skillsTools.skills.project_choose')}</span>
        </button>
        <PickerPopover currentDir={null} />
      </div>
      {recents.length > 0 ? (
        <div className="skillsTools-proj-recents">
          <span className="skillsTools-proj-recents-lbl">{t('gui.skillsTools.skills.project_recent')}</span>
          {recents.slice(0, 3).map((item) => (
            <button
              key={item.path}
              type="button"
              className="skillsTools-proj-chip"
              title={item.path}
              onClick={() => void store.selectProjectDir(item.path)}
            >
              <Icon icon={Folder01Icon} size={13} />
              <span>{item.name}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
