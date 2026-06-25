"""
degoo_sync/degoo_api_adapter.py

Thin adapter wrapping fuse_degoo.py Degoo API calls into the
interface expected by SyncEngine.

Expected interface (duck-typed):
    api.list_all()              -> [{rel_path, hash, degoo_id, ...}]
    api.upload(rel, local_abs)  -> (degoo_id, remote_hash)
    api.download(rel, dest_abs) -> remote_hash
    api.delete(degoo_id)        -> None
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("degoo_api_adapter")


def _load_fuse_module(fuse_path: str):
    spec = importlib.util.spec_from_file_location("fuse_degoo", fuse_path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha256_local(path: str, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(buf):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


class DegooAPIAdapter:
    """
    Wraps Degoo GraphQL/REST calls from fuse_degoo.py.

    Parameters
    ----------
    fuse_degoo_path : str   — path to fuse_degoo.py
    email, password : str   — Degoo credentials
    remote_root     : str   — Degoo path acting as sync root
    tree_cache_db   : str   — path to SQLite tree cache (optional)
    """

    def __init__(self, fuse_degoo_path, email, password,
                 remote_root="/", tree_cache_db=None):
        self._mod         = _load_fuse_module(fuse_degoo_path)
        self._email       = email
        self._password    = password
        self._remote_root = remote_root.rstrip("/") or "/"
        self._logged_in   = False

    def login(self):
        if self._logged_in: return
        self._mod.degoo_login(self._email, self._password)
        self._logged_in = True

    def list_all(self) -> list[dict]:
        self.login()
        items   = []
        root_id = self._resolve_path(self._remote_root)
        self._walk(root_id, "", items)
        return items

    def _walk(self, degoo_id, rel_prefix, out):
        for child in (self._mod.get_children(degoo_id) or []):
            name     = child.get("Name", "")
            rel_path = f"{rel_prefix}/{name}".lstrip("/")
            kind     = child.get("MetaData", {}).get("Kind", 0)
            if kind == 4:
                self._walk(child["ID"], rel_path, out)
            else:
                out.append({
                    "rel_path": rel_path,
                    "degoo_id": child["ID"],
                    "hash":     child.get("SHA256HEX", ""),
                    "size":     child.get("Size", 0),
                })

    def _resolve_path(self, path: str) -> int:
        try:
            return self._mod.get_item_from_path(path)["ID"]
        except Exception:
            return getattr(self._mod, "DEGOO_ROOT_ID", 0)

    def upload(self, rel_path: str, local_abs: str):
        self.login()
        remote_dir = str(Path(self._remote_root) / Path(rel_path).parent).rstrip("/") or "/"
        parent_id  = self._resolve_path(remote_dir)
        degoo_id   = self._mod.upload_file(local_abs, parent_id, Path(rel_path).name)
        rem_hash   = self._sha256_remote(degoo_id)
        return degoo_id, rem_hash

    def download(self, rel_path: str, dest_abs: str) -> str:
        self.login()
        item = self._mod.get_item_from_path(str(Path(self._remote_root) / rel_path))
        url  = item.get("URL") or self._mod.get_file_url(item["ID"])
        self._mod.download_file(url, dest_abs)
        return _sha256_local(dest_abs)

    def delete(self, degoo_id: int):
        self.login()
        self._mod.delete_item(degoo_id)

    def _sha256_remote(self, degoo_id: int) -> str:
        try:
            return self._mod.get_item(degoo_id).get("SHA256HEX", "")
        except Exception:
            return ""
