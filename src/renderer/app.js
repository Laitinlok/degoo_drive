/* Renderer process — communicates with main via contextBridge (preload) */
'use strict';

const api = window.electronAPI;

// ── DOM refs ──────────────────────────────────────────────────────────────
const avatar        = document.getElementById('avatar');
const accountEmail  = document.getElementById('accountEmail');
const storageFill   = document.getElementById('storageFill');
const storageLabel  = document.getElementById('storageLabel');
const iconWrap      = document.getElementById('iconWrap');
const iconCheck     = document.getElementById('iconCheck');
const iconSpin      = document.getElementById('iconSpin');
const iconErr       = document.getElementById('iconErr');
const statusTitle   = document.getElementById('statusTitle');
const statusSub     = document.getElementById('statusSub');
const pillDot       = document.getElementById('pillDot');
const pillText      = document.getElementById('pillText');
const gearBtn       = document.getElementById('gearBtn');
const folderBtn     = document.getElementById('folderBtn');
const webBtn        = document.getElementById('webBtn');
const settingsPanel = document.getElementById('settings');
const backBtn       = document.getElementById('backBtn');
const cancelBtn     = document.getElementById('cancelBtn');
const saveBtn       = document.getElementById('saveBtn');
const browseBtn     = document.getElementById('browseBtn');
const togRow        = document.getElementById('togRow');
const tog           = document.getElementById('tog');
const okMsg         = document.getElementById('ok');

// ── Status render ─────────────────────────────────────────────────────────
const STATUS_CONFIG = {
  stopped: {
    icon: 'check',
    wrapClass: '',
    title: 'Your files are up to date',
    sub:   'Sync activity will show up here',
    dot:   '',
    pill:  'Stopped',
    checkStroke: '#8e8e93',
  },
  running: {
    icon: 'spin',
    wrapClass: 'running',
    title: 'Syncing…',
    sub:   'Connecting to Degoo',
    dot:   'running',
    pill:  'Syncing',
    checkStroke: '#8e8e93',
  },
  synced: {
    icon: 'check',
    wrapClass: 'running',
    title: 'Your files are up to date',
    sub:   'Sync activity will show up here',
    dot:   'running',
    pill:  'Fully synced',
    checkStroke: '#01a99c',
  },
  error: {
    icon: 'err',
    wrapClass: 'error',
    title: 'Sync paused',
    sub:   'Check settings or view logs',
    dot:   'error',
    pill:  'Error',
    checkStroke: '#8e8e93',
  },
};

function applyStatus(raw) {
  // Map main-process states → UI states
  const state = raw === 'running' ? 'synced' : (STATUS_CONFIG[raw] ? raw : 'stopped');
  const cfg = STATUS_CONFIG[state];

  // icon
  iconCheck.style.display = cfg.icon === 'check' ? '' : 'none';
  iconSpin.style.display  = cfg.icon === 'spin'  ? '' : 'none';
  iconErr.style.display   = cfg.icon === 'err'   ? '' : 'none';
  if (cfg.icon === 'check') {
    iconCheck.querySelector('polyline').setAttribute('stroke', cfg.checkStroke);
  }

  // wrap bg
  iconWrap.className = 'status-icon-wrap' + (cfg.wrapClass ? ' ' + cfg.wrapClass : '');

  // text
  statusTitle.textContent = cfg.title;
  statusSub.textContent   = cfg.sub;

  // pill
  pillDot.className  = 'pill-dot' + (cfg.dot ? ' ' + cfg.dot : '');
  pillText.textContent = cfg.pill;
}

// ── Load settings into form ───────────────────────────────────────────────
function initials(email) {
  if (!email) return 'DD';
  const parts = email.split('@')[0].split(/[._-]/);
  return (parts[0][0] + (parts[1] ? parts[1][0] : parts[0][1] || '')).toUpperCase();
}

