const {
  app, BrowserWindow, Tray, Menu, nativeImage,
  ipcMain, shell, dialog, Notification, safeStorage
} = require('electron');
const path  = require('path');
const fs    = require('fs');
const cp    = require('child_process');
const Store = require('electron-store');

const store = new Store({
  defaults: {
    email: '', password: '', passwordEncrypted: '',
    mountpoint: path.join(app.getPath('home'), 'Degoo'),
    degooPath: '/',
    cacheSizeMb: 128, refreshIntervalMin: 10,
    downloadThreads: 8, subchunkConnections: 8,
    lookaheadChunks: 2, chunkMaxAge: 3600,
    startOnLaunch: true,
    allowOther: false,
    dbPath: path.join(app.getPath('home'), '.cache', 'degoo_drive', 'tree_cache.db'),
    chunkCacheDir: path.join(app.getPath('home'), '.cache', 'degoo_drive', 'chunks'),
    storageUsedGb: 0, storageTotalGb: 2048,
  }
});

let tray = null, mainWin = null, mountProc = null, status = 'stopped';
let userStopped = false;  // true when stop was explicitly requested by the user
const LOG_FILE = path.join(app.getPath('userData'), 'degoo-drive.log');
const ALLOWED_EXTERNAL_URLS = new Set([
  'https://app.degoo.com/',
  'https://degoo.com/',
  'https://github.com/Laitinlok/degoo_drive',
]);

function encryptPassword(password) {
  if (!password) return '';
  if (!safeStorage.isEncryptionAvailable()) return '';
  return safeStorage.encryptString(password).toString('base64');
}

function decryptPassword(ciphertext) {
  if (!ciphertext || !safeStorage.isEncryptionAvailable()) return '';
  try {
    return safeStorage.decryptString(Buffer.from(ciphertext, 'base64'));
  } catch (e) {
    return '';
  }
}

function getStoredSettings() {
  const s = { ...store.store };
  s.password = decryptPassword(s.passwordEncrypted) || s.password || '';
  return s;
}

function prepareSettingsForSave(data) {
  const next = { ...data };
  if (Object.prototype.hasOwnProperty.call(next, 'password')) {
    const encrypted = encryptPassword(next.password);
    if (encrypted) {
      next.passwordEncrypted = encrypted;
      next.password = '';
    }
  }
  return next;
}

function redactArgs(args) {
  const redacted = [];
  for (let i = 0; i < args.length; i += 1) {
    redacted.push(args[i]);
    if (args[i] === '--degoo-pass' && i + 1 < args.length) {
      redacted.push('<redacted>');
      i += 1;
    }
  }
  return redacted;
}

function isPathInside(childPath, parentPath) {
  if (!childPath || !parentPath) return false;
  const child = path.resolve(childPath);
  const parent = path.resolve(parentPath);
  return child === parent || child.startsWith(parent + path.sep);
}

function openAllowedExternal(url) {
  if (url === 'logs') return shell.openPath(LOG_FILE);
  let normalized;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:') return Promise.resolve('Blocked non-HTTPS URL');
    normalized = parsed.toString();
  } catch (e) {
    return Promise.resolve('Blocked invalid URL');
  }
  if (!ALLOWED_EXTERNAL_URLS.has(normalized)) return Promise.resolve('Blocked unapproved URL');
  return shell.openExternal(normalized);
}

