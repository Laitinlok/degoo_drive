/**
 * Degoo Drive — Electron main process
 *
 * Manages two sub-systems:
 *   1. FUSE mount  — via ipc_bridge start_mount command
 *   2. Sync engine — via ipc_bridge start_sync command
 *
 * Python child process uses newline-delimited JSON stdio protocol.
 */

const {
  app, BrowserWindow, Tray, Menu, nativeImage,
  ipcMain, shell, dialog, Notification,
} = require('electron');
const path  = require('path');
const fs    = require('fs');
const cp    = require('child_process');
const Store = require('electron-store');

// ── Config ───────────────────────────────────────────────────────────────────
const store = new Store({
  defaults: {
    email: '', password: '',
    mountEnabled:        true,
    mountpoint:          path.join(app.getPath('home'), 'Degoo'),
    syncEnabled:         false,
    syncDir:             path.join(app.getPath('home'), 'Degoo-Sync'),
    degooPath:           '/',
    cacheSizeMb:         128,
    refreshIntervalMin:  10,
    downloadThreads:     8,
    subchunkConnections: 8,
    lookaheadChunks:     2,
    chunkMaxAge:         3600,
    pollSecs:            60,
    startOnLaunch:       true,
    dbPath:      path.join(app.getPath('home'), '.cache', 'degoo_drive', 'tree_cache.db'),
    stateDb:     path.join(app.getPath('home'), '.cache', 'degoo_drive', 'sync_state.db'),
    chunkCacheDir: path.join(app.getPath('home'), '.cache', 'degoo_drive', 'chunks'),
  },
});

// ── Resource paths ────────────────────────────────────────────────────────────
const RESOURCES = process.resourcesPath || path.join(__dirname, '..', '..', '..');
const FUSE_PY   = (() => {
  const packed = path.join(RESOURCES, 'fuse_degoo.py');
  if (fs.existsSync(packed)) return packed;
  return path.join(__dirname, '..', '..', '..', 'fuse_degoo.py');
})();
const BRIDGE_DIR = (() => {
  const packed = path.join(RESOURCES, 'degoo_sync');
  if (fs.existsSync(packed)) return path.dirname(packed);
  return path.join(__dirname, '..', '..', '..', 'python');
})();
const LOG_FILE = path.join(app.getPath('userData'), 'degoo-drive.log');

// ── State ─────────────────────────────────────────────────────────────────────
let tray        = null;
let settingsWin = null;
let bridgeProc  = null;
let syncStatus  = 'stopped';
let mountStatus = 'stopped';

// ── IPC bridge ────────────────────────────────────────────────────────────────
function startBridge() {
  if (bridgeProc) return;
  const logStream = fs.createWriteStream(LOG_FILE, { flags: 'a' });
  bridgeProc = cp.spawn('python3', ['-m', 'degoo_sync.ipc_bridge'], {
    cwd:  BRIDGE_DIR,
    stdio: ['pipe', 'pipe', 'pipe'],
    env:  { ...process.env, PYTHONPATH: BRIDGE_DIR },
  });
  bridgeProc.stderr.pipe(logStream);
  let buf = '';
  bridgeProc.stdout.on('data', chunk => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf('\n')) !== -1) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) { try { handleBridgeEvent(JSON.parse(line)); } catch (_) {} }
    }
  });
  bridgeProc.on('exit', () => { bridgeProc = null; });
}

function sendBridge(obj) {
  if (!bridgeProc) startBridge();
  bridgeProc.stdin.write(JSON.stringify(obj) + '\n');
}

function handleBridgeEvent(msg) {
  if (msg.event === 'status') {
    if (msg.sync)  syncStatus  = msg.sync;
    if (msg.mount) mountStatus = msg.mount;
    updateTray();
    settingsWin?.webContents.send('status', { sync: syncStatus, mount: mountStatus });
  } else if (msg.event === 'sync_progress') {
    settingsWin?.webContents.send('sync_progress', msg);
  } else if (msg.event === 'error') {
    notify('Degoo Drive Error', msg.message);
  }
}

// ── Mount / Sync helpers ──────────────────────────────────────────────────────
function buildArgs() {
  const s = store.store;
  return {
    fuse_path: FUSE_PY, email: s.email, password: s.password,
    degooPath: s.degooPath, mountpoint: s.mountpoint, syncDir: s.syncDir,
    cacheSizeMb: s.cacheSizeMb, refreshIntervalMin: s.refreshIntervalMin,
    downloadThreads: s.downloadThreads, subchunkConnections: s.subchunkConnections,
    lookaheadChunks: s.lookaheadChunks, chunkMaxAge: s.chunkMaxAge,
    pollSecs: s.pollSecs, dbPath: s.dbPath, stateDb: s.stateDb,
    chunkCacheDir: s.chunkCacheDir,
  };
}

