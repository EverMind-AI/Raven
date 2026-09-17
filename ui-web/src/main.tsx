import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'

import { App } from './App'
import * as composer from './features/composer/mount'
import * as sheets from './state/sheetRack'
import * as dagSheet from './features/dag/mount'
import { ConnApp } from './features/connections/ConnPage'
import { installLinkTrap } from './features/browser/store'
import { CronApp } from './features/cron/CronPage'
import { ModelPickerApp } from './features/model/ModelPicker'
import { OnboardApp } from './features/onboard/OnboardPage'
import { MemoryApp } from './features/memory/MemoryPage'
import { KnowledgeApp } from './features/knowledge/KnowledgePage'
import { PlaybooksApp } from './features/playbooks/PlaybooksPage'
import { PlugApp } from './features/plugins/PluginsPage'
import { RailApp } from './features/rail/RailPage'
import * as rail from './features/rail/store'
import { Skeleton as SkillsSkeleton, SkillsApp } from './features/skills/SkillsPage'
import { WsApp } from './features/workspace/WorkspacePage'
import { DeskApp } from './features/workspace/DeskPage'
import * as desk from './features/workspace/deskStore'
import * as workspace from './features/workspace/store'
import * as subagents from './features/subagents/store'
import { XaApp } from './features/xa/XaPage'
import { SettingsApp } from './features/settings/SettingsPage'
import * as find from './state/find'
import * as panes from './chrome/behaviour/panes'
import * as scrollbars from './chrome/behaviour/scrollbars'
import * as session from './lib/session'
import { plugHost, skillsHost, skillsSkeletonHost } from './features/hosts'
import * as pluginsTab from './features/plugins/tab'
import * as settingsChrome from './features/settings/chrome'
import * as skillsTab from './features/skills/tab'
import { boot } from './app/boot'
import { installComposerPalette } from './app/install'
import * as langEffects from './state/lang/effects'
import { dropNoJs, markStart } from './app/splash'
import * as ws from './state/ws'
import { setWsPanel } from './state/wsPanel'
import { setGateway } from './rpc/gateway'
import { installGlobalListeners } from './state/globalListeners'
import * as portals from './state/portals'
import { chooseTransport } from './rpc/chooseTransport'

/* The page: the one root that renders it, the roots the islands mount, the
 * chrome that wires itself over what they render, and the one transport.
 *
 * Nothing is published on window and nothing is injected from outside any
 * more: every line below is this bundle calling a module of its own, in the
 * order the concatenated page script ran them in. That order is the whole
 * reason this file is a straight line rather than a set of install()s --
 * several of these steps read what an earlier one wrote.
 */

/* The page's own root, committed before anything reaches into what it renders.
 *
 * Detached on purpose: createRoot(container).render() clears that container's
 * existing children on its first commit, so a root at document.body would
 * delete #splash, #noJs and every static region src/page.html still carries.
 * What the root renders is portals into those containers (App.tsx).
 *
 * flushSync, and first, because everything below reads the result: the settings
 * island looks #snavList up while it renders, the confirm store focuses #cfNo
 * as it asks, and the chrome's Escape chain clicks that button. A plain
 * render() would commit in a later task, after all of them.
 */
const appRoot = createRoot(document.createElement('div'))
flushSync(() => appRoot.render(<App />))

/* Every listener the page holds on the document or the window, in the order
   they are declared in (state/globalListeners.ts). Here rather than in each
   module's own install() because the order is a contract and a contract needs
   one place; after the root above, because a handler may reach for what it
   renders, and because the root's own listener is react-dom's to register. */
installGlobalListeners()

/* The panel the workspace, browser and sub-agent views are drawn inside, handed
   to the islands that ask it something rather than imported by them
   (state/wsPanel.ts). */
setWsPanel(ws)

/* The desk, handed to the two island stores that open something in it. Handed
   rather than reached for: features/workspace/deskStore imports both of them
   back and subscribes to one as it evaluates, so an import the other way would
   run that subscription against a half-built module -- which is also why the
   panel those stores ask about is handed to them (state/wsPanel.ts). Here,
   before the first frame, because either store may be asked to open a pane
   from the moment the page is on screen. */
subagents.setAgentPane({ openAgent: desk.openDeskAgent, openAgentRecord: desk.openDeskAgentRecord })
workspace.setDeskOpener(desk.openDeskFile)


session.onChange(() => {
  sheets.sync()
  dagSheet.sync()
  /* The desk palette is open or shut per conversation, and this is the event
     that says which one is on screen -- see deskStore.sync. */
  desk.sync()
  rail.draw()
})
find.onChange(rail.draw)

/* Not a listener: the subscription to the frames the agent pushes, for a page
   it opens before this view is ever shown. */