function openAllowedFolder(folderPath) {
  const mountpoint = store.get('mountpoint');
  if (!isPathInside(folderPath, mountpoint)) return Promise.resolve('Blocked unapproved path');
  return shell.openPath(folderPath);
}

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
const PYTHON_BIN = (() => {
  const launcher = path.join(PYTHON_DIR, 'bin', 'degoo_python.sh');
  if (fs.existsSync(launcher)) return launcher;
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

// ── Unmount helper ────────────────────────────────────────────────────────
// Uses lazy unmount (-uz / -l) so a stale/dead FUSE transport is always
// detached, preventing the mountpoint from being left as a blank folder.
function unmount(mountpoint) {
  const log = (msg) => {
    fs.appendFileSync(LOG_FILE, `[${new Date().toISOString()}] ${msg}\n`);
  };

  // Try fusermount3 -uz then fusermount -uz (lazy detach)
  for (const bin of ['fusermount3', 'fusermount']) {
    try {
      const r = cp.spawnSync(bin, ['-uz', mountpoint], { timeout: 5000 });
      if (r.status === 0) {
        log(`unmounted ${mountpoint} via ${bin} -uz`);
        return;
      }
      const err = (r.stderr || Buffer.alloc(0)).toString().trim();
      if (err) log(`${bin} -uz failed (${r.status}): ${err}`);
    } catch (e) {
      // binary not found or timed out — try next
    }
  }

  // Last resort: umount -l (lazy, no sudo needed for user-owned FUSE mounts)
  try {
    const r = cp.spawnSync('umount', ['-l', mountpoint], { timeout: 5000 });
    if (r.status === 0) {
      log(`unmounted ${mountpoint} via umount -l`);
    } else {
      const err = (r.stderr || Buffer.alloc(0)).toString().trim();
      log(`umount -l failed (${r.status}): ${err}`);
    }
  } catch (e) {
    log(`umount -l error: ${e.message}`);
  }
}

// ── Mount management ──────────────────────────────────────────────────────
function startMount() {
  if (mountProc) return;
  const s = getStoredSettings();
  if (!s.email || !s.password) { createWindow(); return; }
  fs.mkdirSync(s.mountpoint,            { recursive: true });
  fs.mkdirSync(path.dirname(s.dbPath),  { recursive: true });
  fs.mkdirSync(s.chunkCacheDir,         { recursive: true });

  const args = [
    SCRIPT,
    '--mountpoint',           s.mountpoint,
    '--degoo-path',           s.degooPath,
    '--cache-size',           String(s.cacheSizeMb),
    '--refresh-interval',     String(s.refreshIntervalMin),
    '--download-threads',     String(s.downloadThreads),
    '--subchunk-connections', String(s.subchunkConnections),
    '--lookahead-chunks',     String(s.lookaheadChunks),
    '--db-path',              s.dbPath,
    '--chunk-cache-dir',      s.chunkCacheDir,
    '--chunk-max-age',        String(s.chunkMaxAge),
  ];

  // Only pass --allow-other when the user has explicitly opted in.
  // FUSE aborts with a fatal error when allow_other is requested but
  // user_allow_other is absent from /etc/fuse.conf / /etc/fuse3.conf.
  if (s.allowOther) {
    args.push('--allow-other');
  }

  const log = fs.createWriteStream(LOG_FILE, { flags: 'a' });
  const env = {
    ...getPythonEnv(),
    DEGOO_EMAIL: s.email,
    DEGOO_PASSWORD: s.password,
  };
  log.write(`\n[${new Date().toISOString()}] Starting: ${PYTHON_BIN} ${redactArgs(args).join(' ')}\n`);

  userStopped = false;
  mountProc = cp.spawn(PYTHON_BIN, args, {
    detached: false,
    stdio: ['ignore', 'pipe', 'pipe'],
    env,
  });
  mountProc.stdout.pipe(log);
  mountProc.stderr.pipe(log);
  mountProc.on('spawn', () => setStatus('running'));
  mountProc.on('exit', (code) => {
    mountProc = null;
    // If the user explicitly requested stop, always treat as stopped
    // regardless of exit code (SIGTERM = -15, SIGKILL = -9, or any other).
    if (userStopped) {
      setStatus('stopped');
    } else {
      setStatus(code === 0 ? 'stopped' : 'error');
    }
  });
}

function stopMount() {
  if (!mountProc) return;
  userStopped = true;
  const mountpoint = store.get('mountpoint');
  mountProc.kill('SIGTERM');
  // Force-unmount after a short delay so the kernel releases the mountpoint
  // even if the process exits uncleanly (stale transport endpoint).
  setTimeout(() => {
    if (mountProc) mountProc.kill('SIGKILL');
    unmount(mountpoint);
  }, 3000);
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
ipcMain.handle('get-settings',  ()     => getStoredSettings());
ipcMain.handle('get-status',    ()     => status);

ipcMain.handle('save-settings', (_, d) => {
  const action = d._action;
  delete d._action;
  const toSave = prepareSettingsForSave(Object.fromEntries(
    Object.entries(d).filter(([, v]) => v !== undefined)
  ));
  store.set(toSave);
  if (action === 'stop') {
    stopMount();
  } else if (status === 'running') {
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
ipcMain.handle('open-external', (_, url) => openAllowedExternal(url));
ipcMain.handle('open-folder',   (_, p) => openAllowedFolder(p));

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
app.on('window-all-closed', e => e.preventDefault());
app.on('before-quit', stopMount);
