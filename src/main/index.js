const {
  app, BrowserWindow, Tray, Menu, nativeImage,
  ipcMain, shell, dialog, Notification
} = require('electron');
const path  = require('path');
const fs    = require('fs');
const cp    = require('child_process');
const Store = require('electron-store');

const store = new Store({
  defaults: {
    email: '', password: '',
    mountpoint: path.join(app.getPath('home'), 'Degoo'),
    degooPath: '/',
    cacheSizeMb: 128, refreshIntervalMin: 10,
    downloadThreads: 8, subchunkConnections: 8,
    lookaheadChunks: 2, chunkMaxAge: 3600,
    startOnLaunch: true,
    dbPath: path.join(app.getPath('home'), '.cache', 'degoo_drive', 'tree_cache.db'),
    chunkCacheDir: path.join(app.getPath('home'), '.cache', 'degoo_drive', 'chunks'),
    storageUsedGb: 0, storageTotalGb: 2048,
  }
});

let tray = null, settingsWin = null, mountProc = null, status = 'stopped';
const LOG_FILE = path.join(app.getPath('userData'), 'degoo-drive.log');

// ── Resolve bundled Python runtime ──────────────────────────────────────────
const RESOURCES = app.isPackaged ? process.resourcesPath : path.join(__dirname, '..', '..');
const PYTHON_DIR = path.join(RESOURCES, 'python');

// Detect bundled Python version from directory name (python/lib/pythonX.Y)
// without needing to execute Python first.
function detectBundledPyVer() {
  const libDir = path.join(PYTHON_DIR, 'lib');
  if (!fs.existsSync(libDir)) return null;
  const entries = fs.readdirSync(libDir);
  for (const e of entries) {
    const m = e.match(/^python(\d+\.\d+)$/);
    if (m) return m[1];
  }
  return null;
}

const BUNDLED_PYVER = detectBundledPyVer();
const PYTHON_BIN = (() => {
  const bundled = path.join(PYTHON_DIR, 'bin', 'python3');
  if (fs.existsSync(bundled)) return bundled;
  return 'python3'; // dev fallback
})();
const SCRIPT = (() => {
  const packed = path.join(RESOURCES, 'fuse_degoo.py');
  if (fs.existsSync(packed)) return packed;
  return path.join(__dirname, '..', '..', 'fuse_degoo.py');
})();

