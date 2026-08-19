/* -- real session actions: branch / clear ---------------------------- */
answerBlock = function (text, meta, anchor) {
  const box = mk('div', 'answer in');
  const body = mk('div', 'prose');
  const acts = mk('div', 'acts');
  const add = (label, path, fn) => {
    const b = mk('button');
    b.dataset.tip = label;
    b.dataset.label = label;
    b.setAttribute('aria-label', label);
    b.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
    b.onclick = fn;
    acts.appendChild(b);
    return b;
  };
  // Toasts are gone, so an action reports back through its own hover label.
  const flash = (b, word) => {
    b.dataset.tip = word;
    setTimeout(() => { b.dataset.tip = b.dataset.label; }, 1400);
  };
  const copy = add(T('gui.answer.copy'), '<rect x="9" y="9" width="11" height="11" rx="2.5"/>'
    + '<path d="M5.5 15H5a1.5 1.5 0 0 1-1.5-1.5v-8A1.5 1.5 0 0 1 5 4h8A1.5 1.5 0 0 1 14.5 5.5V6"/>', () => {
    navigator.clipboard && navigator.clipboard.writeText(text);
    flash(copy, T('gui.answer.copied'));
  });
  /* Branching acts on `cur` -- the OPEN conversation -- so inside a sub-agent
     transcript (stageHost points the renderer at the panel) the button would
     fork the parent session while claiming to fork the run. A run has no
     session to fork; the row shows its clock instead, which the record kept. */
  const canBranch = !stageHost;
  if (canBranch) {
    add(T('gui.answer.branch'), '<circle cx="7" cy="6" r="2.2"/><circle cx="7" cy="18" r="2.2"/>'
      + '<circle cx="17" cy="8" r="2.2"/><path d="M7 8.2v7.6"/>'
      + '<path d="M17 10.2v1.3a3.5 3.5 0 0 1-3.5 3.5H10"/>', () => {
      rpc.call('session.branch', { session_id: cur })
        .then((r) => {
          if (!r.session_id) { toast('这个会话还没有内容，无法分叉'); return; }
          const s = { id: r.session_id, title: r.title || T('gui.sess.branch_title'),
            last: T('gui.sess.branched'), when: T('gui.sess.just_now'),
            at: Math.floor(Date.now() / 1000), run: null, live: true };
          SESS.unshift(s); cur = s.id; drawList(); openSession(s);
          toast(`已分叉，带上了 ${r.message_count || 0} 条消息`);
        })
        .catch((e) => toast(`分叉失败：${e.message || e}`));
    });
  }
  // Copy and branch only — the footer stays a quiet two-icon row per the
  // maintainer's explicit call. Undo/regenerate live in session commands.
  const foot = mk('div', 'ansfoot');
  foot.appendChild(acts);
  if (meta) foot.appendChild(mk('span', 'turnmeta', meta));
  /* Same two actions as the footer: right-clicking an answer should not offer
     less than hovering it does. Selected text keeps the host menu instead --
     copying a quote is what a selection is for. */
  ctxMenu(box, () => [
    { label: T('gui.answer.copy'), fn: () => copyToClip(text, T('gui.answer.copied')) },
  ].concat(canBranch ? [{ label: T('gui.answer.branch'), fn: () => acts.children[1].click() }] : []));
  box.append(body, foot);
  /* `anchor` is where the streamed prose stood. Appending instead was a real
     reordering: anything that landed mid-turn -- a delegated result's delivery
     row, a runtime notice -- was drawn at the tail while the text was still
     streaming, and then the finished answer jumped BELOW it, so the page
     claimed the result came back before the model said it was dispatching one. */
  stageBox().insertBefore(box, anchor || null);
  return body;
};

SLASH.forEach((x) => {
  if (x.id === 'gui.clear') {
    x.fn = () => confirmAsk(T('gui.clear_title'), T('gui.clear_body'), T('gui.clear_yes'), () => {
      rpc.call('session.clear', { session_id: cur })
        .then(() => {
          $('#stage').innerHTML = ''; pitch();
          const s = sess(cur); if (s) s.last = T('gui.sess.cleared');
          use = null; drawMeter(); drawList();
        })
        .catch((e) => noteRow(T('gui.clear_title'), (e.data && e.data.detail) || e.message || String(e)));
    });
  }
  if (x.id === 'gui.compress') x.fn = compressNow;
});

/* Manual compaction. The runtime already compacts when a prompt outgrows the
   window; this forces the same pass early, which is what you want once the
   earlier half of a session has stopped being useful. */
async function compressNow() {
  if (!cur || draft) return;
  const line = noteRow(T('gui.compress.running'), '', { quiet: true, host: $('#stage') });
  try {
    const r = await rpc.call('session.compress', { session_id: cur });
    noteSay(line, r.removed
      ? T('gui.compress.done', { n: r.removed, before: fmtTok(r.before_tokens), after: fmtTok(r.after_tokens) })
      : T('gui.compress.noop'), '');
  } catch (e) {
    line.remove();
    noteRow(T('gui.compress.fail', { err: '' }).replace(/[:：]\s*$/, ''),
      (e.data && e.data.detail) || e.message || String(e));
  }
  down();
}

// Dev-only hook: lets a design pass preview the clarify sheet without
// spending a model turn (window.__clarify({question, choices})).
window.__clarify = (p) => rpc.notify['clarify.request'](p || { request_id: 'dev', question: '预览', choices: ['A', 'B'] });

// Same reason: the update row's version state only appears when a release is
// actually newer, which never happens on a dev checkout
// (window.__upnote('ver', '0.1.11')).
window.__upnote = (kind, latest) => showUpNote(kind || 'ver', latest);

