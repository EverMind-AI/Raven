/* -- knowledge bases: the rpc source ----------------------------------
   The island talks to DS.knowledge and knows nothing about transport; this
   file only knows how to speak knowledge.* over /rpc. Installing onto the
   same name is what swaps the demo fixtures for a real engine. */

import { DS } from '../seam/000-datasource.js'
import { rpc, uploadRefusalBySize } from './020-rpc.js'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  DS.knowledge = {
    status: () => rpc.call('knowledge.status', {}),
    /* Unwrapped here rather than in the island: the contract answers an object
     so it can grow a field beside the list, and the page wants the list. */
    bases: () => rpc.call('knowledge.bases.list', {}).then((r) => (r && r.bases) || []),
    create: (name, description, embedding = true) =>
      rpc.call('knowledge.bases.create', { name, description, embedding }).then((r) => r && r.base),
    rename: (id, name) =>
      rpc.call('knowledge.bases.rename', { base_id: id, name }).then((r) => r && r.base),
    remove: (id) => rpc.call('knowledge.bases.delete', { base_id: id }),
    /* Spread rather than listed: the contract leaves out what it is not sent,
     and naming every field here would send nulls for the untouched ones. */
    settings: (baseId, values) =>
      rpc.call('knowledge.bases.settings', { base_id: baseId, ...values }).then((r) => r && r.base),
    documents: (baseId) =>
      rpc.call('knowledge.documents.list', { base_id: baseId }).then((r) => (r && r.documents) || []),
    /* The bytes ride up through fs.upload, whose answer is a workspace path --
     the one spelling the gateway will read from. Adding is then told where the
     file is, never handed the bytes. */
    upload: async (baseId, file) => {
      /* Refused before the read, not after: this is the path fed manuals and
       reports, so it is the one that clears the limit often, and encoding a
       file this large only to throw the result away is seconds of a frozen
       tab. `File.size` is the exact count the base64 can only be derived to. */
      const refusal = uploadRefusalBySize(file.name, file.size);
      if (refusal) throw new Error(refusal);
      const b64 = await new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onerror = () => reject(new Error('could not read the file'));
        /* dataURL is `data:<mime>;base64,<payload>`; the payload is what the
         contract takes. */
        fr.onload = () => resolve(String(fr.result || '').split(',')[1] || '');
        fr.readAsDataURL(file);
      });
      const up = await rpc.call('fs.upload', { name: file.name, content_b64: b64 });
      const added = await rpc.call('knowledge.documents.add', { base_id: baseId, path: up.path });
      return added && added.document;
    },
    /* A note is markdown the reader typed, so it goes up as text rather than
     through fs.upload: there is no file on their disk to read. */
    addNote: (baseId, title, text) =>
      rpc
        .call('knowledge.documents.add_note', { base_id: baseId, title, text })
        .then((r) => r && r.document),
    updateNote: (documentId, title, text) =>
      rpc
        .call('knowledge.documents.update_note', { document_id: documentId, title, text })
        .then((r) => r && r.document),
    /* The gateway reads the page. A browser cannot fetch a third-party site on
     the reader's behalf, and the bytes have to reach that process to be
     chunked anyway. */
    addUrl: (baseId, url) =>
      rpc.call('knowledge.documents.add_url', { base_id: baseId, url }).then((r) => r && r.document),
    index: (documentId) =>
      rpc.call('knowledge.documents.index', { document_id: documentId }).then((r) => r && r.document),
    /* Answers nothing: a document that was already gone and one this call
     removed leave the page in the same place, and the list read that
     follows is what the row is drawn from either way. */
    removeDoc: (documentId) =>
      rpc.call('knowledge.documents.delete', { document_id: documentId }).then(() => undefined),
    /* The whole answer, not just the list: the recall panel reports what the
     search cost, and only this frame carries it. */
    search: (baseIds, query, topK) =>
      rpc
        .call('knowledge.search', { base_ids: baseIds, query, top_k: topK })
        .then((r) => ({ hits: (r && r.hits) || [], search_ms: (r && r.search_ms) || 0, embed_ms: (r && r.embed_ms) || 0 })),
  };
}
