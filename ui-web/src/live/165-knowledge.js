/* -- knowledge bases: the rpc source ----------------------------------
   The island talks to DS.knowledge and knows nothing about transport; this
   file only knows how to speak knowledge.* over /rpc. Installing onto the
   same name is what swaps the demo fixtures for a real engine. */
DS.knowledge = {
  status: () => rpc.call('knowledge.status', {}),
  /* Unwrapped here rather than in the island: the contract answers an object
     so it can grow a field beside the list, and the page wants the list. */
  bases: () => rpc.call('knowledge.bases.list', {}).then((r) => (r && r.bases) || []),
  create: (name, description) =>
    rpc.call('knowledge.bases.create', { name, description }).then((r) => r && r.base),
  remove: (id) => rpc.call('knowledge.bases.delete', { base_id: id }),
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
  index: (documentId) =>
    rpc.call('knowledge.documents.index', { document_id: documentId }).then((r) => r && r.document),
  /* Answers nothing: a document that was already gone and one this call
     removed leave the page in the same place, and the list read that
     follows is what the row is drawn from either way. */
  removeDoc: (documentId) =>
    rpc.call('knowledge.documents.delete', { document_id: documentId }).then(() => undefined),
  search: (baseIds, query) =>
    rpc.call('knowledge.search', { base_ids: baseIds, query }).then((r) => (r && r.hits) || []),
};
