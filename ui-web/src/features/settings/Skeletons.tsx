/* What a settings section looks like before its values are in.
 *
 * Every section used to wait behind one dashed box with "Loading" in it. That
 * box has a size of its own -- a line of display text and 34px of padding --
 * so the panel was a 90px strip that became whatever the section is the moment
 * the snapshot landed, and a section is rarely one line: the usage page is four
 * cards and a chart, the catalogue is a full-height two-column grid. The two
 * waits the pages own drew even less than that -- the usage totals and the
 * archive list each replaced a card's worth of rows with a single grey word
 * inside it, and the card shrank to fit the word.
 *
 * So a section draws its own anatomy instead, in the section's own card, row
 * and list classes: the skeleton IS the page with its words replaced by bars,
 * which is what makes the swap a fill-in rather than a resize. The bar is the
 * one shape added here.
 *
 * Counts and widths are fixed, not sampled: a skeleton that redraws itself at
 * a new length on every render is a second animation competing with the
 * shimmer, and a case reading the same wait twice would read two shapes.
 *
 * One `Wait` per wait, never nested: the box is what announces the wait to a
 * screen reader, and the bars inside it carry no text of their own.
 */
import { t } from '../../i18n/t'
import GROUPS from './toolGroups.json'

import type { SectionId } from './store'
import type { JSX, ReactNode } from 'react'

/* Widths that read as a list of names rather than a stack of identical bars,
   cycled by row index. */
const W = ['62%', '44%', '73%', '51%', '67%', '38%', '58%', '48%']
const width = (i: number): string => W[i % W.length]!

/** One bar, where a word will be. */
function Bar({ w, h = 11 }: { w?: string; h?: number }): JSX.Element {
  return <span className="settings-wbar" style={{ width: w, height: `${h}px` }} />
}

/* Everything a reader is waiting for is announced once, by the box around it:
   without this a screen reader is told nothing at all between the click and
   the values, where the line of text this replaces at least said "loading". */
function Wait({ cls, children }: { cls?: string; children: ReactNode }): JSX.Element {
  return (
    <div className={cls ? `settings-wait ${cls}` : 'settings-wait'} role="status" aria-busy="true" aria-label={t('gui.settings.loading')}>
      {children}
    </div>
  )
}

/** A label/control row, the shape Fields.tsx's `Row` renders. */
function WRow({ i, ctl = 120 }: { i: number; ctl?: number }): JSX.Element {
  return (
    <div className="settings-row">
      <div className="settings-k"><Bar w={width(i)} /></div>
      <div className="settings-ctl"><Bar w={`${ctl}px`} h={22} /></div>
    </div>
  )
}

/** A card of `n` such rows, with a heading bar when the card carries a title. */
function WCard({ n, titled, ctl, from = 0 }: { n: number; titled?: boolean; ctl?: number; from?: number }): JSX.Element {
  return (
    <div className="settings-card">
      {titled && <div className="settings-ch"><Bar w="140px" h={13} /></div>}
      <div className="settings-rows">
        {Array.from({ length: n }, (_, i) => <WRow key={i} ctl={ctl} i={from + i} />)}
      </div>
    </div>
  )
}

/** The expandable name/switch rows the tools and plugins pages list. */
function WXrows({ n, from = 0 }: { n: number; from?: number }): JSX.Element {
  return (
    <>
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="settings-xrow">
          <span className="settings-xname"><Bar w={width(from + i)} /></span>
          <div className="settings-ctl"><Bar w="38px" h={22} /></div>
        </div>
      ))}
    </>
  )
}

/** One of the usage page's tables: a heading and `rows` rows of figures. */
function WTable({ rows }: { rows: number }): JSX.Element {
  return (
    <div className="settings-card">
      <div className="settings-ch"><Bar w="120px" h={13} /></div>
      <div className="settings-wtab">
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="settings-wtr">
            <Bar w={width(i)} />
            <Bar w="46px" h={9} />
            <Bar w="46px" h={9} />
          </div>
        ))}
      </div>
    </div>
  )
}

/* ---- the two waits the pages own --------------------------------------- */

/** The usage answer: the totals, the per-day chart and the two tables. */
function UsageBars(): JSX.Element {
  return (
    <>
      <div className="settings-card">
        <div className="settings-tiles4">
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="settings-tl settings-wstack">
              <Bar w="58%" h={19} />
              <Bar w="40%" h={9} />
            </div>
          ))}
        </div>
      </div>
      <div className="settings-card">
        <div className="settings-ch"><Bar w="90px" h={13} /></div>
        <div className="settings-bars">
          {/* A silhouette rather than a flat row: a chart whose skeleton is one
              height everywhere reads as a progress bar, not as a chart. */}
          {Array.from({ length: 30 }, (_, i) => (
            <span key={i} className="settings-wbar" style={{ height: `${18 + ((i * 37) % 58)}px` }} />
          ))}
        </div>
        <div className="settings-bax"><Bar w="66px" h={9} /><Bar w="66px" h={9} /></div>
      </div>
      <WTable rows={5} />
      <WTable rows={3} />
    </>
  )
}

/** The archived sessions, drawn as the rows they become. */
function ArchiveBars(): JSX.Element {
  return (
    <>
      {Array.from({ length: 4 }, (_, i) => (
        <div key={i} className="settings-row">
          <div className="settings-k"><Bar w={width(i)} /></div>
          <div className="settings-ctl settings-wpair"><Bar w="64px" h={22} /><Bar w="52px" h={22} /></div>
        </div>
      ))}
    </>
  )
}

/** What the usage page draws under its range picker while the counter answers. */
export function UsageWait(): JSX.Element {
  return <Wait><UsageBars /></Wait>
}