async function loadSettings() {
  const s = await api.getSettings();

  // Top bar
  accountEmail.textContent = s.email || 'Not signed in';
  avatar.textContent = initials(s.email);

  // Storage bar — degoo total is 2 TB = 2048 GB; used comes from email header
  // We don't have live quota data here, so show a placeholder derived from stored values
  const usedGb  = parseFloat(s.storageUsedGb  || 0);
  const totalGb = parseFloat(s.storageTotalGb || 2048);
  const pct = totalGb > 0 ? Math.min((usedGb / totalGb) * 100, 100) : 0;
  storageFill.style.width = pct + '%';
  storageLabel.textContent = usedGb > 0
    ? `${usedGb.toFixed(2)} GB of ${totalGb >= 1024 ? (totalGb/1024).toFixed(0)+'TB' : totalGb+'GB'}`
    : 'Storage usage unknown';

  // Form fields
  document.getElementById('email').value              = s.email              || '';
  document.getElementById('password').value           = s.password           || '';
  document.getElementById('mountpoint').value         = s.mountpoint         || '';
  document.getElementById('degooPath').value          = s.degooPath          || '/';
  document.getElementById('cacheSizeMb').value        = s.cacheSizeMb        || 128;
  document.getElementById('refreshIntervalMin').value = s.refreshIntervalMin || 10;
  document.getElementById('downloadThreads').value    = s.downloadThreads    || 8;
  document.getElementById('subchunkConnections').value= s.subchunkConnections|| 8;
  document.getElementById('lookaheadChunks').value    = s.lookaheadChunks    || 2;
  document.getElementById('chunkMaxAge').value        = s.chunkMaxAge        || 3600;
  document.getElementById('dbPath').value             = s.dbPath             || '';
  document.getElementById('chunkCacheDir').value      = s.chunkCacheDir      || '';
  tog.classList.toggle('on', !!s.startOnLaunch);
}

// ── Settings panel open/close ─────────────────────────────────────────────
function openSettings()  { settingsPanel.classList.add('open'); }
function closeSettings() { settingsPanel.classList.remove('open'); }

gearBtn.addEventListener('click', openSettings);
backBtn.addEventListener('click', closeSettings);
cancelBtn.addEventListener('click', closeSettings);

// ── Toggle ────────────────────────────────────────────────────────────────
togRow.addEventListener('click', () => tog.classList.toggle('on'));

// ── Browse ────────────────────────────────────────────────────────────────
browseBtn.addEventListener('click', async () => {
  const p = await api.browseFolder();
  if (p) document.getElementById('mountpoint').value = p;
});

// ── Save ─────────────────────────────────────────────────────────────────
saveBtn.addEventListener('click', async () => {
  const data = {
    email:               document.getElementById('email').value.trim(),
    password:            document.getElementById('password').value,
    mountpoint:          document.getElementById('mountpoint').value.trim(),
    degooPath:           document.getElementById('degooPath').value.trim() || '/',
    cacheSizeMb:         +document.getElementById('cacheSizeMb').value,
    refreshIntervalMin:  +document.getElementById('refreshIntervalMin').value,
    downloadThreads:     +document.getElementById('downloadThreads').value,
    subchunkConnections: +document.getElementById('subchunkConnections').value,
    lookaheadChunks:     +document.getElementById('lookaheadChunks').value,
    chunkMaxAge:         +document.getElementById('chunkMaxAge').value,
    dbPath:              document.getElementById('dbPath').value.trim(),
    chunkCacheDir:       document.getElementById('chunkCacheDir').value.trim(),
    startOnLaunch:       tog.classList.contains('on'),
  };
  await api.saveSettings(data);
  await loadSettings();
  okMsg.classList.add('show');
  setTimeout(() => { okMsg.classList.remove('show'); closeSettings(); }, 1400);
});

// ── Quick actions ─────────────────────────────────────────────────────────
folderBtn.addEventListener('click', async () => {
  const s = await api.getSettings();
  api.openFolder(s.mountpoint);
});
webBtn.addEventListener('click', () => {
  api.openExternal('https://app.degoo.com');
});

// ── Status listener ───────────────────────────────────────────────────────
if (api.onStatus) {
  api.onStatus((_, s) => applyStatus(s));
}

// ── Init ─────────────────────────────────────────────────────────────────
(async () => {
  await loadSettings();
  const s = await api.getStatus();
  applyStatus(s);
})();
