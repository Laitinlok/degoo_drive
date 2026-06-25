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

let tray = null, mainWin = null, mountProc = null, status = 'stopped';
const LOG_FILE = path.join(app.getPath('userData'), 'degoo-drive.log');

// ── Resolve bundled Python runtime ────────────────────────────────────────
const RESOURCES = app.isPackaged ? process.resourcesPath : path.join(__dirname, '..', '..');
const PYTHON_DIR = path.join(RESOURCES, 'python');

// Detect bundled Python version from lib/pythonX.Y directory name.
// This never requires executing Python, so it always works.
function detectBundledPyVer() {
  const libDir = path.join(PYTHON_DIR, 'lib');
  if (!fs.existsSync(libDir)) return null;
  for (const e of fs.readdirSync(libDir)) {
    const m = e.match(/^python(\d+\.\d+)$/);
    if (m) return m[1];
  }
  return null;
}

const BUNDLED_PYVER = detectBundledPyVer();

// ── Resolve Python executable ──────────────────────────────────────────────
// Prefer the shell launcher (degoo_python.sh) which sets PYTHONHOME,
// PYTHONPATH, and LD_LIBRARY_PATH *before* exec'ing the interpreter.
// This is more reliable than relying on Node spawn env reaching the
// dynamic linker in time inside the AppImage squashfs mount.
const PYTHON_BIN = (() => {
  const launcher = path.join(PYTHON_DIR, 'bin', 'degoo_python.sh');
  if (fs.existsSync(launcher)) return launcher;
  // Fallback: bare binary (dev mode, system Python)
  const bare = path.join(PYTHON_DIR, 'bin', 'python3');
  if (fs.existsSync(bare)) return bare;
  return 'python3';
})();

const SCRIPT = (() => {
  const packed = path.join(RESOURCES, 'fuse_degoo.py');
  if (fs.existsSync(packed)) return packed;
  return path.join(__dirname, '..', '..', 'fuse_degoo.py');
})();

// ── Environment for Python subprocess ────────────────────────────────────
// The launcher script sets the critical vars itself, but we also pass them
// via env so the bare-binary fallback path works too.
function getPythonEnv() {
  const env = { ...process.env };
  if (BUNDLED_PYVER) {
    const libBase   = path.join(PYTHON_DIR, 'lib', `python${BUNDLED_PYVER}`);
    const sitePkgs  = path.join(libBase, 'site-packages');
    const sharedLib = path.join(PYTHON_DIR, 'shlib');

    env.PYTHONHOME = PYTHON_DIR;
    env.PYTHONPATH = sitePkgs + (process.env.PYTHONPATH ? ':' + process.env.PYTHONPATH : '');
    env.LD_LIBRARY_PATH = sharedLib + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
  }
  return env;
}

// ── Icon factory ──────────────────────────────────────────────────────────
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

// ── Main window ───────────────────────────────────────────────────────────
function createWindow() {
  if (mainWin) { mainWin.focus(); return; }
  mainWin = new BrowserWindow({
    width: 980,
    height: 640,
    minWidth: 780,
    minHeight: 520,
    resizable: true,
    title: 'Degoo Drive',
    // frame: false enables the custom HTML titlebar with -webkit-app-region:drag
    frame: false,
    transparent: false,
    backgroundColor: '#111113',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js')
    }
  });
  mainWin.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
  mainWin.on('closed', () => { mainWin = null; });
}

// ── Mount management ──────────────────────────────────────────────────────
function startMount() {
  if (mountProc) return;
  const s = store.store;
  if (!s.email || !s.password) { createWindow(); return; }
  fs.mkdirSync(s.mountpoint,            { recursive: true });
  fs.mkdirSync(path.dirname(s.dbPath),  { recursive: true });
  fs.mkdirSync(s.chunkCacheDir,         { recursive: true });

  const args = [
    SCRIPT,
    '--mountpoint',         s.mountpoint,
    '--degoo-email',        s.email,
    '--degoo-pass',         s.password,
    '--degoo-path',         s.degooPath,
    '--cache-size',         String(s.cacheSizeMb),
    '--refresh-interval',   String(s.refreshIntervalMin),
    '--download-threads',   String(s.downloadThreads),
    '--subchunk-connections', String(s.subchunkConnections),
    '--lookahead-chunks',   String(s.lookaheadChunks),
    '--db-path',            s.dbPath,
    '--chunk-cache-dir',    s.chunkCacheDir,
    '--chunk-max-age',      String(s.chunkMaxAge),
    '--allow-other',
  ];

  const log = fs.createWriteStream(LOG_FILE, { flags: 'a' });
  log.write(`\n[${new Date().toISOString()}] Starting: ${PYTHON_BIN} ${args.join(' ')}\n`);

  mountProc = cp.spawn(PYTHON_BIN, args, {
    detached: false,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: getPythonEnv(),
  });
  mountProc.stdout.pipe(log);
  mountProc.stderr.pipe(log);
  mountProc.on('spawn', () => setStatus('running'));
  mountProc.on('exit',  (code) => {
    mountProc = null;
    setStatus(code === 0 ? 'stopped' : 'error');
  });
}

function stopMount() {
  if (!mountProc) return;
  mountProc.kill('SIGTERM');
  setTimeout(() => { if (mountProc) mountProc.kill('SIGKILL'); }, 5000);
}

function setStatus(s) {
  status = s;
  updateTray();
  const msgs = { running: 'Mount started.', error: 'Mount failed — check logs.' };
  if (msgs[s] && Notification.isSupported())
    new Notification({ title: 'Degoo Drive', body: msgs[s] }).show();
  if (mainWin) mainWin.webContents.send('status', status);
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
    { label: '⚙  Open app',    click: createWindow },
    { label: '📋  View logs…',  click: () => shell.openPath(LOG_FILE) },
    { type: 'separator' },
    { label: '✕  Quit', click: () => { stopMount(); app.quit(); } },
  ]));
}

// ── IPC handlers ──────────────────────────────────────────────────────────
ipcMain.handle('get-settings',  ()     => store.store);
ipcMain.handle('get-status',    ()     => status);

ipcMain.handle('save-settings', (_, d) => {
  // Handle _action signals from renderer
  const action = d._action;
  delete d._action;

  // Only persist non-empty keys
  const toSave = Object.fromEntries(
    Object.entries(d).filter(([, v]) => v !== undefined)
  );
  store.set(toSave);

  if (action === 'stop') {
    stopMount();
  } else if (status === 'running') {
    // Settings changed while mounted — restart
    stopMount();
    setTimeout(startMount, 2000);
  } else if (toSave.email && toSave.password && store.get('startOnLaunch')) {
    startMount();
  }
  return true;
});

ipcMain.handle('browse-folder', async () => {
  const r = await dialog.showOpenDialog({ properties: ['openDirectory'] });
  return r.canceled ? null : r.filePaths[0];
});
ipcMain.handle('open-external', (_, url) => {
  if (url === 'logs') return shell.openPath(LOG_FILE);
  return shell.openExternal(url);
});
ipcMain.handle('open-folder',   (_, p) => shell.openPath(p));

// ── App lifecycle ─────────────────────────────────────────────────────────
app.whenReady().then(() => {
  app.setAppUserModelId('com.degoo.drive');
  tray = new Tray(ICONS.stopped());
  tray.setToolTip('Degoo Drive — stopped');
  tray.on('double-click', createWindow);
  updateTray();
  createWindow();
  if (store.get('startOnLaunch') && store.get('email')) setTimeout(startMount, 1200);
});
app.on('window-all-closed', e => e.preventDefault()); // keep in tray
app.on('before-quit', stopMount);
