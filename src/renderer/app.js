'use strict';

// ── State ───────────────────────────────────────────────
let settings = {};
let mountStatus = 'stopped';
let activityLog = [];

// ── Helpers ─────────────────────────────────────────────
const el  = id => document.getElementById(id);
const fmt = n  => n >= 1024 ? (n / 1024).toFixed(1) + ' TB' : n.toFixed(0) + ' GB';
function ts() {
  const d = new Date();
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}

// ── Navigation ──────────────────────────────────────────
function navigate(pageId) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  const page = el('page-' + pageId);
  if (page) page.classList.add('active');
  const navBtn = document.querySelector(`.nav-item[data-page="${pageId}"]`);
  if (navBtn) navBtn.classList.add('active');
}
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => navigate(btn.dataset.page));
});

// ── Settings tabs ────────────────────────────────────────
function switchSettingsTab(tabId) {
  document.querySelectorAll('.stab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.spane').forEach(p => p.classList.remove('active'));
  const tab  = document.querySelector(`.stab[data-stab="${tabId}"]`);
  const pane = el('spane-' + tabId);
  if (tab)  tab.classList.add('active');
  if (pane) pane.classList.add('active');
}
document.querySelectorAll('.stab').forEach(btn => {
  btn.addEventListener('click', () => switchSettingsTab(btn.dataset.stab));
});

// ── Login screen ─────────────────────────────────────────
function showLogin() {
  el('login').classList.remove('hidden');
}
function hideLogin() {
  el('login').classList.add('hidden');
}

el('loginBtn').addEventListener('click', async () => {
  const email = el('lEmail').value.trim();
  const pass  = el('lPass').value;
  el('loginErr').textContent = '';
  if (!email || !pass) { el('loginErr').textContent = 'Please enter email and password.'; return; }
  const s = await window.electronAPI.getSettings();
  await window.electronAPI.saveSettings({ ...s, email, password: pass });
  hideLogin();
  loadSettings();
  logActivity('Signed in as ' + email, 'ok');
});
el('lPass').addEventListener('keydown', e => { if (e.key === 'Enter') el('loginBtn').click(); });

// ── Load settings into UI ────────────────────────────────
async function loadSettings() {
  settings = await window.electronAPI.getSettings();

  // Sidebar account
  const email = settings.email || '';
  el('sideEmail').textContent = email || 'Not signed in';
  el('sideAvatar').textContent = email ? email[0].toUpperCase() : '?';

  // Storage
  const usedGb  = parseFloat(settings.storageUsedGb)  || 0;
  const totalGb = parseFloat(settings.storageTotalGb) || 2048;
  const pct     = Math.min((usedGb / totalGb) * 100, 100);
  el('storageFill').style.width = pct + '%';
  el('storageUsed').textContent  = fmt(usedGb)  + ' used';
  el('storageTotal').textContent = fmt(totalGb);

  // Settings form
  el('sEmail').value      = settings.email            || '';
  el('sPass').value       = settings.password         || '';
  el('sMountpoint').value = settings.mountpoint       || '';
  el('sDegooPath').value  = settings.degooPath        || '/';
  el('sCacheMb').value    = settings.cacheSizeMb      || 128;
  el('sChunkAge').value   = settings.chunkMaxAge      || 3600;
  el('sThreads').value    = settings.downloadThreads  || 8;
  el('sSubchunk').value   = settings.subchunkConnections || 8;
  el('sLookahead').value  = settings.lookaheadChunks  || 2;
  el('sRefresh').value    = settings.refreshIntervalMin || 10;
  el('sDbPath').value     = settings.dbPath           || '';
  el('sChunkDir').value   = settings.chunkCacheDir    || '';

  // Toggles
  const togLaunch = el('togLaunch');
  if (settings.startOnLaunch) togLaunch.classList.add('on');
  else togLaunch.classList.remove('on');

  const togAllowOther = el('togAllowOther');
  if (settings.allowOther) togAllowOther.classList.add('on');
  else togAllowOther.classList.remove('on');

  // About info
  el('infoMount').textContent = settings.mountpoint   || '—';
  el('infoDb').textContent    = settings.dbPath       || '—';
  el('infoChunk').textContent = settings.chunkCacheDir || '—';

  // Show login if no credentials
  if (!settings.email || !settings.password) showLogin();
  else hideLogin();
}

// ── Status rendering ─────────────────────────────────────
function renderStatus(s) {
  mountStatus = s;
  const dot   = el('statusDot');
  const label = el('statusPillLabel');
  const heroI = el('heroIcon');
  const title = el('heroTitle');
  const sub   = el('heroSub');
  const btn   = el('mountBtn');

  dot.className   = 'status-dot ' + s;
  label.className = 'status-pill-label ' + s;

  el('iconCheck').style.display = 'none';
  el('iconSpin').style.display  = 'none';
  el('iconErr').style.display   = 'none';

  heroI.classList.remove('running', 'error');

  if (s === 'running') {
    label.textContent = 'Syncing';
    el('iconSpin').style.display = 'block';
    heroI.classList.add('running');
    title.textContent = 'Syncing your files…';
    sub.textContent   = 'Your Degoo drive is mounted and syncing.';
    btn.textContent   = 'Stop sync';
    btn.disabled      = false;
  } else if (s === 'error') {
    label.textContent = 'Error';
    el('iconErr').style.display = 'block';
    heroI.classList.add('error');
    title.textContent = 'Something went wrong';
    sub.textContent   = 'Check logs for details, then try restarting.';
    btn.textContent   = 'Retry';
    btn.disabled      = false;
  } else {
    label.textContent = 'Stopped';
    el('iconCheck').style.display = 'block';
    title.textContent = 'Your files are up to date';
    sub.textContent   = 'Start the mount to access your Degoo cloud storage as a local drive.';
    btn.textContent   = 'Start sync';
    btn.disabled      = false;
  }
}

