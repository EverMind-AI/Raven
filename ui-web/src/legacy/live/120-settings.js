/* -- settings and the language flip: the seam -------------------------
   The last part of the live layer, and the reason it is still one: what stays
   here is page chrome none of the sources own yet -- the composer's model chip,
   the language flip's whole-page redraw, the boot-time language restore, and
   the settings page's update check. The sources themselves are
   ui-web/src/features/settings/source.ts (the dialog's data, the permission
   chip's refresh) and features/model/source.ts (the provider list, the two
   writes, the tier).

   `redrawAll` is also what tests/test_ui_language_repaint.py reads, by source
   text, to prove every module page repaints on a flip -- so it stays a
   top-level declaration in this layer, with this file name and this manifest
   entry, until stage C removes the test and the chrome in one change. */

import { modelSource, openModelsForMissingProvider, setChipPainter, tierSource } from '../../features/model/source'
import { open as openModelPicker } from '../../features/model/store'
import { bannerSource, settingsSource, setSettingsChrome } from '../../features/settings/source'
import { islands } from '../../islands'
import { hasUpdateFlag } from '../../rpc/capabilities'
import { draw as drawCtx } from '../../shell/ctxchip'
import { draw as drawFoot } from '../../shell/foot'
import { draw as drawPerm, setPermPersister } from '../../shell/perm'
import { current as sessionCurrent } from '../../shell/session'
import { show as toast } from '../../shell/toast'
import { gateway } from '../../state/gateway'
import { staging } from '../../state/session/staging'
import { sources } from '../../state/sources'
import { $, LANG, T, langSet } from '../demo/010-kernel.js'
import { modelCurrent, queueDraw, sess, turn } from '../demo/040-state.js'
import { sessionDraw, sessionOpen } from '../demo/050-rail.js'
import { closeDetail, drawCapsBadge, drawXa } from '../demo/120-capabilities.js'
import { APP_VERSION, appVersionSet, drawSettings } from '../demo/130-settings.js'
import { drawCron, drawKb, drawMem } from '../demo/140-schedule.js'
import { drawConn } from '../demo/145-connections.js'
import { drawMoreFly } from '../demo/150-chrome.js'
import { drawCaps } from '../demo/152-skills.js'
import { drawPb } from '../demo/154-playbooks.js'
import { isDraft } from '../../state/session/registry'
import { askUpgrade, showUpNote } from '../../state/updates'

/* -- language ---------------------------------------------------------
   One key, both front ends: config.language also drives the TUI (which
   polls it) and the language the agent replies in. */
/* Named, and a local rather than a binding the demo layer declares for this
   layer to fill: the pick reaches it through sources.settings.setLang below.
   langSet moves the language and the catalogue together; what is added here is
   the persist and the redraw of everything drawn from JavaScript. */
async function langPickLive(next, { persist } = {}) {
  if (next === LANG) return;
  const prev = LANG;
  langSet(next);
  redrawAll();
  if (!persist) return;
  try {
    await gateway().call('config.set', { key: 'language', value: next });
    langRemember(next);
  } catch (e) {
    /* Put it back rather than leaving the page in a language the gateway does
       not agree with -- the same key drives the TUI and the agent's replies. */
    langSet(prev);
    redrawAll();
    toast(T('gui.op.lang_failed', { detail: (e.data && e.data.detail) || e.message || e }));
  }
}

/* Everything the catalogue reaches that is drawn rather than written in
   the markup. Cheap enough to run wholesale on a language flip. */
function redrawAll() {
  sessionDraw();
  drawFoot();
  drawCapsBadge();
  setModelLabel();
  drawPerm();
  drawWorkdir();
  drawCtx();
  // Every module page, not just the open one: a hidden page keeps its old
  // DOM, so it would still be in the previous language when reopened.
  drawSettings();
  try { drawCaps(); } catch { /* extensions not loaded yet */ }
  try { drawConn(); } catch { /* channels not loaded yet */ }
  try { drawCron(); } catch { /* schedules not loaded yet */ }
  try { drawXa(); } catch { /* agents not loaded yet */ }
  try { drawMem(); } catch { /* memory not loaded yet */ }
  try { drawKb(); } catch { /* knowledge not loaded yet */ }
  try { drawPb(); } catch { /* playbooks not loaded yet */ }
  // The More rows are redrawn on each open, so only a group standing open at
  // the moment of the flip keeps the old names.
  drawMoreFly();
  /* The shared drawer is closed rather than redrawn: it is not on any page, so
     nothing above reaches it, and every one of its five openers would have to
     hand back the subject it was drawn from. Left open it would sit in the old
     language over a page now in the new one, which reads worse than losing the
     place -- and only the settings dialog, which the flip is made from, is
     above it. */
  closeDetail();
  /* The transcript island re-renders its catalogue words (verbs, fold
     headers, footers) in place -- which is also what covers a turn still
     streaming, where the reload below must not run. */
  islands.transcript.redraw();
  queueDraw();
  /* The words baked into stored segments (note labels, phrased previews) come
     back right on a rebuild from disk. Skipped while a turn is streaming:
     re-opening the session mid-turn would cut the stream off. */
  if (!isDraft() && sessionCurrent() && !turn.busy()) sessionOpen(sess(sessionCurrent()));
}

