"""
degoo_sync/ipc_bridge.py

Spawned as a child process by Electron main.
Reads JSON commands from stdin, writes JSON events to stdout.

Protocol (newline-delimited JSON):
  STDIN :
    {"cmd": "start_sync",   "args": {...}}
    {"cmd": "stop_sync"}
    {"cmd": "start_mount",  "args": {...}}
    {"cmd": "stop_mount"}
    {"cmd": "status"}
    {"cmd": "quit"}
  STDOUT:
    {"event": "ready"}
    {"event": "status",       "sync": "...", "mount": "..."}
    {"event": "sync_progress","rel_path": "...", "kind": "...", "detail": "..."}
    {"event": "error",        "message": "..."}
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from .sync_engine import SyncEngine, SyncEvent

log = logging.getLogger("ipc_bridge")
logging.basicConfig(level=logging.WARNING)


def _emit(obj: dict):
    print(json.dumps(obj), flush=True)


class IpcBridge:
    def __init__(self):
        self._sync_engine: Optional[SyncEngine] = None
        self._mount_proc:  Optional[subprocess.Popen] = None
        self._sync_status  = "stopped"
        self._mount_status = "stopped"

    def start_sync(self, args: dict):
        from .degoo_api_adapter import DegooAPIAdapter
        api = DegooAPIAdapter(
            fuse_degoo_path = args.get("fuse_path", "fuse_degoo.py"),
            email           = args["email"],
            password        = args["password"],
            remote_root     = args.get("degooPath", "/"),
            tree_cache_db   = args.get("dbPath"),
        )
        def on_event(e: SyncEvent):
            _emit({"event": "sync_progress", "rel_path": e.rel_path,
                   "kind": e.kind, "detail": e.detail})
        self._sync_engine = SyncEngine(
            local_dir  = args["syncDir"],
            degoo_api  = api,
            state_db   = args.get("stateDb",
                         str(Path.home()/".cache/degoo_drive/sync_state.db")),
            on_event   = on_event,
            poll_secs  = int(args.get("pollSecs", 60)),
        )
        self._sync_engine.start()
        self._sync_status = "running"
        self._emit_status()

    def stop_sync(self):
        if self._sync_engine:
            self._sync_engine.stop()
            self._sync_engine = None
        self._sync_status = "stopped"
        self._emit_status()

    def start_mount(self, args: dict):
        fuse_path = args.get("fuse_path", "fuse_degoo.py")
        cmd = [
            sys.executable, fuse_path,
            "--mountpoint",           args["mountpoint"],
            "--degoo-email",          args["email"],
            "--degoo-pass",           args["password"],
            "--degoo-path",           args.get("degooPath", "/"),
            "--cache-size",           str(args.get("cacheSizeMb", 128)),
            "--refresh-interval",     str(args.get("refreshIntervalMin", 10)),
            "--download-threads",     str(args.get("downloadThreads", 8)),
            "--subchunk-connections", str(args.get("subchunkConnections", 8)),
            "--lookahead-chunks",     str(args.get("lookaheadChunks", 2)),
            "--db-path",              args.get("dbPath",
                                        str(Path.home()/".cache/degoo_drive/tree_cache.db")),
            "--chunk-cache-dir",      args.get("chunkCacheDir",
                                        str(Path.home()/".cache/degoo_drive/chunks")),
            "--chunk-max-age",        str(args.get("chunkMaxAge", 3600)),
            "--allow-other",
        ]
        Path(args["mountpoint"]).mkdir(parents=True, exist_ok=True)
        self._mount_proc  = subprocess.Popen(cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._mount_status = "running"
        self._emit_status()
        threading.Thread(target=self._watch_mount, daemon=True).start()

    def _watch_mount(self):
        rc = self._mount_proc.wait()
        self._mount_status = "stopped" if rc == 0 else "error"
        self._emit_status()

    def stop_mount(self):
        if self._mount_proc:
            self._mount_proc.terminate()
            try: self._mount_proc.wait(timeout=5)
            except subprocess.TimeoutExpired: self._mount_proc.kill()
            self._mount_proc = None
        self._mount_status = "stopped"
        self._emit_status()

    def _emit_status(self):
        _emit({"event": "status",
               "sync":  self._sync_status,
               "mount": self._mount_status})

    def run(self):
        _emit({"event": "ready"})
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            try:   msg = json.loads(line)
            except json.JSONDecodeError: continue
            cmd  = msg.get("cmd")
            args = msg.get("args", {})
            if   cmd == "start_sync":  self.start_sync(args)
            elif cmd == "stop_sync":   self.stop_sync()
            elif cmd == "start_mount": self.start_mount(args)
            elif cmd == "stop_mount":  self.stop_mount()
            elif cmd == "status":      self._emit_status()
            elif cmd == "quit":
                self.stop_sync(); self.stop_mount(); break


def main():
    IpcBridge().run()


if __name__ == "__main__":
    main()
