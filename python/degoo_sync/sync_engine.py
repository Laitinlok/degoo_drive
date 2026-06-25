"""
degoo_sync/sync_engine.py
Two-way sync engine: local folder <-> Degoo cloud

Architecture (mirrors Internxt drive-desktop-linux):
  - Local watcher  : inotify via watchdog
  - Remote poller  : polls Degoo API on interval, uses tree_cache.db for delta
  - Conflict solver: last-write-wins; .conflict copy kept
  - State DB       : SQLite (sync_state.db)
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

log = logging.getLogger("degoo_sync")


class SyncStateDB:
    """SQLite-backed state tracking for synced items."""

    def __init__(self, db_path: str):
        self._local = threading.local()
        self._path  = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as cn:
            cn.executescript("""
                CREATE TABLE IF NOT EXISTS sync_items (
                    rel_path    TEXT PRIMARY KEY,
                    degoo_id    INTEGER,
                    local_mtime REAL,
                    local_hash  TEXT,
                    remote_hash TEXT,
                    status      TEXT DEFAULT 'synced'
                );
                CREATE INDEX IF NOT EXISTS idx_status ON sync_items(status);
                PRAGMA journal_mode=WAL;
            """)

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "cn", None):
            self._local.cn = sqlite3.connect(self._path, check_same_thread=False)
            self._local.cn.row_factory = sqlite3.Row
        return self._local.cn

    def upsert(self, rel_path, degoo_id, local_mtime, local_hash, remote_hash, status="synced"):
        self._conn().execute("""
            INSERT INTO sync_items(rel_path,degoo_id,local_mtime,local_hash,remote_hash,status)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(rel_path) DO UPDATE SET
              degoo_id=excluded.degoo_id, local_mtime=excluded.local_mtime,
              local_hash=excluded.local_hash, remote_hash=excluded.remote_hash,
              status=excluded.status
        """, (rel_path, degoo_id, local_mtime, local_hash, remote_hash, status))
        self._conn().commit()

    def get(self, rel_path):
        return self._conn().execute(
            "SELECT * FROM sync_items WHERE rel_path=?", (rel_path,)).fetchone()

    def delete(self, rel_path):
        self._conn().execute("DELETE FROM sync_items WHERE rel_path=?", (rel_path,))
        self._conn().commit()

    def all_items(self):
        return self._conn().execute("SELECT * FROM sync_items").fetchall()


class SyncEvent:
    def __init__(self, kind: str, rel_path: str, detail: str = ""):
        self.kind     = kind
        self.rel_path = rel_path
        self.detail   = detail


def _sha256(path: str, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(buf):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


class _LocalHandler(FileSystemEventHandler if HAS_WATCHDOG else object):
    def __init__(self, engine):
        self._eng = engine

    def on_created(self, event):
        if not event.is_directory:
            self._eng._queue_local_change(event.src_path, "upload")

    def on_modified(self, event):
        if not event.is_directory:
            self._eng._queue_local_change(event.src_path, "upload")

    def on_deleted(self, event):
        self._eng._queue_local_change(event.src_path, "delete_remote")

    def on_moved(self, event):
        self._eng._queue_local_change(event.dest_path, "upload")
        self._eng._queue_local_change(event.src_path,  "delete_remote")


class SyncEngine:
    """
    Coordinates two-way sync between a local folder and Degoo.

    Parameters
    ----------
    local_dir  : str   — local sync root
    degoo_api         — DegooAPIAdapter instance
    state_db   : str   — path to sync_state.db
    on_event   : callable(SyncEvent)
    poll_secs  : int   — remote poll interval
    """

    def __init__(self, local_dir, degoo_api, state_db,
                 on_event=None, poll_secs=60, conflict_suffix=".conflict"):
        self.local_dir       = Path(local_dir)
        self.api             = degoo_api
        self.state           = SyncStateDB(state_db)
        self.on_event        = on_event or (lambda e: None)
        self.poll_secs       = poll_secs
        self.conflict_suffix = conflict_suffix
        self._stop     = threading.Event()
        self._queue: list = []
        self._lock     = threading.Lock()
        self._observer = None
        self._threads  = []

    def start(self):
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        threading.Thread(target=self._full_reconcile, daemon=True).start()
        if HAS_WATCHDOG:
            self._observer = Observer()
            self._observer.schedule(_LocalHandler(self), str(self.local_dir), recursive=True)
            self._observer.start()
        for target in (self._poll_loop, self._worker_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("Sync engine started: %s", self.local_dir)

    def stop(self):
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        for t in self._threads:
            t.join(timeout=5)
        log.info("Sync engine stopped")

    def _queue_local_change(self, abs_path, kind):
        try:
            rel = str(Path(abs_path).relative_to(self.local_dir))
        except ValueError:
            return
        with self._lock:
            self._queue.append((rel, kind))

    def _poll_loop(self):
        while not self._stop.wait(self.poll_secs):
            try:
                self._remote_scan()
            except Exception as e:
                log.error("Remote scan error: %s", e)

    def _worker_loop(self):
        while not self._stop.is_set():
            with self._lock:
                tasks, self._queue = list(self._queue), []
            for rel, kind in tasks:
                self._handle(rel, kind)
            time.sleep(0.5)

    def _full_reconcile(self):
        log.info("Full reconcile started")
        try:
            remote = {r["rel_path"]: r for r in self.api.list_all()}
            for abs_path in self.local_dir.rglob("*"):
                if abs_path.is_dir():
                    continue
                rel        = str(abs_path.relative_to(self.local_dir))
                state      = self.state.get(rel)
                local_hash = _sha256(str(abs_path))
                if rel not in remote:
                    with self._lock: self._queue.append((rel, "upload"))
                elif state and state["local_hash"] != local_hash:
                    with self._lock: self._queue.append((rel, "upload"))
                elif state and state["remote_hash"] != remote[rel].get("hash", ""):
                    with self._lock: self._queue.append((rel, "download"))
            for rel in remote:
                if not (self.local_dir / rel).exists():
                    with self._lock: self._queue.append((rel, "download"))
        except Exception as e:
            log.error("Full reconcile failed: %s", e)

    def _remote_scan(self):
        remote = {r["rel_path"]: r for r in self.api.list_all()}
        for rel, r in remote.items():
            state = self.state.get(rel)
            if not state:
                with self._lock: self._queue.append((rel, "download"))
            elif state["remote_hash"] != r.get("hash", ""):
                local_abs  = self.local_dir / rel
                local_hash = _sha256(str(local_abs)) if local_abs.exists() else ""
                kind = "conflict" if local_hash and local_hash != state["local_hash"] else "download"
                with self._lock: self._queue.append((rel, kind))

    def _handle(self, rel, kind):
        abs_path = self.local_dir / rel
        try:
            if   kind == "upload":        self._do_upload(rel, abs_path)
            elif kind == "download":      self._do_download(rel, abs_path)
            elif kind == "delete_remote": self._do_delete_remote(rel)
            elif kind == "delete_local":  self._do_delete_local(rel, abs_path)
            elif kind == "conflict":      self._do_conflict(rel, abs_path)
        except Exception as e:
            log.error("[%s] %s: %s", kind, rel, e)
            self.on_event(SyncEvent("error", rel, str(e)))

    def _do_upload(self, rel, abs_path):
        if not abs_path.exists(): return
        log.info("\u2191 %s", rel)
        local_hash          = _sha256(str(abs_path))
        degoo_id, rem_hash  = self.api.upload(rel, str(abs_path))
        self.state.upsert(rel, degoo_id, abs_path.stat().st_mtime, local_hash, rem_hash)
        self.on_event(SyncEvent("upload", rel))

    def _do_download(self, rel, abs_path):
        log.info("\u2193 %s", rel)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        rem_hash   = self.api.download(rel, str(abs_path))
        local_hash = _sha256(str(abs_path))
        state      = self.state.get(rel)
        self.state.upsert(rel, state["degoo_id"] if state else 0,
                          abs_path.stat().st_mtime, local_hash, rem_hash)
        self.on_event(SyncEvent("download", rel))

    def _do_delete_remote(self, rel):
        state = self.state.get(rel)
        if state:
            self.api.delete(state["degoo_id"])
            self.state.delete(rel)
        self.on_event(SyncEvent("delete_remote", rel))

    def _do_delete_local(self, rel, abs_path):
        abs_path.unlink(missing_ok=True)
        self.state.delete(rel)
        self.on_event(SyncEvent("delete_local", rel))

    def _do_conflict(self, rel, abs_path):
        log.warning("\u26a1 conflict %s", rel)
        conflict = abs_path.with_suffix(abs_path.suffix + self.conflict_suffix)
        shutil.copy2(str(abs_path), str(conflict))
        self._do_download(rel, abs_path)
        self.on_event(SyncEvent("conflict", rel, f"Local copy saved as {conflict.name}"))