installLinkTrap()

/* The dock's own listeners -- the field, the send button, the file picker, the
   drop target, the pill. Registered here rather than on the first paint
   because the markup is already in the document; the handlers read the
   translator and the composer source lazily, which is what makes that safe
   this early. */
composer.install()
/* The chrome that wires itself over the static markup, which is already parsed
   by the time this bundle runs: the page script below is the LAST thing in the
   body. Installing here rather than from the shell keeps each module's wiring
   next to the behaviour it belongs to. What each of these installs is
   element-level -- the grips, the search row -- except the scrollbars, which
   raise the layer their thumbs are parked in. */
scrollbars.install()
panes.install()
find.install()

/* The model picker renders nothing until asked. One root at the body rather
   than a host inside a page: the popover is anchored to whatever button opened
   it -- the composer chip or a settings row -- and belongs to neither. The
   wrapper is inert for layout; .mpick is position: fixed.

   The layer comes from state/portals.ts, which declares where each of the four
   standing layers belongs: this one shares its `--z` step with the two composer
   popovers, and being appended before them is the whole of what puts it under
   them. */
createRoot(portals.host('picker')).render(<ModelPickerApp />)

const onboardHost = document.getElementById('onb')
if (onboardHost) createRoot(onboardHost).render(<OnboardApp />)

const host = document.getElementById('cronBody')
if (host) createRoot(host).render(<CronApp />)

const memHost = document.getElementById('memBody')
if (memHost) createRoot(memHost).render(<MemoryApp />)
const kbHost = document.getElementById('kbBody')
if (kbHost) createRoot(kbHost).render(<KnowledgeApp />)
const pbHost = document.getElementById('pbBody')
if (pbHost) createRoot(pbHost).render(<PlaybooksApp />)
const connHost = document.getElementById('connBody')
if (connHost) createRoot(connHost).render(<ConnApp />)
const deskRoot = createRoot(portals.host('desk'))
queueMicrotask(() => deskRoot.render(<DeskApp />))
createRoot(skillsHost).render(<SkillsApp />)
createRoot(skillsSkeletonHost).render(<>{Array.from({ length: 6 }, (_, i) => <SkillsSkeleton key={i} />)}</>)

/* The workspace island mounts lazily: #wsBody is shared ground -- the agents
   and browser tabs draw into it through their own island roots, so the
   workspace root exists only while a workspace view is up (see
   workspace/store.draw). */
workspace.setRenderer(() => createElement(WsApp))

const listHost = document.getElementById('list')
if (listHost) createRoot(listHost).render(<RailApp />)
createRoot(plugHost).render(<PlugApp />)
const xaHost = document.getElementById('xaBody')
if (xaHost) createRoot(xaHost).render(<XaApp />)
const setHost = document.getElementById('spanels')
if (setHost) createRoot(setHost).render(<SettingsApp />)

/* The one data entry point, installed before anything can ask for it. Every
   mode has one now: a page served by a raven gets the socket, and a page opened
   from disk or with ?stub=1 gets the offline fixture library, which answers the
   same contract (rpc/chooseTransport.ts). */
setGateway(chooseTransport())

/* What the concatenated page script did while it ran, in the order it ran it.
   It was a third inline <script> after this bundle, then a list of install()
   calls, and now it is this: each line belongs to the module that owns the
   thing it does, and the order between them is the order that script had.

   Before the boot below and for the same reason the script was last then: each
   of these reaches for the chrome this file has just wired and for the island
   roots mounted above, and the boot reaches for them. */

/* If this bundle runs at all, the no-script marker goes. */
dropNoJs()
/* The session pointer starts on the offline fixture's first conversation, and
   the boot's own claim clears it again a few lines below: the two writes
   together are what keeps a reload's "come back here" note off fixture noise
   (app/boot.ts's claimFirstFrame, lib/resume.ts). */
session.setCurrent('a')
/* The half of the composer's source no transport answers, before the settings
   seam adds its own member to the same object. */
installComposerPalette()
/* What a match does is the panel's (state/ws.ts); the moment the split point is
   watched from is here, after every listener the page registers itself. */
ws.watchNarrow()
/* The two tabs of the capabilities page, which register their renderers into
   state/caps.ts in this order -- the plugin tab's steps are the outer half of a
   draw and the shared hero is its last step. */
skillsTab.install()
pluginsTab.install()
/* The whole-page redraw a language pick asks for, then the settings transport
   and the model chip. */
langEffects.install()
settingsChrome.install()
/* The splash is up; this is the clock the floor on its display time measures
   from (app/splash.ts). */
markStart()

/* The page's own boot: the seam, the pushes, the actions, then everything a
   first frame needs from the gateway (app/boot.ts). */
boot()
