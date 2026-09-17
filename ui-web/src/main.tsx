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
import * as desk from './features/workspace/DeskPage'
import * as workspace from './features/workspace/store'
import { XaApp } from './features/xa/XaPage'
import { SettingsApp } from './features/settings/SettingsPage'
import * as find from './shell/find'
import * as navfly from './shell/navfly'
import * as panes from './shell/panes'
import * as scrollbars from './shell/scrollbars'
import * as session from './shell/session'
import { plugHost, skillsHost, skillsSkeletonHost } from './islands'
import { installLegacy } from './legacy/index.js'
import { boot } from './state/boot'
import { setGateway } from './state/gateway'
import { installGlobalListeners } from './state/globalListeners'
import * as portals from './state/portals'
import { chooseTransport } from './state/transport'

/* The island bundle: the React roots the page mounts, the chrome that wires
 * itself over the static markup, and the one transport. Nothing is published
 * on window any more -- the legacy layers import what they call, the island
 * bag (islands.ts) is imported too, and the shell half comes in through
 * setShell. The bundle reads the shell lazily (see shell/bridge.ts): at this
 * point no shell has been handed in yet.
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
   because the markup is already in the document; the handlers read the shell
   and DS.composer lazily, which is what makes that safe this early. */
composer.install()
/* The chrome that wires itself over the static markup, which is already parsed
   by the time this bundle runs: the page script below is the LAST thing in the
   body. Installing here rather than from the shell keeps each module's wiring
   next to the behaviour it belongs to. What each of these installs is
   element-level -- the grips, the More button, the search row -- except the
   scrollbars, which raise the layer their thumbs are parked in. */
scrollbars.install()
panes.install()
navfly.install()
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
   same contract (state/transport.ts). */
setGateway(chooseTransport())

/* The legacy chrome, which used to be a third inline <script> after this
   bundle. Before the boot and for the same reason it was last then: its
   install() steps reach for the chrome this file has just wired and for the
   island roots mounted above, and the boot below reaches for them. */
installLegacy()

/* The page's own boot: the seam, the pushes, the actions, then everything a
   first frame needs from the gateway (state/boot.ts). */
boot()
