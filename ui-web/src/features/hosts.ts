/* The three nodes an island renders into that no component renders.
 *
 * Every other island root is given a container src/App.tsx rendered (a page's
 * `.wrap`, `#list`, `#spanels`) or a layer state/portals.ts hands out. These
 * three are detached elements a tab module re-attaches on every draw, so
 * nobody can render them: the capabilities page's two tabs clear their box
 * with innerHTML, which must never tear down nodes React owns.
 *
 * Here rather than in the two domains that own them because the skills tab's
 * skeleton is attached by the plugins tab as well, and a module with no
 * imports of its own is the one shape neither domain has to reach across for.
 *
 * The React roots that render into them are src/main.tsx's.
 */

/* Re-attached under #capsBody on every draw (features/skills/wire.ts). */
export const skillsHost = document.createElement('div')
export const skillsSkeletonHost = document.createElement('div')
skillsSkeletonHost.className = 'hubgrid'

/* The same, for the plugin tab's own draw (features/plugins/wire.ts). */
export const plugHost = document.createElement('div')
