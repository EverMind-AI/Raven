/* ---- history: the real source behind the transcript island ---------- */
/* Reading a stored turn as segments lives with the renderer (the transcript
   island); what stays here is the live half of DS.transcript -- how a tool
   result is previewed and judged, which is wire knowledge -- and the shim the
   session openers still call. The delegation verbs keep the fixture's
   late-bound closures: live/240 assigns the demo seams they read. */
DS.transcript = {
  ...DS.transcript,
  clean: (text) => cleanPreview(text),
  okOf: (name, preview) => okOf(name, preview),
};

function renderHistory(messages) {
  /* Opening a stored conversation IS content: the new-task flag comes down
     before the island paints. */
  const ch = document.querySelector('.chat');
  if (ch) delete ch.dataset.fresh;
  RavenIslands.transcript.history(messages);
}
