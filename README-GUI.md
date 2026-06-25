# Degoo Drive — Electron GUI

System-tray desktop app for Linux that manages your Degoo FUSE mount.

## Install

```bash
chmod +x degoo-drive-*.AppImage && ./degoo-drive-*.AppImage
# or
sudo dpkg -i degoo-drive-*.deb
```

Right-click the tray icon → **Settings**, enter credentials, click **Save**.

## Build locally

```bash
npm install
npm run build        # AppImage only
npm run build:all    # AppImage + .deb
```

Output lands in `dist/`.

## Architecture

| File | Role |
|---|---|
| `src/main/index.js` | Electron main process: tray, IPC, child-process management |
| `src/main/preload.js` | Context bridge — exposes safe IPC to renderer |
| `src/renderer/index.html` | Settings window markup |
| `src/renderer/app.js` | Settings window logic |
| `fuse_degoo.py` | FUSE backend (from `feat/cligoo-backend`) |