// ── Activity log ─────────────────────────────────────────
function logActivity(msg, type = 'ok') {
  activityLog.unshift({ msg, type, time: ts() });
  if (activityLog.length > 50) activityLog.pop();
  renderActivity();
}
function renderActivity() {
  const list  = el('activityList');
  const empty = el('activityEmpty');
  if (!activityLog.length) { empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  list.innerHTML = activityLog.map(e => `
    <div class="activity-item">
      <div class="act-dot${e.type === 'err' ? ' err' : e.type === 'warn' ? ' warn' : ''}"></div>
      <div class="act-msg">${e.msg}</div>
      <div class="act-time">${e.time}</div>
    </div>`).join('') + `<div class="activity-empty" id="activityEmpty" style="display:none">No recent activity</div>`;
}

// ── Mount button ─────────────────────────────────────────
el('mountBtn').addEventListener('click', async () => {
  if (mountStatus === 'running') {
    // stop
    await window.electronAPI.saveSettings({ ...settings, _action: 'stop' });
    logActivity('Mount stopped', 'warn');
  } else {
    if (!settings.email || !settings.password) { showLogin(); return; }
    logActivity('Starting mount…', 'ok');
    await window.electronAPI.saveSettings({ ...settings });
  }
});

el('openFolderBtn').addEventListener('click', () => {
  window.electronAPI.openFolder(settings.mountpoint);
});

// ── Settings save/cancel ─────────────────────────────────
el('sSaveBtn').addEventListener('click', async () => {
  const updated = {
    email:                el('sEmail').value.trim(),
    password:             el('sPass').value,
    mountpoint:           el('sMountpoint').value.trim(),
    degooPath:            el('sDegooPath').value.trim() || '/',
    cacheSizeMb:          Number(el('sCacheMb').value),
    chunkMaxAge:          Number(el('sChunkAge').value),
    downloadThreads:      Number(el('sThreads').value),
    subchunkConnections:  Number(el('sSubchunk').value),
    lookaheadChunks:      Number(el('sLookahead').value),
    refreshIntervalMin:   Number(el('sRefresh').value),
    dbPath:               el('sDbPath').value.trim(),
    chunkCacheDir:        el('sChunkDir').value.trim(),
    startOnLaunch:        el('togLaunch').classList.contains('on'),
    allowOther:           el('togAllowOther').classList.contains('on'),
  };
  await window.electronAPI.saveSettings(updated);
  settings = { ...settings, ...updated };
  const ok = el('saveOk');
  ok.classList.add('show');
  setTimeout(() => ok.classList.remove('show'), 2500);
  logActivity('Settings saved', 'ok');
  loadSettings();
});
el('sCancelBtn').addEventListener('click', loadSettings);

// Browse buttons
async function browse(inputId) {
  const p = await window.electronAPI.browseFolder();
  if (p) el(inputId).value = p;
}
el('sBrowseMount').addEventListener('click', () => browse('sMountpoint'));
el('sBrowseDb').addEventListener('click',    () => browse('sDbPath'));
el('sBrowseChunk').addEventListener('click', () => browse('sChunkDir'));

// Toggles
el('togLaunch').addEventListener('click',     () => el('togLaunch').classList.toggle('on'));
el('togAllowOther').addEventListener('click', () => el('togAllowOther').classList.toggle('on'));

// Sign out
el('signOutBtn').addEventListener('click', async () => {
  await window.electronAPI.saveSettings({ ...settings, email: '', password: '', _action: 'stop' });
  logActivity('Signed out', 'warn');
  await loadSettings();
});

// ── Titlebar buttons ─────────────────────────────────────
el('tbFolder').addEventListener('click', () => window.electronAPI.openFolder(settings.mountpoint));
el('tbWeb').addEventListener('click',    () => window.electronAPI.openExternal('https://app.degoo.com'));

// ── About page buttons ───────────────────────────────────
el('aGithub').addEventListener('click',  () => window.electronAPI.openExternal('https://github.com/Laitinlok/degoo_drive'));
el('aDegooWeb').addEventListener('click',() => window.electronAPI.openExternal('https://degoo.com'));
el('aLogs').addEventListener('click',    () => window.electronAPI.openExternal('logs'));

// ── Backup add ───────────────────────────────────────────
el('addBackupBtn').addEventListener('click', async () => {
  const p = await window.electronAPI.browseFolder();
  if (!p) return;
  const card = document.createElement('div');
  card.className = 'backup-card';
  const name = p.split('/').pop() || p;
  card.innerHTML = `
    <div class="bc-name">${name}</div>
    <div class="bc-path">${p}</div>
    <div class="bc-status">Active</div>`;
  el('addBackupBtn').insertAdjacentElement('beforebegin', card);
  logActivity('Backup added: ' + name, 'ok');
});

// ── Status listener from main process ───────────────────
window.electronAPI.onStatus((_, s) => {
  renderStatus(s);
  logActivity(s === 'running' ? 'Mount started' : s === 'error' ? 'Mount error — check logs' : 'Mount stopped', s === 'error' ? 'err' : s === 'running' ? 'ok' : 'warn');
});

// ── Init ─────────────────────────────────────────────────
(async () => {
  await loadSettings();
  const s = await window.electronAPI.getStatus();
  renderStatus(s);
})();