/* The language the gateway last agreed to, kept where a page that cannot
   reach it can still read it. `loadLang` runs only after the connect
   succeeds, so on a failed connect nothing sets the language at all -- and
   the sign-in notice, the one message that explains the empty page, arrives
   in English on a Chinese install. Wrapped like the look settings, because
   private mode throws on access rather than answering null. */
const LANG_KEY = 'raven.gui.lang';

function langRemember(v) {
  try { localStorage.setItem(LANG_KEY, v); } catch { /* private mode */ }
}

/* Applied before the socket is up. Whatever `loadLang` resolves afterwards
   wins, so a language changed elsewhere still lands on this boot. */
function langRestore() {
  try {
    const v = localStorage.getItem(LANG_KEY);
    if (v === 'en' || v === 'zh') langSet(v);
  } catch { /* private mode */ }
}

async function loadLang() {
  try {
    const r = await gateway().call('config.get', { keys: ['language'] });
    const v = r && r.config && r.config.language;
    if (v === 'en' || v === 'zh') { langSet(v); langRemember(v); redrawAll(); }
  } catch { /* stay on the built-in default */ }
}

/* A model id is provider-qualified (openrouter/anthropic/claude-opus-4.6);
   the chip only has room for the part that identifies the model. */
const shortModel = (m) => String(m || '').split('/').pop();
const setModelLabel = () => {
  const model = modelCurrent();
  $('#modelName').textContent = shortModel(model); $('#modelChip').title = model;
};

/* The version check the rail-foot notice already does, on demand. No new
   backend: system.version carries the answer. */
async function checkUpdate(btn) {
  const was = btn.textContent;
  btn.textContent = T('gui.set.checking'); btn.disabled = true;
  try {
    /* check:true = fetch now, not the daily cache: the button says check for
     updates, and a person who just clicked it is asking about now. */
    const v = await gateway().call('system.version', { check: true });
    if (v.raven_version) appVersionSet(v.raven_version);
    if (hasUpdateFlag(v)) {
      showUpNote('ver', v.latest_version);
      drawSettings();
      askUpgrade();
      return;
    }
    /* The answer has to land on the button: this layer sends toasts to the
     console, and "nothing happened" is indistinguishable from a broken
     check. */
    btn.disabled = false;
    btn.textContent = T('gui.set.abt.latest');
    setTimeout(() => { btn.textContent = was; }, 2200);
    return;
  } catch (e) {
    btn.textContent = T('gui.set.abt.check_fail');
    setTimeout(() => { btn.textContent = was; }, 2600);
    if (window.console) console.error('[update check]', e);
  }
  btn.disabled = false;
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  /* The chip the provider refresh and the settings default both move. */
  setChipPainter(setModelLabel);
  setSettingsChrome({
    version: () => APP_VERSION,
    checkUpdate,
    /* Not awaited: the pick repaints synchronously and the persist speaks for
       itself if it fails. */
    setLang: (v) => { langPickLive(v, { persist: true }); },
  });

  sources.banner = bannerSource;

  /* The pick's write-back. In a conversation it reaches that conversation only
   (config.set under its session_id; the gate reads it live). A draft has no
   session_id to write under yet, so the pick is staged and applied to the
   session the first message mints -- the shape the model and tier chips take
   (`applyStagedPerm` in 080). The default is the settings panel's to change.
   Registered here because this file owns the settings transport; the chip
   asks for it only when a pick is made. */
  setPermPersister((m) => {
    const sid = sessionCurrent();
    if (!sid) { staging().perm = m; return true; }
    return gateway().call('config.set', { key: 'permissions.mode', value: m, scope: 'session', session_id: sid })
      .then((r) => {
        if (r && r.applied) return true;
        toast(T('gui.perm.save_failed'));
        return false;
      })
      .catch(() => { toast(T('gui.perm.save_failed')); return false; });
  });

  sources.settings = settingsSource;
  sources.tier = tierSource;
  sources.model = modelSource;

  sources.composer.beforeSend = openModelsForMissingProvider;

  $('#modelChip').onclick = () => {
    if (openModelsForMissingProvider()) return;
    openModelPicker(null, setModelLabel);
  };
}

export { langPickLive, redrawAll, LANG_KEY, langRemember, langRestore, loadLang, shortModel, setModelLabel, checkUpdate }