function startMount() {
  const s = store.store;
  if (!s.email) { openSettings(); return; }
  fs.mkdirSync(s.mountpoint, { recursive: true });
  sendBridge({ cmd: 'start_mount', args: buildArgs() });
}
function stopMount()  { sendBridge({ cmd: 'stop_mount' }); }
function startSync()  {
  const s = store.store;
  if (!s.email) { openSettings(); return; }
  fs.mkdirSync(s.syncDir, { recursive: true });
  sendBridge({ cmd: 'start_sync', args: buildArgs() });
}
function stopSync()   { sendBridge({ cmd: 'stop_sync' }); }

// ── Settings window ───────────────────────────────────────────────────────────
function openSettings() {
  if (settingsWin) { settingsWin.focus(); return; }
  settingsWin = new BrowserWindow({
    width: 560, height: 780, resizable: false,
    title: 'Degoo Drive — Settings',
    webPreferences: {
      nodeIntegration: false, contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  settingsWin.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
  settingsWin.on('closed', () => { settingsWin = null; });
}

// ── Tray ──────────────────────────────────────────────────────────────────────
function makeIcon(syncSt, mountSt) {
  const color = (syncSt==='running'||mountSt==='running') ? '#01696f'
               :(syncSt==='error'  ||mountSt==='error')   ? '#a12c7b' : '#6b7280';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">
    <circle cx="16" cy="16" r="14" fill="${color}"/>
    <text x="16" y="22" font-family="sans-serif" font-size="16"
          font-weight="bold" fill="white" text-anchor="middle">D</text></svg>`;
  return nativeImage.createFromDataURL(
    'data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64'));
}

function cap(v='') { return v.charAt(0).toUpperCase() + v.slice(1); }

function updateTray() {
  if (!tray) return;
  tray.setImage(makeIcon(syncStatus, mountStatus));
  tray.setToolTip(`Degoo Drive  Mount:${cap(mountStatus)}  Sync:${cap(syncStatus)}`);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: `Mount: ${cap(mountStatus)}`, enabled: false },
    { label: `Sync:  ${cap(syncStatus)}`,  enabled: false },
    { type: 'separator' },
    { label: '▶ Start mount', enabled: mountStatus!=='running', click: startMount },
    { label: '■ Stop mount',  enabled: mountStatus==='running', click: stopMount  },
    { type: 'separator' },
    { label: '⇄ Start sync',  enabled: syncStatus!=='running',  click: startSync  },
    { label: '■ Stop sync',   enabled: syncStatus==='running',  click: stopSync   },
    { type: 'separator' },
    { label: '📂 Open mount folder', click: () => shell.openPath(store.get('mountpoint')) },
    { label: '📂 Open sync folder',  click: () => shell.openPath(store.get('syncDir'))    },
    { label: '⚙  Settings…',         click: openSettings },
    { label: '📋 View logs…',         click: () => shell.openPath(LOG_FILE) },
    { type: 'separator' },
    { label: '✕ Quit', click: () => {
        stopMount(); stopSync();
        setTimeout(() => app.quit(), 1500);
    }},
  ]));
}

function notify(title, body) {
  if (Notification.isSupported()) new Notification({ title, body }).show();
}

// ── IPC to renderer ───────────────────────────────────────────────────────────
ipcMain.handle('get-settings',  () => store.store);
ipcMain.handle('save-settings', (_, d) => { store.set(d); return true; });
ipcMain.handle('get-status',    () => ({ sync: syncStatus, mount: mountStatus }));
ipcMain.handle('start-mount',   () => startMount());
ipcMain.handle('stop-mount',    () => stopMount());
ipcMain.handle('start-sync',    () => startSync());
ipcMain.handle('stop-sync',     () => stopSync());
ipcMain.handle('browse-folder', async () => {
  const r = await dialog.showOpenDialog({ properties: ['openDirectory'] });
  return r.canceled ? null : r.filePaths[0];
});

// ── App lifecycle ─────────────────────────────────────────────────────────────
app.whenReady().then(() => {
  app.setAppUserModelId('com.degoo.drive');
  tray = new Tray(makeIcon('stopped','stopped'));
  tray.on('double-click', () => shell.openPath(store.get('mountpoint')));
  updateTray();
  startBridge();
  const s = store.store;
  if (s.startOnLaunch && s.email) {
    if (s.mountEnabled) setTimeout(startMount, 1500);
    if (s.syncEnabled)  setTimeout(startSync,  2500);
  }
});

app.on('window-all-closed', e => e.preventDefault());
app.on('before-quit', () => {
  stopMount(); stopSync();
  bridgeProc?.stdin.write('{"cmd":"quit"}\n');
});
