# Degoo Drive — Desktop App

Electron tray app for Linux providing **FUSE mount** and **two-way sync**
for Degoo cloud storage — inspired by
[Internxt Drive Linux](https://github.com/internxt/drive-desktop-linux).

## Architecture

```
┌─ Electron (GUI) ───────────────────────────────────────────┐
│  Tray → Mount tab  /  Sync tab  /  Settings             │
│  Main process spawns Python IPC bridge (stdio JSON)    │
└──────────────────┬───────────────────────────┘
                   │
        ┌─────────┴──────────────────┐
        │  python/degoo_sync/           │
        │  ipc_bridge.py               │  ← JSON stdin/stdout
        │    ├── SyncEngine            │  ← watchdog + poller
        │    │     └── SyncStateDB    │  ← sync_state.db
        │    └── DegooAPIAdapter       │  ← wraps fuse_degoo.py
        └──────────────────────────────┘
                   │
        ┌─────────┴──────────────────┐
        │  fuse_degoo.py (backend)      │
        │    └── TreeCache             │  ← tree_cache.db
        └──────────────────────────────┘
```

## Features

| Feature | Detail |
|---|---|
| **FUSE mount** | Browse Degoo as a virtual local filesystem |
| **Two-way sync** | Upload local changes, download remote changes |
| **Conflict handling** | Last-write-wins; `.conflict` copy preserved |
| **SQLite sync state** | `sync_state.db` persists across restarts |
| **SQLite tree cache** | `tree_cache.db` persists FUSE tree across mounts |
| **Live activity log** | Per-file sync events in the Sync tab |
| **Teal tray icon** | Green=running, grey=stopped, purple=error |
| **Tabbed UI** | Mount / Sync / Settings in one window |

## Install (AppImage)

```bash
chmod +x degoo-drive-*.AppImage
./degoo-drive-*.AppImage
```

Right-click tray → **Settings** → enter Degoo credentials → **Save**.

## Build locally

```bash
pip install watchdog          # for local file watching
cd electron && npm install
npm run build:all             # dist/*.AppImage + dist/*.deb
```

## Python dependencies

```
watchdog
```

Add to `requirements.txt` in the backend branch.
