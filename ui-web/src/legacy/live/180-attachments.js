/* -- attachments -----------------------------------------------------
   The tray is the composer island's (ui-web/src/features/composer/): the chips,
   the staging, the drop target and the picker all live there. What only the
   live layer can do is the one thing left here -- put the bytes somewhere the
   agent can read them.

   Files are uploaded into <workspace>/uploads and handed to the agent as
   paths: every file tool is already workspace-scoped, so a path is all it
   needs. Bytes never ride inside the message. */

import { DS } from '../seam/000-datasource.js'
import { rpc, uploadRefusal } from './020-rpc.js'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  DS.composer.upload = (p) => {
    const refusal = uploadRefusal(p.name, p.content_b64);
    /* Rejected, not returned: the tray already renders a rejection as the chip's
     failure note, and a refusal is one -- the file is not attached either way. */
    if (refusal) return Promise.reject(new Error(refusal));
    return rpc.call('fs.upload', {
      name: p.name,
      content_b64: p.content_b64,
      /* Read per call, not captured: a file can be staged in a draft that becomes
       a session between the pick and the upload. */
      session: sessionCurrent() || '',
    });
  };
}
