(() => {
  let dirty = false;
  const feed = document.querySelector('.event-feed');
  if (feed && !new URLSearchParams(location.search).has('event')) {
    feed.scrollTop = feed.scrollHeight;
    feed.querySelectorAll('img').forEach(img => img.addEventListener('load', () => { feed.scrollTop = feed.scrollHeight; }, { once: true }));
  }
  document.querySelectorAll('[data-copy-session]').forEach(node => {
    node.addEventListener('click', async () => { await navigator.clipboard.writeText(node.dataset.sessionId); node.title = 'Session id copied'; });
  });
  document.addEventListener('input', event => { if (event.target.closest('[data-preserve-input]')) dirty = true; });
  function notify(text) {
    let box = document.querySelector('[data-live-notice]');
    if (!box) { box = document.createElement('div'); box.dataset.liveNotice = ''; box.className = 'watch-live-notice'; box.setAttribute('role', 'status'); document.body.append(box); }
    box.textContent = text + ' ';
    const button = document.createElement('button'); button.textContent = 'Refresh'; button.className = 'underline'; button.onclick = () => location.reload(); box.append(button);
  }
  function clocks() {
    document.querySelectorAll('[data-countdown]').forEach(node => {
      const seconds = Math.ceil((Date.parse(node.dataset.countdown) - Date.now()) / 1000);
      node.textContent = seconds > 0 ? `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}` : 'Deadline elapsed';
    });
  }
  clocks(); setInterval(clocks, 1000);
  const live = document.querySelector('[data-live-url]');
  if (live) setInterval(async () => {
    try {
      const response = await fetch(live.dataset.liveUrl, { headers: { 'Accept': 'application/json' } });
      if (!response.ok) return;
      const state = await response.json();
      if (String(state.version) !== live.dataset.liveVersion) {
        if (dirty) notify('New activity is available. Your unsaved text has been preserved.');
        else location.reload();
      }
    } catch (_) { /* The visible server snapshot remains; next poll retries. */ }
  }, 4000);
  document.querySelectorAll('[data-focus-composer]').forEach(link => link.addEventListener('click', event => {
    const composer = document.getElementById('composer');
    if (!composer) return;
    event.preventDefault();
    composer.scrollIntoView({ behavior: 'smooth', block: 'start' });
    composer.querySelector('textarea, input:not([type="hidden"]), select')?.focus({ preventScroll: true });
  }));
  const noteForm = document.querySelector('[data-note-form]');
  const photoInput = noteForm?.querySelector('input[type="file"]');
  let photos = [];
  let previewUrls = [];
  function showPhotos(message = '') {
    const transfer = new DataTransfer();
    photos.forEach(file => transfer.items.add(file));
    photoInput.files = transfer.files;
    previewUrls.forEach(url => URL.revokeObjectURL(url));
    previewUrls = [];
    const previews = noteForm.querySelector('[data-photo-previews]');
    previews.replaceChildren();
    photos.forEach((file, index) => {
      const figure = document.createElement('figure');
      const img = document.createElement('img');
      const url = URL.createObjectURL(file); previewUrls.push(url);
      img.src = url; img.alt = `Pending photo: ${file.name}`; img.className = 'note-photo-preview';
      const caption = document.createElement('figcaption'); caption.textContent = file.name;
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove';
      remove.className = 'watch-link'; remove.setAttribute('aria-label', `Remove ${file.name}`);
      remove.onclick = () => { photos.splice(index, 1); dirty = true; showPhotos(); };
      figure.append(img, caption, remove); previews.append(figure);
    });
    noteForm.querySelector('[data-photo-status]').textContent = message || (photos.length ? `${photos.length} photo${photos.length === 1 ? '' : 's'} ready. Saved with your note when you choose Add note.` : '');
  }
  function stagePhotos(files) {
    const incoming = Array.from(files);
    let error = '';
    if (photos.length + incoming.length > 8) error = 'Attach up to 8 photos per note.';
    else if (incoming.some(file => !['image/png', 'image/jpeg', 'image/webp'].includes(file.type))) error = 'Choose PNG, JPEG or WebP photos.';
    else if (incoming.some(file => !file.size || file.size > 5 * 1024 * 1024)) error = 'Each photo must be nonempty and at most 5 MB.';
    if (!error) { photos.push(...incoming); dirty = true; }
    showPhotos(error);
  }
  photoInput?.addEventListener('change', () => stagePhotos(photoInput.files));
  if (noteForm) {
    noteForm.addEventListener('dragover', event => { if (event.dataTransfer?.types.includes('Files')) { event.preventDefault(); noteForm.classList.add('note-dragover'); } });
    noteForm.addEventListener('dragleave', event => { if (!noteForm.contains(event.relatedTarget)) noteForm.classList.remove('note-dragover'); });
    noteForm.addEventListener('drop', event => {
      noteForm.classList.remove('note-dragover');
      if (event.dataTransfer?.files.length) { event.preventDefault(); stagePhotos(event.dataTransfer.files); }
    });
  }
  document.addEventListener('paste', async event => {
    const form = document.querySelector('[data-paste-upload]');
    if (!form || !event.target.closest('[data-preserve-input]')) return;
    const pasted = Array.from(event.clipboardData?.files || []).filter(item => item.type.startsWith('image/'));
    if (!pasted.length) return;
    event.preventDefault();
    if (noteForm && noteForm.contains(event.target)) { stagePhotos(pasted); return; }
    const file = pasted[0];
    const data = new FormData(form); data.set('attachment-file', file, file.name || 'pasted-image.png'); data.set('attachment-description', 'Image pasted by the operator.');
    try {
      const response = await fetch(form.getAttribute('action') || location.pathname, { method: 'POST', body: data });
      if (response.ok) { if (dirty) notify('Image attached. Your unsaved text is preserved.'); else location.reload(); }
      else notify('Image could not be attached. Use Upload evidence to check the file and try again.');
    } catch (_) { notify('Image could not be attached. Your unsaved text is preserved.'); }
  });
})();
