(async () => {
  const d = window.degoo;
  const s  = await d.getSettings();
  const st = await d.getStatus();

  // Tabs
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('panel-' + tab.dataset.tab).classList.add('active');
    });
  });

  // Load fields
  const FIELDS = [
    'email','password','mountpoint','syncDir','degooPath',
    'cacheSizeMb','refreshIntervalMin','downloadThreads',
    'subchunkConnections','lookaheadChunks','chunkMaxAge','pollSecs',
  ];
  FIELDS.forEach(id => {
    const el = document.getElementById(id);
    if (el && s[id] !== undefined) el.value = s[id];
  });

  // Toggles
  let mountEnabled = s.mountEnabled ?? true;
  let syncEnabled  = s.syncEnabled  ?? false;
  const mt = document.getElementById('mountToggle');
  const st2 = document.getElementById('syncToggle');
  const rmt = () => mt.classList.toggle('on', mountEnabled);
  const rst = () => st2.classList.toggle('on', syncEnabled);
  rmt(); rst();
  document.getElementById('mountToggleRow').addEventListener('click', () => { mountEnabled=!mountEnabled; rmt(); });
  document.getElementById('syncToggleRow' ).addEventListener('click', () => { syncEnabled=!syncEnabled;   rst(); });

  // Status
  const mBadge = document.getElementById('mountBadge');
  const sBadge = document.getElementById('syncBadge');
  function cap(v='') { return v.charAt(0).toUpperCase()+v.slice(1); }
  function applyStatus({ sync, mount }) {
    mBadge.textContent = 'Mount: '+cap(mount);
    mBadge.className   = 'badge '+(mount||'');
    sBadge.textContent = 'Sync: '+cap(sync);
    sBadge.className   = 'badge '+(sync||'');
  }
  applyStatus(st);
  d.onStatus(applyStatus);

  // Activity log
  const actList = document.getElementById('activityList');
  let firstAct = true;
  const kindClass = { upload:'ku', download:'kd', conflict:'kc', error:'ke' };
  d.onProgress(({ rel_path, kind, detail }) => {
    if (firstAct) { actList.innerHTML=''; firstAct=false; }
    const li = document.createElement('li');
    const k  = kindClass[kind]||'';
    li.innerHTML = `<span class="${k}">${cap(kind)}</span>  ${rel_path}`
                 + (detail ? ` — ${detail}` : '');
    actList.prepend(li);
    while (actList.children.length > 100) actList.removeChild(actList.lastChild);
  });

  // Browse
  document.querySelectorAll('[data-browse]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const p = await d.browseFolder();
      if (p) document.getElementById(btn.dataset.browse).value = p;
    });
  });

  // Mount / Sync buttons
  document.getElementById('mountStartBtn').addEventListener('click', () => d.startMount());
  document.getElementById('mountStopBtn' ).addEventListener('click', () => d.stopMount());
  document.getElementById('syncStartBtn' ).addEventListener('click', () => d.startSync());
  document.getElementById('syncStopBtn'  ).addEventListener('click', () => d.stopSync());

  // Save
  document.getElementById('saveBtn').addEventListener('click', async () => {
    const data = { mountEnabled, syncEnabled };
    FIELDS.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      data[id] = el.type==='number' ? Number(el.value) : el.value;
    });
    await d.saveSettings(data);
    const msg = document.getElementById('savedMsg');
    msg.classList.add('show');
    setTimeout(() => msg.classList.remove('show'), 2500);
  });

  document.getElementById('cancelBtn').addEventListener('click', () => window.close());
})();
