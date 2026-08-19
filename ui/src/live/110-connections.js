/* -- connections (channels) ------------------------------------------ */
async function loadChannels() {
  const r = await rpc.call('channels.status', {});
  const byName = Object.fromEntries(r.channels.map((c) => [c.name, c]));
  CHANNELS.forEach((c) => {
    const s = byName[c.id];
    if (!s) return;
    /* No prose state line: the LED and the switch say on/off, and a missing
       credential says 未配置 through c.missing. `who` is reserved for a real
       identity (the account the channel signs in as), which no backend
       supplies yet -- so live rows keep their sub line empty. */
    c.who = '';
    /* The schema-declared field list rides the status row; the page's
       configure form is drawn from it, so the form and the config can't drift. */
    c.fields = s.fields || [];
    c.missing = s.missing || [];
    c._on = s.enabled;
    /* Three separate facts, kept separate. `on` is what the config asks for;
       `running` is whether the adapter came up; `connected` is whether the
       account is paired, which only the QR channels report. Absent means the
       gateway could not be asked -- not "no". */
    c.running = s.running;
    c.connected = s.connected;
    c.qrLogin = !!s.qr_login;
    if (!c._live) {
      c._live = true;
      Object.defineProperty(c, 'on', {
        get: () => c._on,
        set: (v) => {
          c._on = v;
          rpc.call('settings.set', { key: `channels.${c.id}.enabled`, value: v })
            .then(() => toast(T('gui.conn.toggled', { name: chanName(c), state: T(v ? 'gui.conn.enabled' : 'gui.conn.disabled') })))
            .catch((e) => { c._on = !v; toast(`保存失败：${e.message || e}`); drawConn(); });
        },
      });
    }
  });
  gatewayRunningLive = r.gateway_running;
}
let gatewayRunningLive = false;

/* Credentials and the switch travel together, and the server applies them in
   that order, so a channel is never on without the values it was turned on
   for. `enable` used to be a local `c.on = true` that `loadChannels()` then
   overwrote from the server -- which made connect a no-op that looked like it
   worked, and disconnect a no-op with nothing to show for it at all. */
connApply = async (c, patch, enable) => {
  try {
    const fields = patch && Object.keys(patch).length ? patch : {};
    await rpc.call('channels.configure', { name: c.id, fields, enabled: !!enable });
    if (Object.keys(fields).length) toast(T('gui.conn.saved_x', { name: chanName(c) }));
    await loadChannels();
  } catch (e) {
    toast(`保存失败：${(e.data && e.data.detail) || e.message || e}`);
  }
  drawConn();
};

let chanLoaded = false;
openConn = async function () {
  showPage('connPage');
  if (chanLoaded) drawConn();
  else $('#connBody').innerHTML = '';
  try { await loadChannels(); chanLoaded = true; } catch (e) { toast(`加载失败：${e.message || e}`); }
  drawConn();
  if (!gatewayRunningLive && CHANNELS.some((c) => c.on)) {
    toast('已启用的入口还没在收消息 — 重新打开 Raven App 即可生效');
  }
};

