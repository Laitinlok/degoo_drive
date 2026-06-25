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
  }
});

let tray = null, settingsWin = null, mountProc = null, status = 'stopped';
const LOG_FILE = path.join(app.getPath('userData'), 'degoo-drive.log');

const SCRIPT = (() => {
  const packed = path.join(process.resourcesPath, 'fuse_degoo.py');
  if (fs.existsSync(packed)) return packed;
  return path.join(__dirname, '..', '..', 'fuse_degoo.py');
})();

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
    width: 520, height: 700, resizable: false, title: 'Degoo Drive — Settings',
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
  mountProc = cp.spawn('python3', args, { detached: false, stdio: ['ignore', 'pipe', 'pipe'] });
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

app.whenReady().then(() => {
  app.setAppUserModelId('com.degoo.drive');
  tray = new Tray(ICONS.stopped());
  tray.setToolTip('Degoo Drive — stopped');
  tray.on('double-click', () => shell.openPath(store.get('mountpoint')));
  updateTray();
  if (store.get('startOnLaunch') && store.get('email')) setTimeout(startMount, 1000);
});
app.on('window-all-closed', (e) => e.preventDefault());
app.on('before-quit', stopMount);
