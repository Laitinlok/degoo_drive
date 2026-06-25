(async () => {
  const d = window.degoo;
  const s = await d.getSettings();
  const st = await d.getStatus();
  const fields = ['email','password','mountpoint','degooPath','cacheSizeMb',
                  'refreshIntervalMin','downloadThreads','subchunkConnections',
                  'lookaheadChunks','chunkMaxAge'];
  fields.forEach(id => {
    const el = document.getElementById(id);
    if (el && s[id] !== undefined) el.value = s[id];
  });
  let startOnLaunch = s.startOnLaunch ?? true;
  const tog = document.getElementById('tog');
  const sync = () => tog.classList.toggle('on', startOnLaunch);
  sync();
  document.getElementById('togRow').addEventListener('click', () => { startOnLaunch = !startOnLaunch; sync(); });
  const badge = document.getElementById('badge');
  function setStatus(v) { badge.textContent = v.charAt(0).toUpperCase() + v.slice(1); badge.className = 'badge ' + v; }
  setStatus(st);
  d.onStatus(setStatus);
  document.getElementById('browseBtn').addEventListener('click', async () => {
    const p = await d.browseFolder();
    if (p) document.getElementById('mountpoint').value = p;
  });
  document.getElementById('saveBtn').addEventListener('click', async () => {
    const data = { startOnLaunch };
    fields.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      data[id] = el.type === 'number' ? Number(el.value) : el.value;
    });
    await d.saveSettings(data);
    const ok = document.getElementById('ok');
    ok.classList.add('show');
    setTimeout(() => ok.classList.remove('show'), 2500);
  });
  document.getElementById('cancelBtn').addEventListener('click', () => window.close());
})();