/** What the archive card holds while the list is being read. */
export function ArchiveWait(): JSX.Element {
  return <Wait><ArchiveBars /></Wait>
}

/* ---- the three a drawer or a popover fetches ---------------------------- */

/** A skill's SKILL.md: the identity block and the prose under it. */
export function SkillDetailWait(): JSX.Element {
  return (
    <Wait cls="settings-card">
      <div className="settings-mdhead settings-wstack">
        <Bar w="180px" h={16} />
        <Bar w="72%" />
        <Bar w="150px" h={20} />
      </div>
      <div className="settings-md settings-wstack">
        {Array.from({ length: 7 }, (_, i) => <Bar key={i} w={width(i + 2)} />)}
      </div>
    </Wait>
  )
}

/** A plugin's credential fields, which the catalogue answers per server. */
export function KeyFieldsWait(): JSX.Element {
  return (
    <Wait>
      <div className="settings-row settings-stack">
        <div className="settings-k"><Bar w="120px" /></div>
        <div className="settings-ctl settings-wpair"><Bar w="220px" h={30} /><Bar w="64px" h={30} /></div>
      </div>
    </Wait>
  )
}

/** The vendor's model list, inside the add-model popover. */
export function ModelListWait(): JSX.Element {
  return (
    <Wait>
      {Array.from({ length: 8 }, (_, i) => (
        <div key={i} className="settings-apm">
          <Bar h={15} w="15px" />
          <Bar w={width(i)} />
        </div>
      ))}
    </Wait>
  )
}

/* ---- the section itself, while the snapshot is still coming ------------- */

/* The catalogue is a full-height grid rather than a column of cards, so its
   skeleton is that grid: a column of cards in its place would collapse the
   pane the moment the vendors landed. */
function ProviderWait(): JSX.Element {
  return (
    <Wait cls="settings-tp">
      <div className="settings-tp-side">
        <div className="settings-tp-find"><Bar h={30} w="100%" /></div>
        <div className="settings-tp-list settings-wstack">
          {Array.from({ length: 11 }, (_, i) => <Bar key={i} h={18} w={width(i)} />)}
        </div>
      </div>
      <div className="settings-tp-main settings-wstack">
        <Bar h={20} w="170px" />
        <Bar w="58%" />
        <Bar h={64} w="100%" />
        <Bar h={120} w="100%" />
      </div>
    </Wait>
  )
}

function SkillsWait(): JSX.Element {
  return (
    <Wait>
      <Bar h={32} w="100%" />
      {[6, 4].map((n, card) => (
        <div key={card} className="settings-card">
          <div className="settings-ch"><Bar h={13} w="160px" /></div>
          {Array.from({ length: n }, (_, i) => (
            <div key={i} className="settings-skrow">
              <span className="settings-open"><Bar w="80%" /><Bar w={width(i + card)} /></span>
              <Bar h={22} w="38px" />
            </div>
          ))}
        </div>
      ))}
    </Wait>
  )
}

/* The first groups at the sizes the page will draw them, so what arrives below
   the fold arrives under a scroll position that already fits it. */
const TOOL_SIZES = Object.values(GROUPS as Record<string, string[]>).slice(0, 4).map((ids) => ids.length)

function ToolsWait(): JSX.Element {
  let seen = 0
  return (
    <Wait>
      <div className="settings-crumb"><Bar h={13} w="150px" /></div>
      {TOOL_SIZES.map((n, i) => {
        const from = seen
        seen += n
        return (
          <div key={i} className="settings-card">
            <div className="settings-ch"><Bar h={13} w="110px" /></div>
            <WXrows from={from} n={n} />
          </div>
        )
      })}
    </Wait>
  )
}

function PluginsWait(): JSX.Element {
  return (
    <Wait>
      <div className="settings-crumb"><Bar h={13} w="120px" /></div>
      <div className="settings-card"><WXrows n={6} /></div>
    </Wait>
  )
}

/* Eleven roles in one card -- providers/Roles.tsx's ROLES, whose length is the
   number here. */
const ROLE_ROWS = 11

const SHAPE: Partial<Record<SectionId, () => JSX.Element>> = {
  about: () => <Wait><WCard ctl={160} n={4} /></Wait>,
  archive: () => (
    <Wait>
      <div className="settings-card">
        <div className="settings-ch"><Bar h={13} w="130px" /></div>
        <div className="settings-rows"><ArchiveBars /></div>
      </div>
      <WCard ctl={38} from={5} n={1} />
    </Wait>
  ),
  general: () => <Wait><WCard ctl={190} n={3} /></Wait>,
  model: () => <Wait><WCard ctl={210} n={ROLE_ROWS} titled /></Wait>,
  plugins: PluginsWait,
  provider: ProviderWait,
  skills: SkillsWait,
  tools: ToolsWait,
  /* The range picker is the reader's own state rather than the gateway's, so
     its card is a shape here and a live control the moment the page draws. */
  usage: () => <Wait><WCard ctl={300} n={1} /><UsageBars /></Wait>,
}

/* The onboarding wizard renders two of the dialog's own panes as its steps
   (SetupBodies.tsx), off the same snapshot -- so they wait on the same load
   and neither is a section the nav can reach. */
export function SetupWait({ step }: { step: 'model' | 'web' }): JSX.Element {
  return step === 'model'
    ? <Wait><WCard ctl={150} n={2} titled /><WCard ctl={210} from={3} n={ROLE_ROWS} titled /></Wait>
    : <Wait><WCard ctl={190} n={2} titled /><WCard ctl={190} from={2} n={2} titled /></Wait>
}

/** The shape of the section a reader is waiting for. */
export function SectionWait({ id }: { id: SectionId }): JSX.Element {
  const Shape = SHAPE[id]
  return Shape ? <Shape /> : <Wait><WCard n={3} /></Wait>
}
