/* -- attachments -----------------------------------------------------
   Files are uploaded into <workspace>/uploads and handed to the agent as
   paths: every file tool is already workspace-scoped, so a path is all it
   needs. Bytes never ride inside the message. */
const atts = [];
const fmtSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB`
  : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);

function drawAtts() {
  let box = $('#atts');
  if (!box) {
    box = mk('div', 'atts');
    box.id = 'atts';
    const dock = $('#ta').closest('.dock-in');
    dock.insertBefore(box, dock.querySelector('.field'));
  }
  box.innerHTML = '';
  box.hidden = !atts.length;
  atts.forEach((a, i) => {
    const isImg = !!a.url;
    const chip = mk('div', 'att' + (isImg ? ' img' : '') + (a.uploading ? ' up' : ''));
    if (isImg) {
      const img = mk('img');
      img.src = a.url;
      img.alt = a.name;
      img.title = `${a.name} · ${a.uploading ? T('gui.att.uploading') : fmtSize(a.size)}`;
      /* the square crops the image, so a click has to be able to show all of it */
      img.onclick = () => openImage(a.url, a.name);
      chip.appendChild(img);
    } else {
      chip.append(mk('span', 'nm', a.name), mk('span', 'sz', a.uploading ? T('gui.att.uploading') : fmtSize(a.size)));
    }
    const rm = mk('button', 'rm', '✕');
    rm.setAttribute('aria-label', T('gui.att.remove', { name: a.name }));
    rm.onclick = (e) => { e.stopPropagation(); atts.splice(i, 1); drawAtts(); };
    chip.appendChild(rm);
    box.appendChild(chip);
  });
  /* A staged file is enough to send, so the button's enabled state follows the
     tray, not just the field. */
  goState();
}

hasAtts = () => atts.length > 0;

function addFiles(fileList) {
  [...fileList].forEach((file) => {
    const entry = { name: file.name, size: file.size, uploading: true, path: null, url: null };
    atts.push(entry);
    drawAtts();
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result);
      const b64 = dataUrl.split(',')[1] || '';
      /* Keep the bytes for display only: an image renders as itself in the
         composer, and once uploaded, keyed by path, in the sent bubble. */
      if (/^image\//.test(file.type || '')) entry.url = dataUrl;
      rpc.call('fs.upload', { name: file.name, content_b64: b64, session: cur || '' })
        .then((r) => {
          entry.path = r.path; entry.size = r.size; entry.uploading = false;
          if (entry.url) ATT_IMG.set(r.path, entry.url);
          drawAtts();
        })
        .catch((e) => {
          const i = atts.indexOf(entry);
          if (i >= 0) atts.splice(i, 1);
          drawAtts();
          noteRow(T('gui.att.fail', { name: file.name }), (e.data && e.data.detail) || e.message || String(e));
        });
    };
    reader.onerror = () => {
      const i = atts.indexOf(entry);
      if (i >= 0) atts.splice(i, 1);
      drawAtts();
    };
    reader.readAsDataURL(file);
  });
}

{
  const picker = mk('input');
  picker.type = 'file';
  picker.multiple = true;
  picker.style.display = 'none';
  picker.onchange = () => { addFiles(picker.files); picker.value = ''; };
  document.body.appendChild(picker);
  $('#attBtn').onclick = () => picker.click();

  const field = $('#ta').closest('.field');
  let dragDepth = 0;
  const over = (e) => { e.preventDefault(); };
  field.addEventListener('dragenter', (e) => { over(e); dragDepth++; field.classList.add('drop'); });
  field.addEventListener('dragover', over);
  field.addEventListener('dragleave', () => { if (--dragDepth <= 0) field.classList.remove('drop'); });
  field.addEventListener('drop', (e) => {
    e.preventDefault(); dragDepth = 0; field.classList.remove('drop');
    if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });
  $('#ta').addEventListener('paste', (e) => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
}

