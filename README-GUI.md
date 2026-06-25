# Degoo Drive GUI

A Linux system-tray application that runs `fuse_degoo.py` in the background
and exposes start / stop / settings through a tray icon.

## Features

- **System tray icon** — green when mounted, grey when stopped, magenta on error
- **Settings window** — credentials, mount folder, performance tuning
- **Auto-start** — optionally starts the mount on launch
- **Log viewer** — opens the log file in your default text editor
- **AppImage distribution** — single executable, no install needed

## Quick start (from AppImage)

```bash
chmod +x degoo-drive-gui-linux-x86_64.AppImage
./degoo-drive-gui-linux-x86_64.AppImage
```

Right-click the tray icon → **Settings** → enter your Degoo email/password
and choose a local folder → **Save** → the mount starts automatically.

## Build locally

```bash
pip install python-appimage PyQt6
python -m python_appimage build app appimage-recipe/
```

## Branch layout

| Branch | Purpose |
|---|---|
| `feat/cligoo-backend` | FUSE backend + SQLite cache |
| `feat/gui` | This GUI layer (depends on the backend branch) |

## Requirements

- Linux x86_64
- FUSE3 (`fuse3` package on Debian/Fedora)
- `--allow-other` requires `/etc/fuse.conf` → `user_allow_other`
