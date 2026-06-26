#!/usr/bin/env python3
"""Degoo Drive GUI — system-tray background sync daemon.

Wraps fuse_degoo.py as a subprocess, exposes start/stop/status via a
system-tray icon, and shows a settings window for credentials & mount path.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer, QSize
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QAction
from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QFormLayout, QGroupBox,
    QMessageBox, QCheckBox, QFileDialog, QFrame,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_DIR  = Path.home() / ".config" / "degoo-drive-gui"
CONFIG_FILE = CONFIG_DIR / "settings.json"
LOG_FILE    = CONFIG_DIR / "degoo-drive.log"
SCRIPT_DIR  = Path(__file__).resolve().parent.parent   # repo root inside AppImage

DEFAULT_SETTINGS = {
    "email": "",
    "password": "",
    "mountpoint": str(Path.home() / "Degoo"),
    "degoo_path": "/",
    "cache_size_mb": 128,
    "refresh_interval_min": 10,
    "download_threads": 8,
    "subchunk_connections": 8,
    "lookahead_chunks": 2,
    "chunk_max_age": 3600,
    "start_on_launch": True,
    "allow_other": False,
    "db_path": str(Path.home() / ".cache" / "degoo_drive" / "tree_cache.db"),
    "chunk_cache_dir": str(Path.home() / ".cache" / "degoo_drive" / "chunks"),
}

# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            return {**DEFAULT_SETTINGS, **data}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(s: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ---------------------------------------------------------------------------
# Build tray icon programmatically (no external icon file needed)
# ---------------------------------------------------------------------------

def _make_icon(color: str, letter: str = "D") -> QIcon:
    px = QPixmap(64, 64)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("#ffffff"))
    font = QFont("Sans Serif", 26, QFont.Weight.Bold)
    p.setFont(font)
    p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, letter)
    p.end()
    return QIcon(px)


ICON_IDLE    = None
ICON_RUNNING = None
ICON_ERROR   = None


def init_icons():
    global ICON_IDLE, ICON_RUNNING, ICON_ERROR
    ICON_IDLE    = _make_icon("#6b7280")
    ICON_RUNNING = _make_icon("#01696f")
    ICON_ERROR   = _make_icon("#a12c7b", "!")


# ---------------------------------------------------------------------------
# Mount worker thread
# ---------------------------------------------------------------------------

class MountWorker(QThread):
    status_changed = pyqtSignal(str)
    log_line       = pyqtSignal(str)

    def __init__(self, settings: dict):
        super().__init__()
        self._settings = settings
        self._proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()

    def run(self):
        s = self._settings
        mountpoint = Path(s["mountpoint"])
        mountpoint.mkdir(parents=True, exist_ok=True)
        Path(s["db_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(s["chunk_cache_dir"]).mkdir(parents=True, exist_ok=True)

        script = SCRIPT_DIR / "fuse_degoo.py"
        if not script.exists():
            script = Path(sys.executable).parent / "fuse_degoo.py"

        cmd = [
            sys.executable, str(script),
            "--mountpoint",           str(mountpoint),
            "--degoo-email",          s["email"],
            "--degoo-pass",           s["password"],
            "--degoo-path",           s["degoo_path"],
            "--cache-size",           str(s["cache_size_mb"]),
            "--refresh-interval",     str(s["refresh_interval_min"]),
            "--download-threads",     str(s["download_threads"]),
            "--subchunk-connections", str(s["subchunk_connections"]),
            "--lookahead-chunks",     str(s["lookahead_chunks"]),
            "--db-path",              s["db_path"],
            "--chunk-cache-dir",      s["chunk_cache_dir"],
            "--chunk-max-age",        str(s["chunk_max_age"]),
        ]

        # Only pass --allow-other when the user has explicitly opted in.
        # FUSE aborts with a fatal error when allow_other is requested but
        # user_allow_other is absent from /etc/fuse3.conf, so this must
        # never be the default for a desktop AppImage.
        if s.get("allow_other", False):
            cmd.append("--allow-other")

        try:
            with open(LOG_FILE, "a") as log_f:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    preexec_fn=os.setsid,
                )
            self.status_changed.emit("running")
            self.log_line.emit(f"[degoo-gui] mount started (pid {self._proc.pid})")

            while not self._stop_event.is_set():
                ret = self._proc.poll()
                if ret is not None:
                    # Process died on its own (not via our stop() call).
                    # Exit codes from SIGTERM (-15) or SIGKILL (-9) are NOT
                    # errors from the user's perspective -- treat them as stopped.
                    self.log_line.emit(f"[degoo-gui] process exited with code {ret}")
                    natural_error = ret not in (0, -signal.SIGTERM, -signal.SIGKILL)
                    self.status_changed.emit("error" if natural_error else "stopped")
                    return
                time.sleep(1)

            # ── User requested stop ──────────────────────────────────────────
            # 1. Ask fuse_degoo to exit cleanly via SIGTERM to its process group.
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                self._proc.wait(timeout=8)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

            # 2. Force-unmount the FUSE mountpoint so the kernel releases the
            #    directory even if the process exited uncleanly (e.g. --allow-other
            #    was rejected and fuse_degoo never fully started the VFS).
            self._unmount(str(mountpoint))

            self.status_changed.emit("stopped")
            self.log_line.emit("[degoo-gui] mount stopped")

        except Exception as exc:
            self.log_line.emit(f"[degoo-gui] error: {exc}")
            # Attempt unmount even on unexpected errors so the mountpoint
            # does not remain in a "transport endpoint is not connected" state.
            self._unmount(s.get("mountpoint", ""))
            self.status_changed.emit("error")

    def _unmount(self, mountpoint: str) -> None:
        """Try fusermount3 -u then fusermount -u as fallback."""
        if not mountpoint:
            return
        for cmd in (["fusermount3", "-u", mountpoint],
                    ["fusermount",  "-u", mountpoint]):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=5)
                if result.returncode == 0:
                    self.log_line.emit(f"[degoo-gui] unmounted {mountpoint}")
                    return
            except FileNotFoundError:
                continue
            except Exception:
                continue
        # Last resort: umount (may need sudo, will fail silently if not available)
        try:
            subprocess.run(["umount", mountpoint], capture_output=True, timeout=5)
        except Exception:
            pass

    def stop(self):
        self._stop_event.set()


# ---------------------------------------------------------------------------
# Settings window
# ---------------------------------------------------------------------------

class SettingsWindow(QWidget):
    saved = pyqtSignal(dict)

    def __init__(self, settings: dict):
        super().__init__()
        self._s = dict(settings)
        self.setWindowTitle("Degoo Drive — Settings")
        self.setMinimumWidth(480)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowCloseButtonHint
        )
        self._build_ui()
        self._load_values()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)

        header = QLabel("Degoo Drive")
        header.setStyleSheet("font-size: 20px; font-weight: bold; color: #01696f;")
        root.addWidget(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #dcd9d5;")
        root.addWidget(sep)

        acc = QGroupBox("Account")
        acc_form = QFormLayout(acc)
        self._email    = QLineEdit(); self._email.setPlaceholderText("your@email.com")
        self._password = QLineEdit(); self._password.setEchoMode(QLineEdit.EchoMode.Password)
        acc_form.addRow("Email:",    self._email)
        acc_form.addRow("Password:", self._password)
        root.addWidget(acc)

        mnt = QGroupBox("Mount")
        mnt_form = QFormLayout(mnt)
        mp_row = QHBoxLayout()
        self._mountpoint = QLineEdit()
        mp_btn = QPushButton("Browse…")
        mp_btn.clicked.connect(self._browse_mountpoint)
        mp_row.addWidget(self._mountpoint)
        mp_row.addWidget(mp_btn)
        self._degoo_path = QLineEdit(); self._degoo_path.setPlaceholderText("/")
        mnt_form.addRow("Local folder:", mp_row)
        mnt_form.addRow("Degoo path:",   self._degoo_path)
        root.addWidget(mnt)

        perf = QGroupBox("Performance")
        perf_form = QFormLayout(perf)
        self._cache_size    = QSpinBox(); self._cache_size.setRange(32, 2048);    self._cache_size.setSuffix(" MB")
        self._refresh       = QSpinBox(); self._refresh.setRange(1, 1440);        self._refresh.setSuffix(" min")
        self._dl_threads    = QSpinBox(); self._dl_threads.setRange(1, 32)
        self._subchunk      = QSpinBox(); self._subchunk.setRange(1, 32)
        self._lookahead     = QSpinBox(); self._lookahead.setRange(1, 16)
        self._chunk_max_age = QSpinBox(); self._chunk_max_age.setRange(0, 86400); self._chunk_max_age.setSuffix(" sec")
        perf_form.addRow("Chunk size:",       self._cache_size)
        perf_form.addRow("Refresh interval:", self._refresh)
        perf_form.addRow("Download threads:", self._dl_threads)
        perf_form.addRow("Sub-chunk conn.:",  self._subchunk)
        perf_form.addRow("Lookahead chunks:", self._lookahead)
        perf_form.addRow("Chunk max age:",    self._chunk_max_age)
        root.addWidget(perf)

        self._start_on_launch = QCheckBox("Start mount automatically on launch")
        root.addWidget(self._start_on_launch)

        self._allow_other = QCheckBox(
            "Allow other users to access the mount  "
            "(requires user_allow_other in /etc/fuse3.conf)"
        )
        root.addWidget(self._allow_other)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel"); cancel_btn.clicked.connect(self.close)
        save_btn   = QPushButton("Save");   save_btn.clicked.connect(self._save)
        save_btn.setStyleSheet(
            "QPushButton { background: #01696f; color: white; border-radius: 4px; "
            "padding: 6px 20px; } QPushButton:hover { background: #0c4e54; }"
        )
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

    def _load_values(self):
        s = self._s
        self._email.setText(s.get("email", ""))
        self._password.setText(s.get("password", ""))
        self._mountpoint.setText(s.get("mountpoint", ""))
        self._degoo_path.setText(s.get("degoo_path", "/"))
        self._cache_size.setValue(s.get("cache_size_mb", 128))
        self._refresh.setValue(s.get("refresh_interval_min", 10))
        self._dl_threads.setValue(s.get("download_threads", 8))
        self._subchunk.setValue(s.get("subchunk_connections", 8))
        self._lookahead.setValue(s.get("lookahead_chunks", 2))
        self._chunk_max_age.setValue(s.get("chunk_max_age", 3600))
        self._start_on_launch.setChecked(s.get("start_on_launch", True))
        self._allow_other.setChecked(s.get("allow_other", False))

    def _browse_mountpoint(self):
        path = QFileDialog.getExistingDirectory(self, "Select mount folder",
                                                self._mountpoint.text())
        if path:
            self._mountpoint.setText(path)

    def _save(self):
        self._s.update({
            "email":                self._email.text().strip(),
            "password":             self._password.text(),
            "mountpoint":           self._mountpoint.text().strip(),
            "degoo_path":           self._degoo_path.text().strip() or "/",
            "cache_size_mb":        self._cache_size.value(),
            "refresh_interval_min": self._refresh.value(),
            "download_threads":     self._dl_threads.value(),
            "subchunk_connections": self._subchunk.value(),
            "lookahead_chunks":     self._lookahead.value(),
            "chunk_max_age":        self._chunk_max_age.value(),
            "start_on_launch":      self._start_on_launch.isChecked(),
            "allow_other":          self._allow_other.isChecked(),
        })
        save_settings(self._s)
        self.saved.emit(self._s)
        self.close()


# ---------------------------------------------------------------------------
# Main tray application
# ---------------------------------------------------------------------------

class DegooTrayApp:
    def __init__(self, app: QApplication):
        self._app = app
        self._settings = load_settings()
        self._worker: MountWorker | None = None
        self._status = "stopped"
        self._settings_win: SettingsWindow | None = None

        init_icons()

        self._tray = QSystemTrayIcon(ICON_IDLE)
        self._tray.setToolTip("Degoo Drive — stopped")
        self._build_menu()
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

        if self._settings.get("start_on_launch"):
            QTimer.singleShot(500, self.start_mount)

    def _build_menu(self):
        menu = QMenu()
        self._status_action = QAction("● Stopped")
        self._status_action.setEnabled(False)
        menu.addAction(self._status_action)
        menu.addSeparator()
        self._start_action = QAction("▶  Start mount")
        self._start_action.triggered.connect(self.start_mount)
        menu.addAction(self._start_action)
        self._stop_action = QAction("■  Stop mount")
        self._stop_action.setEnabled(False)
        self._stop_action.triggered.connect(self.stop_mount)
        menu.addAction(self._stop_action)
        menu.addSeparator()
        open_action = QAction("📂  Open folder")
        open_action.triggered.connect(self._open_folder)
        menu.addAction(open_action)
        settings_action = QAction("⚙  Settings…")
        settings_action.triggered.connect(self._show_settings)
        menu.addAction(settings_action)
        logs_action = QAction("📋  View logs…")
        logs_action.triggered.connect(self._show_logs)
        menu.addAction(logs_action)
        menu.addSeparator()
        quit_action = QAction("✕  Quit")
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._open_folder()

    def start_mount(self):
        if self._worker and self._worker.isRunning():
            return
        if not self._settings.get("email") or not self._settings.get("password"):
            self._tray.showMessage(
                "Degoo Drive",
                "Please configure your credentials in Settings first.",
                QSystemTrayIcon.MessageIcon.Warning, 3000,
            )
            self._show_settings()
            return
        self._worker = MountWorker(self._settings)
        self._worker.status_changed.connect(self._on_status_changed)
        self._worker.log_line.connect(self._on_log)
        self._worker.start()

    def stop_mount(self):
        if self._worker:
            self._worker.stop()
            self._worker.wait(5000)

    def _on_status_changed(self, status: str):
        self._status = status
        labels = {
            "running": ("● Running", ICON_RUNNING, "Degoo Drive — running"),
            "stopped": ("● Stopped", ICON_IDLE,    "Degoo Drive — stopped"),
            "error":   ("● Error",   ICON_ERROR,   "Degoo Drive — error"),
        }
        label, icon, tooltip = labels.get(status, labels["stopped"])
        self._status_action.setText(label)
        self._tray.setIcon(icon)
        self._tray.setToolTip(tooltip)
        self._start_action.setEnabled(status != "running")
        self._stop_action.setEnabled(status == "running")
        if status == "running":
            self._tray.showMessage("Degoo Drive", "Mount started successfully.",
                                   QSystemTrayIcon.MessageIcon.Information, 2000)
        elif status == "error":
            self._tray.showMessage(
                "Degoo Drive",
                "Mount failed — check logs.\nClick ▶ Start mount to retry.",
                QSystemTrayIcon.MessageIcon.Critical, 5000,
            )

    def _on_log(self, line: str):
        pass

    def _open_folder(self):
        mp = self._settings.get("mountpoint", "")
        if mp:
            subprocess.Popen(["xdg-open", mp])

    def _show_settings(self):
        if self._settings_win and self._settings_win.isVisible():
            self._settings_win.raise_()
            return
        self._settings_win = SettingsWindow(self._settings)
        self._settings_win.saved.connect(self._on_settings_saved)
        self._settings_win.show()

    def _on_settings_saved(self, new_settings: dict):
        self._settings = new_settings
        was_running = self._status == "running"
        if was_running:
            self.stop_mount()
            QTimer.singleShot(2000, self.start_mount)

    def _show_logs(self):
        if LOG_FILE.exists():
            subprocess.Popen(["xdg-open", str(LOG_FILE)])
        else:
            QMessageBox.information(None, "Logs", "No log file found yet.")

    def _quit(self):
        self.stop_mount()
        self._tray.hide()
        self._app.quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Degoo Drive")
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "Degoo Drive", "No system tray detected. Cannot run.")
        sys.exit(1)
    tray_app = DegooTrayApp(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