// ── Build Python environment ──────────────────────────────────────────────
//
// CPython looks for stdlib + lib-dynload relative to PYTHONHOME:
//   $PYTHONHOME/lib/pythonX.Y/           <- pure-Python stdlib
//   $PYTHONHOME/lib/pythonX.Y/lib-dynload/ <- C extensions (_sqlite3, etc.)
//
// Our bundle layout (under resources/python/) mirrors this exactly:
//   python/
//     bin/python3
//     lib/
//       pythonX.Y/
//         site-packages/   <- pip --target output (pyfuse3, trio, requests …)
//         lib-dynload/     <- _sqlite3.so, _ssl.so, _hashlib.so …
//     stdlib/              <- copy of /usr/lib/pythonX.Y (pure-Python stdlib)
//     lib/                 <- libfuse3.so.3, libpython3.X.so, libssl.so …
//
// We set:
//   PYTHONHOME  = PYTHON_DIR          so CPython finds stdlib + lib-dynload
//   PYTHONPATH  = site-packages        so 'import pyfuse3' etc. work
//   LD_LIBRARY_PATH = python/lib       so dlopen finds libfuse3 + libpython
//
function getPythonEnv() {
  const env = { ...process.env };

  if (BUNDLED_PYVER) {
    const libBase     = path.join(PYTHON_DIR, 'lib', `python${BUNDLED_PYVER}`);
    const sitePkgs    = path.join(libBase, 'site-packages');
    const stdlib      = path.join(PYTHON_DIR, 'stdlib');
    const sharedLibs  = path.join(PYTHON_DIR, 'lib');

    // PYTHONHOME tells CPython where to find its standard library.
    // Without this, even basic imports like 'import os' fail inside AppImage.
    env.PYTHONHOME = PYTHON_DIR;

    // PYTHONPATH: site-packages (pip deps) + stdlib fallback
    const pyPaths = [sitePkgs];
    if (fs.existsSync(stdlib)) pyPaths.push(stdlib);
    env.PYTHONPATH = pyPaths.join(':') +
      (process.env.PYTHONPATH ? ':' + process.env.PYTHONPATH : '');

    // LD_LIBRARY_PATH: libfuse3.so.3, libpython3.x.so, libssl.so
    env.LD_LIBRARY_PATH = sharedLibs +
      (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
  }

  return env;
}

function makeIcon(color, letter = 'D') {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">
    <circle cx="16" cy="16" r="14" fill="${color}"/>
    <text x="16" y="22" font-family="sans-serif" font-size="16" font-weight="bold"
          fill="white" text-anchor="middle">${letter}</text></svg>`;
  return nativeImage.createFromDataURL('data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64'));
}
const ICONS = {
  stopped: () => makeIcon('#6b7280'),
  running: () => makeIcon('#01696f'),
  error:   () => makeIcon('#a12c7b', '!')
};

function openSettings() {
  if (settingsWin) { settingsWin.focus(); return; }
  settingsWin = new BrowserWindow({
    width: 320, height: 560, resizable: false, title: 'Degoo Drive',
    frame: false, transparent: false,
    webPreferences: {
      nodeIntegration: false, contextIsolation: true,
      preload: path.join(__dirname, 'preload.js')
    }
  });
  settingsWin.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
  settingsWin.on('closed', () => { settingsWin = null; });
}

function startMount() {
  if (mountProc) return;
  const s = store.store;
  if (!s.email || !s.password) { openSettings(); return; }
  fs.mkdirSync(s.mountpoint, { recursive: true });
  fs.mkdirSync(path.dirname(s.dbPath), { recursive: true });
  fs.mkdirSync(s.chunkCacheDir, { recursive: true });
  const args = [
    SCRIPT,
    '--mountpoint', s.mountpoint, '--degoo-email', s.email, '--degoo-pass', s.password,
    '--degoo-path', s.degooPath, '--cache-size', String(s.cacheSizeMb),
    '--refresh-interval', String(s.refreshIntervalMin),
    '--download-threads', String(s.downloadThreads),
    '--subchunk-connections', String(s.subchunkConnections),
    '--lookahead-chunks', String(s.lookaheadChunks),
    '--db-path', s.dbPath, '--chunk-cache-dir', s.chunkCacheDir,
    '--chunk-max-age', String(s.chunkMaxAge), '--allow-other',
  ];
  const log = fs.createWriteStream(LOG_FILE, { flags: 'a' });
  mountProc = cp.spawn(PYTHON_BIN, args, {
    detached: false,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: getPythonEnv(),
  });
  mountProc.stdout.pipe(log);
  mountProc.stderr.pipe(log);
  mountProc.on('spawn', () => setStatus('running'));
  mountProc.on('exit', (code) => { mountProc = null; setStatus(code === 0 ? 'stopped' : 'error'); });
}

function stopMount() {
  if (!mountProc) return;
  mountProc.kill('SIGTERM');
  setTimeout(() => { if (mountProc) mountProc.kill('SIGKILL'); }, 5000);
}

function setStatus(s) {
  status = s;
  updateTray();
  const msgs = { running: 'Mount started successfully.', error: 'Mount failed — check logs.' };
  if (msgs[s] && Notification.isSupported()) new Notification({ title: 'Degoo Drive', body: msgs[s] }).show();
  if (settingsWin) settingsWin.webContents.send('status', status);
}

function updateTray() {
  if (!tray) return;
  tray.setImage(ICONS[status]());
  tray.setToolTip(`Degoo Drive — ${status}`);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: `● ${status.charAt(0).toUpperCase() + status.slice(1)}`, enabled: false },
    { type: 'separator' },
    { label: '▶  Start mount',  enabled: status !== 'running', click: startMount },
    { label: '■  Stop mount',   enabled: status === 'running', click: stopMount },
    { type: 'separator' },
    { label: '📂  Open folder', click: () => shell.openPath(store.get('mountpoint')) },
    { label: '⚙  Settings…',   click: openSettings },
    { label: '📋  View logs…',  click: () => shell.openPath(LOG_FILE) },
    { type: 'separator' },
    { label: '✕  Quit', click: () => { stopMount(); app.quit(); } },
  ]));
}

ipcMain.handle('get-settings',  ()     => store.store);
ipcMain.handle('save-settings', (_, d) => {
  store.set(d);
  if (status === 'running') { stopMount(); setTimeout(startMount, 2000); }
  return true;
});
ipcMain.handle('get-status',    ()     => status);
ipcMain.handle('browse-folder', async () => {
  const r = await dialog.showOpenDialog({ properties: ['openDirectory'] });
  return r.canceled ? null : r.filePaths[0];
});
ipcMain.handle('open-external', (_, url)  => shell.openExternal(url));
ipcMain.handle('open-folder',   (_, p)    => shell.openPath(p));

app.whenReady().then(() => {
  app.setAppUserModelId('com.degoo.drive');
  tray = new Tray(ICONS.stopped());
  tray.setToolTip('Degoo Drive — stopped');
  tray.on('double-click', () => {
    if (settingsWin) settingsWin.focus();
    else openSettings();
  });
  updateTray();
  if (store.get('startOnLaunch') && store.get('email')) setTimeout(startMount, 1000);
});
app.on('window-all-closed', (e) => e.preventDefault());
app.on('before-quit', stopMount);
