/* Data source for the Skills & Tools module page.
 *
 * Delegates to features/settings/source.ts (a sibling domain's public surface)
 * so the config snapshot, web-vendor tables, and extension inventory stay on
 * one canonical read/write path without duplicating gateway calls.
 */
import {
  FETCH_KEYLESS,
  PPT_IMAGE_VENDORS,
  SEARCH_KEYLESS,
  WEB_VENDOR,
  WEB_VENDOR_LABEL,
  WEB_VENDOR_URL,
  borrowedKey,
  legacyKey,
  settingsSource,
  str,
  vendorKey,
  watchMcp,
  webKeyReady,
  webVendor,
} from '../settings/source'
import { workspaceSource } from '../workspace/source'

import type { SkillsToolsSource } from './types'

export {
  FETCH_KEYLESS,
  PPT_IMAGE_VENDORS,
  SEARCH_KEYLESS,
  WEB_VENDOR,
  WEB_VENDOR_LABEL,
  WEB_VENDOR_URL,
  borrowedKey,
  legacyKey,
  str,
  vendorKey,
  watchMcp,
  webKeyReady,
  webVendor,
}

export const skillsToolsSource: SkillsToolsSource = {
  load: () => settingsSource.load(),
  set: (key, value) => settingsSource.set(key, value),
  everosSet: (section, model, provider, protocol) => settingsSource.everosSet(section, model, provider, protocol),
  addModel: (slug, model, tags) => settingsSource.provider('add_model', { slug, model, ...tags }),
  inspectSkill: (name, projectDir) => settingsSource.inspectSkill(name, projectDir),
  openSkillFile: (name, file, projectDir) => settingsSource.openSkillFile(name, file, projectDir),
  projectSkills: (projectDir) => settingsSource.projectSkills!(projectDir),
  listDirs: (path) => workspaceSource.dirs!(path),
  pickDir: () => workspaceSource.pickDir!(),
  uninstallSkill: (name) => settingsSource.uninstallSkill(name),
  blockSkill: (name, blocked) => settingsSource.blockSkill(name, blocked),
  serverDetail: (name) => settingsSource.serverDetail(name),
  toggleServer: (name, on) => settingsSource.toggleServer(name, on),
  retryServer: (name) => settingsSource.retryServer(name),
  revokeServer: (name) => settingsSource.revokeServer(name),
  configureServer: (name, form) => settingsSource.configureServer(name, form),
  authServer: (name) => settingsSource.authServer(name),
  agentPlugins: () => settingsSource.agentPlugins(),
  setPackageMcp: (name, on) => settingsSource.setPackageMcp(name, on),
}
