#!/usr/bin/env python3
"""fuse_degoo.py — FUSE driver for Degoo cloud storage.

Updated to use cligoo (https://github.com/marcomc/cligoo) as the Degoo API
backend instead of the bundled degoo/__init__.py module.

Directory tree is now persisted in a SQLite database so the FUSE mount
starts instantly on subsequent runs without a full API rescan.

Download chunks are also persisted in a dedicated cache directory tracked
by the same SQLite database, so files do not need to be re-downloaded
after a restart or reboot.

Original degoo_drive: https://github.com/Laitinlok/degoo_drive
cligoo API:           https://github.com/marcomc/cligoo
"""

import datetime
import errno
import faulthandler
import glob
import json
import logging
import mimetypes
import os
import sqlite3
import stat as stat_m
import sys
import tempfile
import threading
import time
from argparse import ArgumentParser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from os import fsencode, fsdecode
from urllib.parse import urlparse
from pathlib import Path

import pyfuse3
import requests
import trio
import urllib3
import re
from pyfuse3 import FUSEError

# ---------------------------------------------------------------------------
# cligoo integration
# ---------------------------------------------------------------------------
from cligoo.api import DegooClient, DegooAPIError
from cligoo.auth import get_token, AuthError, login  # was: login_password

# to load the module from there first.
basedir = os.path.abspath(os.path.join(os.path.dirname(sys.argv[0]), '..'))
if (os.path.exists(os.path.join(basedir, 'setup.py')) and
        os.path.exists(os.path.join(basedir, 'src', 'pyfuse3.pyx'))):
    sys.path.insert(0, os.path.join(basedir, 'src'))

faulthandler.enable()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_CACHE_BASE = os.path.join(
    os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache')),
    'degoo_drive',
)
_DEFAULT_DB_PATH = os.path.join(_CACHE_BASE, 'tree_cache.db')
_DEFAULT_CHUNK_CACHE_DIR = os.path.join(_CACHE_BASE, 'chunks')


# ---------------------------------------------------------------------------
# FUSE allow_other permission check
# ---------------------------------------------------------------------------

def _fuse3_allow_other_permitted() -> bool:
    """Return True only when user_allow_other is enabled in /etc/fuse3.conf
    (or the legacy /etc/fuse.conf).

    FUSE raises a fatal "option allow_other only allowed if 'user_allow_other'
    is set in /etc/fuse3.conf" error when the option is passed without the
    sysadmin having opted in.  This check lets the GUI app pass --allow-other
    safely: the flag is honoured when permitted and silently skipped otherwise.
    """
    for conf in ('/etc/fuse3.conf', '/etc/fuse.conf'):
        try:
            with open(conf) as fh:
                for line in fh:
                    stripped = line.strip()
                    # accept bare "user_allow_other" or "user_allow_other = ..."
                    if stripped.startswith('user_allow_other') and not stripped.startswith('#'):
                        return True
        except OSError:
            pass
    return False


# ---------------------------------------------------------------------------
# SQLite-backed tree cache
# ---------------------------------------------------------------------------

class TreeCache:
    """Thread-safe, SQLite-backed mapping that replaces the old `degoo_tree_content` dict.

    Each entry is stored as a JSON blob keyed by the integer item ID.
    The database uses WAL journaling so concurrent readers never block writers.

    A ``parent_id`` column is maintained alongside each row and indexed so
    that :meth:`children_of` can retrieve all direct children in O(children)
    time rather than scanning every row.

    The class exposes the minimal dict-like interface used throughout this
    module:  ``__getitem__``, ``__setitem__``, ``__delitem__``,
    ``__contains__``, ``__iter__``, ``items()``, ``values()``, ``get()``,
    ``keys()`` and ``clear()``.
    """

    def __init__(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._local = threading.local()  # per-thread connection
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # Bootstrap the schema on the main thread
        conn = self._conn()
        conn.execute(
            'CREATE TABLE IF NOT EXISTS items '
            '(inode INTEGER PRIMARY KEY, parent_id INTEGER, payload TEXT NOT NULL)'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_items_parent_id ON items(parent_id)'
        )
        conn.execute('PRAGMA journal_mode=WAL')
        conn.commit()

    # ------------------------------------------------------------------
    # Connection management (one sqlite3 connection per OS thread)
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, 'conn', None):
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')   # faster, still safe with WAL
            self._local.conn = conn
        return self._local.conn

    # ------------------------------------------------------------------
    # dict-like interface
    # ------------------------------------------------------------------

    def __getitem__(self, inode: int) -> dict:
        row = self._conn().execute(
            'SELECT payload FROM items WHERE inode=?', (int(inode),)
        ).fetchone()
        if row is None:
            raise KeyError(inode)
        return json.loads(row[0])

    def __setitem__(self, inode: int, value: dict) -> None:
        conn = self._conn()
        parent_id = int(value.get('ParentID') or 0) if isinstance(value, dict) else None
        conn.execute(
            'INSERT OR REPLACE INTO items (inode, parent_id, payload) VALUES (?, ?, ?)',
            (int(inode), parent_id, json.dumps(value)),
        )
        conn.commit()

    def __delitem__(self, inode: int) -> None:
        conn = self._conn()
        conn.execute('DELETE FROM items WHERE inode=?', (int(inode),))
        conn.commit()

    def __contains__(self, inode: object) -> bool:
        row = self._conn().execute(
            'SELECT 1 FROM items WHERE inode=?', (int(inode),)  # type: ignore[arg-type]
        ).fetchone()
        return row is not None

    def __iter__(self):
        for row in self._conn().execute('SELECT inode FROM items'):
            yield row[0]

    def get(self, inode: int, default=None):
        try:
            return self[inode]
        except KeyError:
            return default

    def children_of(self, parent_id: int) -> list:
        """Return list of entry dicts whose ParentID == parent_id.
        Uses the parent_id index — O(children) not O(total items)."""
        rows = self._conn().execute(
            'SELECT payload FROM items WHERE parent_id=?', (int(parent_id),)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def items(self):
        for row in self._conn().execute('SELECT inode, payload FROM items'):
            yield row[0], json.loads(row[1])

    def values(self):
        for row in self._conn().execute('SELECT payload FROM items'):
            yield json.loads(row[0])

    def keys(self):
        for row in self._conn().execute('SELECT inode FROM items'):
            yield row[0]

    def clear(self) -> None:
        conn = self._conn()
        conn.execute('DELETE FROM items')
        conn.commit()

    def __len__(self) -> int:
        row = self._conn().execute('SELECT COUNT(*) FROM items').fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# SQLite-backed chunk cache
# ---------------------------------------------------------------------------

class ChunkCache:
    """Persistent cache for downloaded file chunks.

    Chunk bytes are stored as regular files in *cache_dir* so they can be
    memory-mapped and read without copying.  The SQLite database (shared with
    TreeCache) tracks each chunk's path, size, and last-accessed timestamp so
    the maintenance thread can evict stale entries without scanning the
    filesystem.

    Schema (``chunks`` table)::

        item_id      INTEGER  — Degoo item/file ID
        part         INTEGER  — zero-based chunk index
        path         TEXT     — absolute path to the chunk file on disk
        size         INTEGER  — byte length of the chunk
        last_accessed INTEGER — Unix timestamp (seconds) of last read
        PRIMARY KEY (item_id, part)

    Thread safety
    -------------
    Same one-connection-per-thread strategy as TreeCache.
    """

    def __init__(self, db_path: str, cache_dir: str = _DEFAULT_CHUNK_CACHE_DIR) -> None:
        self._db_path = db_path
        self._cache_dir = cache_dir
        self._local = threading.local()
        os.makedirs(cache_dir, exist_ok=True)
        conn = self._conn()
        conn.execute(
            'CREATE TABLE IF NOT EXISTS chunks ('
            '  item_id      INTEGER NOT NULL,'
            '  part         INTEGER NOT NULL,'
            '  path         TEXT    NOT NULL,'
            '  size         INTEGER NOT NULL DEFAULT 0,'
            '  last_accessed INTEGER NOT NULL DEFAULT 0,'
            '  PRIMARY KEY (item_id, part)'
            ')'
        )
        conn.execute('PRAGMA journal_mode=WAL')
        conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, 'conn', None):
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            self._local.conn = conn
        return self._local.conn

    def _stable_path(self, item_id: int, part: int) -> str:
        """Return the deterministic filesystem path for a chunk.

        Uses ``<item_id>_<part>.chunk`` so the same chunk always maps to the
        same file regardless of the Degoo filename (avoids collisions between
        two files that share the same display name).
        """
        return os.path.join(self._cache_dir, f'{item_id}_{part}.chunk')

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk_path(self, item_id: int, part: int) -> str:
        """Return the path where this chunk should be (or is already) stored."""
        row = self._conn().execute(
            'SELECT path FROM chunks WHERE item_id=? AND part=?',
            (item_id, part),
        ).fetchone()
        if row:
            return row[0]
        # Not registered yet — return the stable computed path.
        return self._stable_path(item_id, part)

    def exists(self, item_id: int, part: int) -> bool:
        """Return True if the chunk is registered in the DB *and* the file exists on disk."""
        row = self._conn().execute(
            'SELECT path FROM chunks WHERE item_id=? AND part=?',
            (item_id, part),
        ).fetchone()
        if row is None:
            return False
        if not os.path.isfile(row[0]):
            # DB row is stale — clean it up.
            self._conn().execute(
                'DELETE FROM chunks WHERE item_id=? AND part=?', (item_id, part)
            )
            self._conn().commit()
            return False
        return True

    def touch(self, item_id: int, part: int) -> None:
        """Update the last_accessed timestamp for a chunk (called on every read)."""
        self._conn().execute(
            'UPDATE chunks SET last_accessed=? WHERE item_id=? AND part=?',
            (int(time.time()), item_id, part),
        )
        self._conn().commit()

    def register(self, item_id: int, part: int, path: str, size: int) -> None:
        """Record a newly downloaded chunk in the database."""
        conn = self._conn()
        conn.execute(
            'INSERT OR REPLACE INTO chunks (item_id, part, path, size, last_accessed) '
            'VALUES (?, ?, ?, ?, ?)',
            (item_id, part, path, size, int(time.time())),
        )
        conn.commit()

    def evict_file(self, item_id: int) -> None:
        """Remove all cached chunks for *item_id* from disk and the DB."""
        conn = self._conn()
        rows = conn.execute(
            'SELECT path FROM chunks WHERE item_id=?', (item_id,)
        ).fetchall()
        for (path,) in rows:
            try:
                os.remove(path)
                log.debug('ChunkCache: evicted %s', path)
            except FileNotFoundError:
                pass
        conn.execute('DELETE FROM chunks WHERE item_id=?', (item_id,))
        conn.commit()

    def evict_stale(self, max_age_seconds: int = 3600) -> int:
        """Delete chunks not accessed within *max_age_seconds*.

        Returns the number of chunks evicted.
        """
        cutoff = int(time.time()) - max_age_seconds
        conn = self._conn()
        rows = conn.execute(
            'SELECT item_id, part, path FROM chunks WHERE last_accessed < ?',
            (cutoff,),
        ).fetchall()
        for item_id, part, path in rows:
            try:
                os.remove(path)
                log.debug('ChunkCache: evicted stale chunk %s', path)
            except FileNotFoundError:
                pass
        if rows:
            conn.execute('DELETE FROM chunks WHERE last_accessed < ?', (cutoff,))
            conn.commit()
        return len(rows)

    def evict_all(self) -> None:
        """Remove every cached chunk (used during full tree refresh)."""
        conn = self._conn()
        rows = conn.execute('SELECT path FROM chunks').fetchall()
        for (path,) in rows:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        conn.execute('DELETE FROM chunks')
        conn.commit()

    def __len__(self) -> int:
        row = self._conn().execute('SELECT COUNT(*) FROM chunks').fetchone()
        return row[0] if row else 0


# Global caches – initialised in main() once CLI args are known.
tree_cache: TreeCache = None  # type: ignore[assignment]
chunk_cache: ChunkCache = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Legacy alias so all existing code referencing `degoo_tree_content` keeps
# working without modification.  Reassigned in main() / load_degoo_content().
# ---------------------------------------------------------------------------
degoo_tree_content: TreeCache = None  # type: ignore[assignment]

# Global cligoo client (initialised in main())
_client: DegooClient = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Per-process persistent HTTP session with connection pooling
# ---------------------------------------------------------------------------
_http_session: requests.Session = None  # type: ignore[assignment]


def _make_http_session(pool_connections: int = 8, pool_maxsize: int = 32) -> requests.Session:
    """Create a requests.Session with a large urllib3 connection pool.

    A single TCP connection to Degoo's CDN is capped at ~8 MB/s by the
    server-side per-connection rate limit.  Opening *N* parallel connections
    to the same host multiplies that ceiling by N.  A shared Session with a
    large HTTPAdapter pool lets all download threads reuse existing sockets
    instead of paying the TCP/TLS handshake cost on every request.
    """
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=urllib3.Retry(
            total=5,
            backoff_factor=0.5,
            status_forcelist={429, 500, 502, 503, 504},
            allowed_methods={"GET"},
        ),
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


LOCAL_PATH_DEGOO = '/home/degoo'
PATH_ROOT_DEGOO = '/'
DEGOO_HOSTNAME_EU = 'c.degoo.eu'

percentage_read = 25
is_refresh_enabled = True
caching_file_list = []
threadLock = threading.Lock()
requests_control = []

# How many sub-ranges to split one cache chunk into for parallel downloading.
# Each sub-range is fetched on its own TCP connection simultaneously.
_DEFAULT_SUBCHUNK_CONNECTIONS = 8


# ---------------------------------------------------------------------------
# Helpers to adapt cligoo item dicts to the format degoo_drive expects
# ---------------------------------------------------------------------------

def _cligoo_item_to_tree(item: dict, parent_path: str) -> dict:
    """Convert a cligoo list_dir item to the degoo_tree_content format."""
    name = item.get('Name', '')
    file_path = parent_path.rstrip('/') + '/' + name if parent_path != '/' else '/' + name
    is_folder = _client.is_folder(item)
    return {
        'ID': int(item['ID']),
        'Name': name,
        'FilePath': file_path,
        'Size': int(item.get('Size') or 0),
        'ParentID': int(item.get('ParentID') or 0),
        'isFolder': is_folder,
        'LastUploadTime': item.get('LastUploadTime'),
        'LastModificationTime': item.get('LastModificationTime'),
        'CreationTime': item.get('CreationTime'),
        'URL': item.get('URL') or item.get('OptimizedURL') or item.get('ThumbnailURL'),
        'Category': item.get('Category', 0),
    }


def _build_tree_recursive(parent_id: int, parent_path: str, mode: str) -> None:
    """Single-folder fetch used by lazy mode and _fetch_dir_if_needed."""
    items = _client.list_dir(str(parent_id), limit=None)
    for item in items:
        parent_entry = degoo_tree_content.get(parent_id)
        p_path = parent_entry['FilePath'] if parent_entry else parent_path
        entry = _cligoo_item_to_tree(item, p_path)
        degoo_tree_content[entry['ID']] = entry

        if mode != 'lazy' and entry['isFolder']:
            _build_tree_recursive(entry['ID'], entry['FilePath'], mode)


def _build_tree_parallel(root_id: int, root_path: str, mode: str, workers: int = 8) -> None:
    """BFS parallel tree fetch -- each directory listing runs in its own thread.

    Compared with the serial recursive approach this keeps *workers* API
    connections busy simultaneously, which dramatically reduces wall-clock
    time when a large number of sub-directories must be scanned.  The
    TreeCache (SQLite WAL + per-thread connections) is safe for concurrent
    writes, so multiple threads can call ``degoo_tree_content[k] = v``
    without additional locking.

    Falls back to the serial recursive helper for ``mode=lazy`` so that
    single-directory on-demand fetches are not penalised by thread overhead.
    """
    if mode == 'lazy':
        _build_tree_recursive(root_id, root_path, mode)
        return

    queue = [(root_id, root_path)]

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="degoo-tree") as pool:
        while queue:
            futures = {
                pool.submit(_client.list_dir, str(pid), None): (pid, ppath)
                for pid, ppath in queue
            }
            queue = []
            for future in as_completed(futures):
                pid, ppath = futures[future]
                try:
                    items = future.result()
                except Exception as exc:
                    log.warning('_build_tree_parallel: list_dir failed: %s %s', pid, exc)
                    continue
                for item in items:
                    parent_entry = degoo_tree_content.get(pid)
                    p_path = parent_entry['FilePath'] if parent_entry else ppath
                    entry = _cligoo_item_to_tree(item, p_path)
                    degoo_tree_content[entry['ID']] = entry
                    if entry['isFolder']:
                        queue.append((entry['ID'], entry['FilePath']))


def _fetch_dir_if_needed(dir_id: int, mode: str) -> None:
    """Fetch children of *dir_id* from the API if not yet loaded (lazy mode)."""
    if mode == 'lazy':
        if hasattr(degoo_tree_content, 'children_of'):
            has_children = len(degoo_tree_content.children_of(dir_id)) > 0
        else:
            has_children = any(
                e['ParentID'] == dir_id for e in degoo_tree_content.values()
            )
        if not has_children:
            parent = degoo_tree_content.get(dir_id)
            p_path = parent['FilePath'] if parent else '/'
            _build_tree_recursive(dir_id, p_path, 'lazy')


# ---------------------------------------------------------------------------
# FUSE Operations
# ---------------------------------------------------------------------------

class Operations(pyfuse3.Operations):
    enable_writeback_cache = True

    def __init__(self, source, cache_size, flood_sleep_time, flood_time_to_check, flood_max_requests,
                 enable_flood_control, change_hostname, mode, plex_split_file,
                 chunk_cache: ChunkCache,
                 download_threads=8, subchunk_connections=_DEFAULT_SUBCHUNK_CONNECTIONS,
                 lookahead_chunks=2):
        super().__init__()
        self._inode_path_map = {pyfuse3.ROOT_INODE: source}
        self._source = source
        self._lookup_cnt = defaultdict(lambda: 0)
        self._fd_inode_map = dict()
        self._inode_fd_map = dict()
        self._fd_open_count = dict()
        self._degoo_path = dict()
        self._fd_buffer_length = dict()
        self._cache_size = cache_size
        self._min_size_read_next_part = (percentage_read * self._cache_size) / 100
        self._flood_sleep_time = flood_sleep_time
        self._flood_time_to_check = flood_time_to_check
        self._flood_max_requests = flood_max_requests
        self._enable_flood_control = enable_flood_control
        self._change_hostname = change_hostname
        self._mode = mode
        self._plex_split_file = plex_split_file
        self._chunk_cache = chunk_cache
        self._subchunk_connections = subchunk_connections
        # Number of chunks to pre-fetch ahead of the current read position.
        self._lookahead_chunks = max(1, lookahead_chunks)
        # Thread pool: chunk-level parallelism (one future per chunk).
        # sub-chunk parallel downloads run inside _cache_file using a *separate*
        # inner executor so we don't dead-lock the outer pool.
        self._download_executor = ThreadPoolExecutor(
            max_workers=download_threads,
            thread_name_prefix="degoo-dl",
        )

    def __del__(self):
        try:
            self._download_executor.shutdown(wait=False)
        except Exception:
            pass

    def _set_id_root_degoo(self, id_degoo):
        self._id_root_degoo = id_degoo

    def _get_id_root_degoo(self):
        return self._id_root_degoo

    def _inode_to_path(self, inode, fullpath=False):
        try:
            val = self._inode_path_map[inode]
        except KeyError:
            raise FUSEError(errno.ENOENT)

        if pyfuse3.ROOT_INODE == inode:
            return val

        if '/' in val and not fullpath:
            val = val[val.rfind('/') + 1:]

        if isinstance(val, set):
            val = next(iter(val))
        return val

    def _add_path(self, inode, path):
        log.debug('_add_path for %d, %s', inode, path)
        self._lookup_cnt[inode] += 1

        if inode not in self._inode_path_map:
            self._inode_path_map[inode] = path
            return

        val = self._inode_path_map[inode]
        if isinstance(val, set):
            val.add(path)
        elif val != path:
            self._inode_path_map[inode] = {path, val}

    async def forget(self, inode_list):
        for (inode, nlookup) in inode_list:
            if self._lookup_cnt[inode] > nlookup:
                self._lookup_cnt[inode] -= nlookup
                continue
            log.debug('forgetting about inode %d', inode)
            assert inode not in self._inode_fd_map
            del self._lookup_cnt[inode]
            try:
                del self._inode_path_map[inode]
            except KeyError:
                pass

    async def lookup(self, inode_p, name, ctx=None):
        name = fsdecode(name)
        log.debug('lookup for %s in %d', name, inode_p)

        if inode_p == pyfuse3.ROOT_INODE:
            inode_p = self._get_id_root_degoo()

        # BUG FIX: cp/mv/cat/stat never call opendir+readdir first, so in
        # lazy mode the directory children may not yet be in degoo_tree_content.
        # Trigger a fetch for this parent dir if it has no children yet.
        if self._mode == 'lazy':
            if hasattr(degoo_tree_content, 'children_of'):
                _no_children = len(degoo_tree_content.children_of(inode_p)) == 0
            else:
                _no_children = not any(
                    e['ParentID'] == inode_p for e in degoo_tree_content.values()
                )
            if _no_children:
                _fetch_dir_if_needed(inode_p, self._mode)
                self._refresh_path()

        children = self._get_degoo_childs(inode_p)
        attr = None

        for element in children:
            if name == element['Name']:
                attr = self._get_degoo_attrs(element['FilePath'])
                break

        if attr:
            return attr
        else:
            raise FUSEError(errno.ENOENT)

    async def getattr(self, inode, ctx=None):
        if inode in self._inode_fd_map:
            return self._getattr(fd=self._inode_fd_map[inode])
        else:
            return self._get_degoo_attrs(self._inode_to_path(inode, fullpath=True))

    async def setattr(self, inode, attr, fields, fh, ctx):
        return self._get_degoo_attrs(self._inode_to_path(inode, fullpath=True))

    async def readlink(self, inode, ctx):
        path = self._inode_to_path(inode)
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        return fsencode(target)

    async def opendir(self, inode, ctx):
        return inode

    def _get_degoo_id(self, name):
        folder_id = None
        attr = 'FilePath' if '/' in name else 'Name'
        for idx, degoo_element in degoo_tree_content.items():
            if degoo_element[attr] == name:
                folder_id = degoo_element['ID']
                break
        return folder_id

    def _get_degoo_element(self, name):
        element_id = self._get_degoo_id(name)
        element = None
        if element_id is not None:
            element = self._get_degoo_element_by_id(element_id)
        return element

    def _get_degoo_element_by_id(self, element_id):
        if element_id == pyfuse3.ROOT_INODE:
            element_id = self._get_id_root_degoo()
        return degoo_tree_content[element_id]

    def _get_degoo_element_path_by_id(self, element_id):
        element = self._get_degoo_element_by_id(element_id)
        return element['FilePath']

    def _get_degoo_element_path_by_name(self, name):
        element = self._get_degoo_element(name)
        return element['FilePath']

    def _get_degoo_childs(self, parent_id):
        childs = []
        if parent_id is not None:
            if hasattr(degoo_tree_content, 'children_of'):
                childs = degoo_tree_content.children_of(parent_id)
            else:
                for idx, degoo_element in degoo_tree_content.items():
                    if degoo_element['ParentID'] == parent_id:
                        childs.append(degoo_element)
        return childs

    def _get_degoo_attrs(self, name):
        element = self._get_degoo_element(name)
        if not element:
            raise FUSEError(2)

        entry = pyfuse3.EntryAttributes()

        if int(element['ID']) == self._get_id_root_degoo() or element['isFolder']:
            entry.st_size = 0
            entry.st_mode = (stat_m.S_IFDIR | 0o755)
        else:
            entry.st_size = int(element['Size'])
            entry.st_mode = (stat_m.S_IFREG | 0o664)

        entry.st_ino = int(element['ID'])
        entry.st_uid = os.getuid()
        entry.st_gid = os.getgid()
        entry.st_blksize = 512
        entry.st_blocks = ((entry.st_size + entry.st_blksize - 1) // entry.st_blksize)

        timestamp = int(1438467123.985654)

        try:
            entry.st_atime_ns = int(element['LastUploadTime']) * 1e9
        except (KeyError, TypeError):
            entry.st_atime_ns = timestamp
        try:
            entry.st_ctime_ns = int(element['LastModificationTime']) * 1e9
        except (KeyError, TypeError):
            entry.st_ctime_ns = timestamp
        try:
            creation_time = datetime.datetime.fromisoformat(element['CreationTime'])
            entry.st_mtime_ns = creation_time.timestamp() * 1e9
        except (KeyError, TypeError):
            entry.st_mtime_ns = timestamp

        return entry

    def _getattr(self, path=None, fd=None):
        assert fd is None or path is None
        assert not (fd is None and path is None)
        try:
            if fd is None:
                stat = os.lstat(path)
            else:
                stat = os.fstat(fd)
        except OSError as exc:
            raise FUSEError(exc.errno)

        entry = pyfuse3.EntryAttributes()
        for attr in ('st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
                     'st_rdev', 'st_size', 'st_atime_ns', 'st_mtime_ns',
                     'st_ctime_ns'):
            setattr(entry, attr, getattr(stat, attr))
        entry.generation = 0
        entry.entry_timeout = 0
        entry.attr_timeout = 0
        entry.st_blksize = 512
        entry.st_blocks = ((entry.st_size + entry.st_blksize - 1) // entry.st_blksize)

        return entry

    async def readdir(self, inode, off, token):
        path = self._inode_to_path(inode, fullpath=True)
        log.debug('reading %s', path)

        parent_id = self._get_degoo_id(path)
        children = self._get_degoo_childs(parent_id)

        if self._mode == 'lazy' and len(children) == 0:
            _fetch_dir_if_needed(parent_id, self._mode)
            self._refresh_path()
            children = self._get_degoo_childs(parent_id)

        entries = []
        for element in children:
            attr = self._get_degoo_attrs(element['FilePath'])
            entries.append((attr.st_ino, element['Name'], attr))

        for (ino, name, attr) in sorted(entries):
            if ino <= off:
                continue
            if not pyfuse3.readdir_reply(
                    token, fsencode(name), attr, ino):
                break
            self._add_path(attr.st_ino, self._get_degoo_element_path_by_id(attr.st_ino))

    async def unlink(self, inode_p, name, ctx):
        name = fsdecode(name)
        parent = self._inode_to_path(inode_p, fullpath=True)
        path = parent.rstrip('/') + '/' + name

        file_id = self._get_degoo_id(path)
        if file_id is None:
            raise FUSEError(errno.ENOENT)
        _client.delete([str(file_id)])
        if file_id in degoo_tree_content:
            del degoo_tree_content[file_id]
        # Evict any cached chunks for this file
        self._chunk_cache.evict_file(file_id)

        if file_id in self._lookup_cnt:
            self._forget_path(file_id, path)

    async def rmdir(self, inode_p, name, ctx):
        name = fsdecode(name)
        parent = self._inode_to_path(inode_p, fullpath=True)
        path = parent.rstrip('/') + '/' + name

        file_id = self._get_degoo_id(path)
        if file_id is None:
            raise FUSEError(errno.ENOENT)
        _client.delete([str(file_id)])
        if file_id in degoo_tree_content:
            del degoo_tree_content[file_id]
        self._chunk_cache.evict_file(file_id)

        if file_id in self._lookup_cnt:
            self._forget_path(file_id, path)

    def _forget_path(self, inode, path):
        log.debug('forget %s for %d', path, inode)
        val = self._inode_path_map[inode]
        if isinstance(val, set):
            val.remove(path)
            if len(val) == 1:
                self._inode_path_map[inode] = next(iter(val))
        else:
            del self._inode_path_map[inode]

    async def rename(self, inode_p_old, name_old, inode_p_new, name_new,
                     flags, ctx):
        if flags != 0:
            raise FUSEError(errno.EINVAL)

        name_old = fsdecode(name_old)
        name_new = fsdecode(name_new)

        path = self._inode_to_path(inode_p_old, fullpath=True)
        path_old = path.rstrip('/') + '/' + name_old

        inode = self._get_degoo_id(path_old)

        if self._mode == 'lazy' and len(self._get_degoo_childs(inode_p_new)) == 0:
            _fetch_dir_if_needed(inode_p_new, self._mode)
            self._refresh_path()

        if inode_p_old == inode_p_new:
            _client.rename(str(inode), name_new)
            if inode in degoo_tree_content:
                entry = degoo_tree_content[inode]
                old_fp = entry['FilePath']
                new_fp = old_fp[:old_fp.rfind('/') + 1] + name_new
                entry['Name'] = name_new
                entry['FilePath'] = new_fp
                degoo_tree_content[inode] = entry  # write back to DB
        else:
            if name_old != name_new:
                _client.rename(str(inode), name_new)
                if inode in degoo_tree_content:
                    entry = degoo_tree_content[inode]
                    entry['Name'] = name_new
                    degoo_tree_content[inode] = entry
            _client.move([str(inode)], str(inode_p_new))
            if inode in degoo_tree_content:
                entry = degoo_tree_content[inode]
                new_parent_path = self._inode_to_path(inode_p_new, fullpath=True)
                new_fp = new_parent_path.rstrip('/') + '/' + name_new
                entry['FilePath'] = new_fp
                entry['ParentID'] = inode_p_new
                degoo_tree_content[inode] = entry

        path_new = self._inode_to_path(inode_p_new, fullpath=True).rstrip('/') + '/' + name_new

        val = self._inode_path_map[inode]
        if isinstance(val, set):
            assert len(val) > 1
            val.add(path_new)
            val.remove(path_old)
        else:
            del self._inode_path_map[inode]
            self._inode_path_map[inode] = path_new

    async def mkdir(self, inode_p, name, mode, ctx):
        name = fsdecode(name)
        base_path = self._inode_to_path(inode_p, fullpath=True)
        element_id = self._get_degoo_id(base_path)

        if element_id is None:
            log.debug('mkdir: cannot resolve parent inode %d (path=%s)', inode_p, base_path)
            raise FUSEError(errno.ENOENT)

        log.debug("Creating directory '%s' in Degoo path '%s' (id=%s)", name, base_path, element_id)

        try:
            _client.mkdir(name, str(element_id))
        except DegooAPIError as exc:
            log.debug('mkdir API error for %s in %s: %s', name, base_path, exc)
            raise FUSEError(errno.EIO)

        new_dir_item = _client.resolve_path_under(str(element_id), name)
        if not new_dir_item:
            raise FUSEError(errno.EIO)

        parent_entry = degoo_tree_content.get(element_id)
        p_path = parent_entry['FilePath'] if parent_entry else base_path
        new_entry = _cligoo_item_to_tree(new_dir_item, p_path)
        new_dir_id = new_entry['ID']
        degoo_tree_content[new_dir_id] = new_entry

        attr = self._get_degoo_attrs(new_entry['FilePath'])
        self._add_path(attr.st_ino, new_entry['FilePath'])

        return attr

    async def open(self, inode, flags, ctx):
        return pyfuse3.FileInfo(fh=inode)

    async def create(self, inode_p, name, mode, flags, ctx):
        path = os.path.join(self._get_temp_directory(), fsdecode(name))
        try:
            if os.path.exists(path):
                os.remove(path)
            fd = os.open(path, flags | os.O_CREAT | os.O_TRUNC)
        except OSError as exc:
            raise FUSEError(exc.exc)
        attr = self._getattr(fd=fd)
        self._add_path(attr.st_ino, path)
        self._inode_fd_map[attr.st_ino] = fd
        self._fd_inode_map[fd] = attr.st_ino
        self._fd_open_count[fd] = 1
        self._degoo_path[attr.st_ino] = self._inode_to_path(inode_p, fullpath=True)
        return pyfuse3.FileInfo(fh=fd, direct_io=True), attr

    async def read(self, fd, offset, length):
        path_file = self._inode_to_path(fd, fullpath=True)

        degoo_file_size = self._get_degoo_element_by_id(fd)['Size']
        item_id = self._get_degoo_id(path_file)

        first_file_part = offset // self._cache_size
        temp_filename = self._chunk_cache.chunk_path(item_id, first_file_part)

        if not self._chunk_cache.exists(item_id, first_file_part):
            self._check_requests()
            future = self._download_executor.submit(
                self._cache_file, path_file, first_file_part, degoo_file_size)
            future.result()
        else:
            # Chunk already on disk — update access time so it won't be evicted.
            self._chunk_cache.touch(item_id, first_file_part)

        # ---------------------------------------------------------------
        # Multi-chunk lookahead pre-fetch
        # ---------------------------------------------------------------
        for ahead in range(1, self._lookahead_chunks + 1):
            lookahead_part = first_file_part + ahead
            if lookahead_part * self._cache_size >= degoo_file_size:
                break
            next_temp = self._chunk_cache.chunk_path(item_id, lookahead_part)
            if next_temp not in caching_file_list and not self._chunk_cache.exists(item_id, lookahead_part):
                self._check_requests()
                log.debug('Pre-fetching chunk %d [%s]', lookahead_part, next_temp)
                caching_file_list.append(next_temp)
                self._download_executor.submit(
                    self._cache_file, path_file, lookahead_part, degoo_file_size)

        self.check_and_split_file(path_file)

        # ---------------------------------------------------------------
        # Serve the bytes from the already-downloaded chunk
        # ---------------------------------------------------------------
        result = (offset // self._cache_size)
        size_to_read = offset + length
        second_file_part = first_file_part + 1
        next_temp_filename = self._chunk_cache.chunk_path(item_id, second_file_part)

        if not os.path.isfile(temp_filename):
            raise pyfuse3.FUSEError(errno.ENOENT)

        file_descriptor = os.open(temp_filename, os.O_RDONLY)
        if offset - (result * self._cache_size) >= 0:
            os.lseek(file_descriptor, offset - (result * self._cache_size), os.SEEK_SET)
            byte = os.read(file_descriptor, length)
            os.close(file_descriptor)
        else:
            log.debug('Reading first part from two files. File 1 [%s]', temp_filename)

            part_offset = self._cache_size - ((result * self._cache_size) - offset)
            os.lseek(file_descriptor, part_offset, os.SEEK_SET)
            byte = os.read(file_descriptor, self._cache_size - length)
            os.close(file_descriptor)

            log.debug('Reading second part from two files. File 2 [%s]', next_temp_filename)

            self._clear_files(path_file, skip_item_id=item_id, skip_part=second_file_part)

            retries = 0
            while next_temp_filename in caching_file_list and retries < 10:
                log.debug('Waiting to read second part file [%s]', next_temp_filename)
                retries += 1
                time.sleep(0.5)

            if not os.path.isfile(next_temp_filename):
                raise pyfuse3.FUSEError(errno.ENOENT)

            file_descriptor = os.open(next_temp_filename, os.O_RDONLY)
            os.lseek(file_descriptor, 0, os.SEEK_SET)
            byte += os.read(file_descriptor, length - len(byte))
            os.close(file_descriptor)

        return byte

    def check_and_split_file(self, path_file):
        if self._plex_split_file:
            item_id = self._get_degoo_id(path_file)
            if item_id is None:
                return

            if not self._chunk_cache.exists(item_id, 0) and \
                    self._chunk_cache.chunk_path(item_id, 0) not in caching_file_list and \
                    self._is_media(path_file):

                fd_new_part = self._get_next_part_split_file(path_file)
                if fd_new_part:
                    path_file_new_part = self._inode_to_path(fd_new_part, fullpath=True)
                    degoo_file_size_new_part = self._get_degoo_element_by_id(fd_new_part)['Size']
                    last_part = degoo_file_size_new_part // self._cache_size

                    self._check_requests()

                    temp_filename = self._chunk_cache.chunk_path(item_id, 0)
                    caching_file_list.append(temp_filename)
                    pre_cache_parts = [0, 1, last_part]
                    for part in pre_cache_parts:
                        log.debug('Precaching split file %s, part %d', path_file_new_part, part)
                        self._download_executor.submit(
                            self._cache_file, path_file_new_part, part, degoo_file_size_new_part)

    def _get_next_part_split_file(self, path_file):
        fd_new_part = None
        filename = self._get_filename(path_file)
        if filename.rfind('.'):
            name = filename[:filename.rfind('.')]
        else:
            name = filename

        match = re.search(r'(cd|disc|disk|dvd|part|pt)\d+', name)
        if match:
            path = path_file[:path_file.rfind('/')]
            folder_id = self._get_degoo_id(path)
            children = self._get_degoo_childs(folder_id)
            next_part = int(match.group().replace(match.group(1), '')) + 1

            for element in children:
                match = re.search(r'(cd|disc|disk|dvd|part|pt)' + str(next_part), element['Name'])
                if match:
                    fd_new_part = element['ID']
                    break

        return fd_new_part

    async def write(self, fd, offset, buf):
        os.lseek(fd, offset, os.SEEK_SET)
        length = os.write(fd, buf)

        if fd not in self._fd_buffer_length:
            self._fd_buffer_length[fd] = length

        if length != self._fd_buffer_length[fd]:
            inode = self._fd_inode_map[fd]
            source_file = self._inode_to_path(inode, fullpath=True)
            filename = source_file[source_file.rfind('/') + 1:]

            target_path = self._degoo_path[inode]
            log.debug('Uploading file [%s] to Degoo path [%s]', filename, target_path)

            try:
                target_id = self._get_degoo_id(target_path)
                file_id = _client.upload(source_file, str(target_id), name=filename)

                new_item = _client.resolve_path_under(str(target_id), filename)
                if new_item:
                    parent_entry = degoo_tree_content.get(target_id)
                    p_path = parent_entry['FilePath'] if parent_entry else target_path
                    new_entry = _cligoo_item_to_tree(new_item, p_path)
                    degoo_tree_content[new_entry['ID']] = new_entry
                    path = new_entry['FilePath']
                    url = new_entry.get('URL', '')
                else:
                    path = target_path.rstrip('/') + '/' + filename
                    url = ''

                log.debug('Upload of file [%s] finished. Id [%s] Url [%s]', filename, file_id, url)

                if not url:
                    log.debug('WARN: file [%s] has not been uploaded successfully', filename)

                attr = self._get_degoo_attrs(path)
                self._add_path(attr.st_ino, path)
            except DegooAPIError as e:
                log.debug('ERROR uploading file [{}]: {}'.format(filename, str(e)))

        return length

    async def release(self, fd):
        try:
            element = self._get_degoo_element_by_id(fd)
            filename = self._get_filename(element['FilePath'])

            if self._plex_split_file:
                fd_new_part = self._get_next_part_split_file(element['FilePath'])
                if fd_new_part:
                    log.debug('Skipping release file part of file %s', filename)
                    return

            log.debug('Releasing file %s', filename)
            self._clear_upload_temp(element['FilePath'])

            return
        except Exception:
            pass

        # Guard: pyfuse3/Trio can deliver a release callback for an fd that has
        # already been cleaned up (duplicate release or race during teardown).
        # Treat it as a no-op rather than raising KeyError and crashing the mount.
        if fd not in self._fd_open_count:
            log.debug('release: fd %s already released; ignoring duplicate', fd)
            return

        if self._fd_open_count[fd] > 1:
            self._fd_open_count[fd] -= 1
            return

        self._fd_open_count.pop(fd, None)
        if fd in self._fd_buffer_length:
            del self._fd_buffer_length[fd]

        inode = self._fd_inode_map.pop(fd, None)
        if inode is not None:
            self._inode_fd_map.pop(inode, None)
        try:
            os.close(fd)
        except OSError as exc:
            raise FUSEError(exc.errno)

        if inode is not None:
            source_file = self._inode_to_path(inode, fullpath=True)
            try:
                del self._inode_path_map[inode]
            except KeyError:
                pass
            try:
                os.remove(source_file)
            except FileNotFoundError:
                pass

    def _check_requests(self):
        if self._enable_flood_control:
            global requests_control
            requests_control.append(datetime.datetime.now())
            self._control_requests_flood()

    def _control_requests_flood(self):
        global requests_control

        last_minute = datetime.datetime.now() - datetime.timedelta(minutes=self._flood_time_to_check)
        requests_control = [x for x in requests_control if x >= last_minute]
        number_of_requests = len(requests_control)
        log.debug('Number of requests made in %s minute(s): %s', str(self._flood_time_to_check),
                  str(number_of_requests))
        if number_of_requests > self._flood_max_requests:
            log.debug('Reached max of requests %s in %s minutes. Waiting %s seconds',
                      str(self._flood_max_requests), str(self._flood_time_to_check), str(self._flood_sleep_time))
            time.sleep(self._flood_sleep_time)

    def _is_media(self, filename):
        is_media_type = False
        if '/' in filename:
            filename = filename[filename.rfind('/') + 1:]

        mimetype_file = mimetypes.guess_type(filename)[0]
        if mimetype_file is not None:
            mimetype_file = mimetype_file.split('/')[0]
            is_media_type = mimetype_file == 'audio' or mimetype_file == 'video'
        return is_media_type

    def _cache_file(self, degoo_path_file: str, file_part: int, degoo_file_size: int) -> None:
        """Download one cache-chunk using multiple parallel sub-range requests."""
        global caching_file_list

        item_id = self._get_degoo_id(degoo_path_file)
        if item_id is None:
            log.debug('WARN: No item ID for path %s', degoo_path_file)
            raise pyfuse3.FUSEError(errno.ENOENT)

        if self._chunk_cache.exists(item_id, file_part):
            chunk_path = self._chunk_cache.chunk_path(item_id, file_part)
            log.debug('Chunk cache hit: item=%d part=%d [%s]', item_id, file_part, chunk_path)
            self._chunk_cache.touch(item_id, file_part)
            caching_file_list_remove(caching_file_list, chunk_path)
            return

        cached = degoo_tree_content.get(item_id, {})
        url = cached.get('URL')
        if not url:
            try:
                item = _client.get_item(str(item_id))
                url = item.get('URL') or item.get('OptimizedURL') or item.get('ThumbnailURL')
                if item_id in degoo_tree_content:
                    entry = degoo_tree_content[item_id]
                    entry['URL'] = url
                    degoo_tree_content[item_id] = entry
            except DegooAPIError as e:
                log.debug('Error getting info for file [%s]: %s', degoo_path_file, str(e))

        if not url:
            log.debug('WARN: No url for file %s', degoo_path_file)
            raise pyfuse3.FUSEError(errno.ENOENT)

        url_parsed = urlparse(url)

        chunk_path = self._chunk_cache.chunk_path(item_id, file_part)

        if not url_parsed.scheme:
            with open(chunk_path, 'wb') as out:
                out.write(url if isinstance(url, bytes) else url.encode())
            self._chunk_cache.register(item_id, file_part, chunk_path,
                                       os.path.getsize(chunk_path))
            caching_file_list_remove(caching_file_list, chunk_path)
            return

        if self._change_hostname and 'degoo' in url_parsed.hostname and url_parsed.hostname != DEGOO_HOSTNAME_EU:
            log.debug('Changing hostname [%s] to [%s]', url_parsed.hostname, DEGOO_HOSTNAME_EU)
            url = url_parsed._replace(netloc=DEGOO_HOSTNAME_EU).geturl()

        chunk_start = file_part * self._cache_size
        chunk_end_inclusive = min(chunk_start + self._cache_size, degoo_file_size) - 1
        chunk_bytes = chunk_end_inclusive - chunk_start + 1

        if self._chunk_cache.exists(item_id, file_part):
            self._chunk_cache.touch(item_id, file_part)
            caching_file_list_remove(caching_file_list, chunk_path)
            return

        n = self._subchunk_connections
        min_subchunk = 256 * 1024
        if chunk_bytes < min_subchunk * n:
            n = max(1, chunk_bytes // min_subchunk)

        sub_size = chunk_bytes // n
        ranges = []
        for i in range(n):
            s = chunk_start + i * sub_size
            e = (chunk_start + (i + 1) * sub_size - 1) if i < n - 1 else chunk_end_inclusive
            ranges.append((s, e))

        log.debug(
            'Downloading chunk %d [bytes %d-%d] via %d parallel sub-ranges',
            file_part, chunk_start, chunk_end_inclusive, n
        )

        def _fetch_range(byte_start: int, byte_end: int) -> tuple:
            resp = _http_session.get(
                url,
                headers={'Range': f'bytes={byte_start}-{byte_end}'},
                timeout=(10, 60),
                stream=False,
            )
            if resp.status_code == 206:
                return (byte_start, resp.content)
            if resp.status_code == 200:
                data = resp.content
                local_start = byte_start - chunk_start
                local_end   = byte_end   - chunk_start + 1
                return (byte_start, data[local_start:local_end])
            resp.raise_for_status()

        try:
            with ThreadPoolExecutor(
                max_workers=n, thread_name_prefix="degoo-sub"
            ) as sub_pool:
                futures = {
                    sub_pool.submit(_fetch_range, s, e): (s, e)
                    for s, e in ranges
                }
                results = {}
                for fut in as_completed(futures):
                    s, _ = futures[fut]
                    try:
                        start_pos, data = fut.result()
                        results[start_pos] = data
                    except Exception as exc:
                        log.debug(
                            'Sub-range [%d-%d] failed for [%s]: %s',
                            s, futures[fut][1], chunk_path, exc
                        )
                        raise

            assembled = b''.join(results[s] for s, _ in ranges)
            tmp_write = chunk_path + '.tmp'
            with open(tmp_write, 'wb') as out:
                out.write(assembled)
            os.replace(tmp_write, chunk_path)

            self._chunk_cache.register(item_id, file_part, chunk_path, len(assembled))

            log.debug('Downloaded + cached chunk [%s] (%d bytes)', chunk_path, len(assembled))

        except Exception as exc:
            log.debug('Error downloading chunk [%s]: %s', chunk_path, exc)
            for leftover in (chunk_path, chunk_path + '.tmp'):
                try:
                    os.remove(leftover)
                except FileNotFoundError:
                    pass
        finally:
            caching_file_list_remove(caching_file_list, chunk_path)

    def _get_temp_directory(self):
        temp_directory = os.path.join(tempfile.gettempdir(), 'degoo')
        if not os.path.exists(temp_directory):
            os.makedirs(temp_directory)
        return temp_directory

    def _get_filename(self, path_file):
        filename = path_file
        if '/' in filename:
            filename = filename[filename.rfind('/') + 1:]
        return filename

    def _get_temp_file(self, degoo_path_file, filepart):
        item_id = self._get_degoo_id(degoo_path_file)
        if item_id is None:
            filename = self._get_filename(degoo_path_file)
            name = filename[:filename.rfind('.')] if '.' in filename else filename
            ext = filename[filename.rfind('.') + 1:] if '.' in filename else 'bin'
            return os.path.join(self._chunk_cache._cache_dir,
                                f'{name}_{filepart}.{ext}')
        return self._chunk_cache.chunk_path(item_id, filepart)

    def _clear_files(self, filename, skip_filename=None,
                     skip_item_id=None, skip_part=None):
        item_id = self._get_degoo_id(filename) if '/' in filename else None

        if item_id is not None:
            keep_part = skip_part if skip_item_id == item_id else None
            conn = self._chunk_cache._conn()
            rows = conn.execute(
                'SELECT part, path FROM chunks WHERE item_id=?', (item_id,)
            ).fetchall()
            for part, path in rows:
                if keep_part is not None and part == keep_part:
                    continue
                try:
                    os.remove(path)
                    log.debug('_clear_files: evicted chunk %s', path)
                except FileNotFoundError:
                    pass
            if keep_part is not None:
                conn.execute(
                    'DELETE FROM chunks WHERE item_id=? AND part != ?',
                    (item_id, keep_part)
                )
            else:
                conn.execute('DELETE FROM chunks WHERE item_id=?', (item_id,))
            conn.commit()
            return

        base = self._get_filename(filename)
        if '.' in base:
            base = base[:base.rfind('.')]
        if skip_filename and '/' in skip_filename:
            skip_filename = skip_filename[skip_filename.rfind('/') + 1:]
        base = glob.escape(base)
        for file in glob.glob(os.path.join(self._get_temp_directory(), base) + '*',
                              recursive=False):
            if self._get_filename(file) != skip_filename:
                log.debug('Removing upload temp %s', file)
                try:
                    os.remove(file)
                except FileNotFoundError:
                    pass

    def _clear_upload_temp(self, path_file):
        filename = self._get_filename(path_file)
        temp_path = os.path.join(self._get_temp_directory(), filename)
        try:
            os.remove(temp_path)
            log.debug('Removed upload temp %s', temp_path)
        except FileNotFoundError:
            pass

    def _refresh_path(self):
        for idx, degoo_element in degoo_tree_content.items():
            if self._source in degoo_element['FilePath']:
                attr = self._get_degoo_attrs(degoo_element['FilePath'])
                inode = attr.st_ino
                path = degoo_element['FilePath']

                if inode not in self._inode_path_map:
                    self._add_path(inode, path)
                elif inode in self._inode_path_map and self._inode_path_map[inode] != path:
                    del self._inode_path_map[inode]
                    self._add_path(inode, path)

    def run_maintenance(self, interval=60, max_chunk_age=3600):
        cease_continuous_run = threading.Event()

        chunk_cache_ref = self._chunk_cache

        class ScheduleThread(threading.Thread):
            @classmethod
            def run(cls):
                while not cease_continuous_run.is_set():
                    evicted = chunk_cache_ref.evict_stale(max_chunk_age)
                    if evicted:
                        log.debug('Maintenance: evicted %d stale chunks', evicted)
                    time.sleep(interval)

        continuous_thread = ScheduleThread()
        continuous_thread.daemon = True
        continuous_thread.start()
        return cease_continuous_run

    def refresh_degoo_content(self, refresh_interval):
        while is_refresh_enabled:
            time.sleep(refresh_interval)
            log.debug('Loading Degoo content')
            self.load_degoo_content()
        log.debug('Refresh content finished')

    def load_degoo_content(self):
        # Resolve root and fetch the full tree *outside* the lock so that
        # FUSE operations (lookup, readdir, getattr) are not blocked for the
        # entire duration of a potentially slow API scan.
        root_item = _client.resolve_path(self._source)
        if root_item is None:
            raise RuntimeError(f'Degoo path not found: {self._source}')

        root_id = int(root_item['ID'])
        root_entry = {
            'ID': root_id,
            'Name': root_item.get('Name', self._source.rstrip('/').rsplit('/', 1)[-1] or '/'),
            'FilePath': self._source,
            'Size': 0,
            'ParentID': int(root_item.get('ParentID') or 0),
            'isFolder': True,
            'LastUploadTime': None,
            'LastModificationTime': None,
            'CreationTime': None,
            'URL': None,
            'Category': root_item.get('Category', 2),
        }

        # Clear + insert root while holding the lock (fast, no I/O).
        with threadLock:
            degoo_tree_content.clear()
            degoo_tree_content[root_id] = root_entry

        # Parallel BFS fetch -- runs outside the lock so FUSE ops stay responsive.
        workers = getattr(self._download_executor, '_max_workers', 8)
        _build_tree_parallel(root_id, self._source, self._mode, workers=workers)

        # Final bookkeeping back under the lock.
        with threadLock:
            self._set_id_root_degoo(root_id)
            self._refresh_path()


# ---------------------------------------------------------------------------
# Small thread-safe helper (avoids ValueError when removing from list)
# ---------------------------------------------------------------------------

def caching_file_list_remove(lst, value):
    try:
        lst.remove(value)
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Logging / argument parsing
# ---------------------------------------------------------------------------

def init_logging(debug=False):
    formatter = logging.Formatter('%(asctime)s.%(msecs)03d %(threadName)s: '
                                  '[%(name)s] %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    if debug:
        handler.setLevel(logging.DEBUG)
        root_logger.setLevel(logging.DEBUG)
    else:
        handler.setLevel(logging.INFO)
        root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)


def parse_args(args):
    """Parse command line"""

    parser = ArgumentParser()

    parser.add_argument('--mountpoint', type=str, default=LOCAL_PATH_DEGOO,
                        help='Where to mount the file system. Default is ' + LOCAL_PATH_DEGOO)
    parser.add_argument('--degoo-email', type=str,
                        help='Email to login in Degoo')
    parser.add_argument('--degoo-pass', type=str,
                        help='Password to login in Degoo')
    parser.add_argument('--degoo-refresh-token', type=str,
                        help='Used when token expires. Alternative if login fails')
    parser.add_argument('--degoo-path', type=str, default=PATH_ROOT_DEGOO,
                        help='Absolute path from Degoo. Default is ' + PATH_ROOT_DEGOO)
    parser.add_argument('--cache-size', type=int, default=128,
                        help='Size of each downloaded chunk in MB (default: 128)')
    parser.add_argument('--debug', action='store_true', default=False,
                        help='Enable debugging output')
    parser.add_argument('--debug-fuse', action='store_true', default=False,
                        help='Enable FUSE debugging output')
    parser.add_argument('--allow-other', action='store_true', default=False,
                        help=(
                            'Request allow_other FUSE option (lets other users access the mount). '
                            'Silently ignored when user_allow_other is absent from /etc/fuse3.conf '
                            'so the GUI app can always pass this flag without crashing.'
                        ))
    parser.add_argument('--refresh-interval', type=int, default=10,
                        help='Refresh degoo content interval (default: 10 * 60sec)')
    parser.add_argument('--disable-refresh', action='store_true', default=False,
                        help='Disable automatic refresh')
    parser.add_argument('--flood-sleep-time', action='store_true', default=60,
                        help='Waiting time, in seconds, before resuming requests once the maximum has been reached')
    parser.add_argument('--flood-max-requests', action='store_true', default=20,
                        help='Maximum number of requests in the period')
    parser.add_argument('--flood-time-to-check', action='store_true', default=1,
                        help='Request control period, in minutes')
    parser.add_argument('--enable-flood-control', action='store_true', default=False,
                        help='Enable flood control')
    parser.add_argument('--change-hostname', action='store_true', default=False,
                        help='Change domain for media files to EU endpoint')
    parser.add_argument('--mode', type=str, default='lazy',
                        help='How content is read. Default is lazy')
    parser.add_argument('--config-path', type=str,
                        help='Path to the cligoo configuration directory '
                             '(default: ~/.config/cligoo/)')
    parser.add_argument('--plex-split-file', action='store_true', default=False,
                        help='Check if there are split files to cache the first part')
    parser.add_argument('--download-threads', type=int, default=8,
                        help='Number of parallel chunk-download threads (default: 8)')
    parser.add_argument('--subchunk-connections', type=int, default=_DEFAULT_SUBCHUNK_CONNECTIONS,
                        help=(
                            'Number of parallel TCP connections used to download a single '
                            'chunk (default: %(default)s). Each connection fetches an equal '
                            'sub-range; raising this value multiplies the per-chunk throughput '
                            'up to your line speed.'
                        ))
    parser.add_argument('--lookahead-chunks', type=int, default=2,
                        help='Number of chunks to pre-fetch ahead of the current read position (default: 2)')
    parser.add_argument('--db-path', type=str, default=_DEFAULT_DB_PATH,
                        help=(
                            'Path to the SQLite database used to persist the directory tree '
                            'and chunk-cache metadata (default: %(default)s). '
                            'Point to a persistent volume path in Docker/Kubernetes so the '
                            'cache survives container restarts.'
                        ))
    parser.add_argument('--chunk-cache-dir', type=str, default=_DEFAULT_CHUNK_CACHE_DIR,
                        help=(
                            'Directory where downloaded file chunks are stored '
                            '(default: %(default)s).'
                        ))
    parser.add_argument('--chunk-max-age', type=int, default=3600,
                        help=(
                            'Maximum age in seconds for cached chunks before they are evicted '
                            'by the maintenance thread (default: 3600). '
                            'Set to 0 to disable chunk eviction.'
                        ))

    return parser.parse_args(args)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    options = parse_args(sys.argv[1:])
    init_logging(options.debug)

    cache_size = options.cache_size * 1024 * 1024
    degoo_email = options.degoo_email
    degoo_pass = options.degoo_pass
    degoo_refresh_token = options.degoo_refresh_token
    degoo_path = options.degoo_path
    refresh_interval = options.refresh_interval * 60
    disable_refresh = options.disable_refresh
    enable_flood_control = options.enable_flood_control
    change_hostname = options.change_hostname
    mode = options.mode
    config_path = options.config_path
    plex_split_file = options.plex_split_file
    download_threads = options.download_threads
    subchunk_connections = options.subchunk_connections
    lookahead_chunks = options.lookahead_chunks
    db_path = options.db_path
    chunk_cache_dir = options.chunk_cache_dir
    chunk_max_age = options.chunk_max_age

    log.debug('##### Initializing Degoo drive (cligoo backend) #####')
    log.debug('Local mount point:      %s', options.mountpoint)
    log.debug('Cache chunk size:       %s MB', str(options.cache_size))
    log.debug('Download threads:       %d', download_threads)
    log.debug('Sub-chunk connections:  %d', subchunk_connections)
    log.debug('Lookahead chunks:       %d', lookahead_chunks)
    log.debug('Tree cache DB:          %s', db_path)
    log.debug('Chunk cache dir:        %s', chunk_cache_dir)
    log.debug('Chunk max age:          %d seconds', chunk_max_age)
    if degoo_email and degoo_pass:
        log.debug('Degoo email:            %s', degoo_email)
        log.debug('Degoo pass:             %s', '*' * len(degoo_pass))
    if degoo_refresh_token:
        log.debug('Degoo refresh token:    %s', '*' * len(degoo_refresh_token[:10]))
    log.debug('Root Degoo path:        %s', degoo_path)
    log.debug('Refresh interval:       %s', 'Disabled' if disable_refresh else str(refresh_interval) + ' seconds')
    log.debug('Flood control:          %s', 'Enabled' if enable_flood_control else 'Disabled')
    if enable_flood_control:
        log.debug('Flood sleep time:       %s seconds', str(options.flood_sleep_time))
        log.debug('Flood max requests:     %s', str(options.flood_max_requests))
        log.debug('Flood time check:       %s minute(s)', str(options.flood_time_to_check))
    log.debug('Change hostname:        %s', 'Disabled' if not change_hostname else DEGOO_HOSTNAME_EU)
    log.debug('Search split files:     %s', 'Enabled' if plex_split_file else 'Disabled')
    log.debug('Mode:                   %s', mode)
    if config_path:
        log.debug('Configuration path:     %s', config_path)

    if config_path:
        import cligoo.config as _cfg_mod
        from pathlib import Path as _Path
        _cfg_mod.CONFIG_DIR = _Path(config_path)
        _cfg_mod.TOML_FILE = _cfg_mod.CONFIG_DIR / 'config.toml'
        _cfg_mod.CONFIG_FILE = _cfg_mod.CONFIG_DIR / 'config.json'

    # ---------------------------------------------------------------------------
    # Initialise the SQLite-backed tree cache
    # ---------------------------------------------------------------------------
    global tree_cache, chunk_cache, degoo_tree_content
    tree_cache = TreeCache(db_path)
    degoo_tree_content = tree_cache
    log.debug(
        'Tree cache opened: %s  (%d entries already cached)',
        db_path, len(tree_cache)
    )

    # ---------------------------------------------------------------------------
    # Initialise the persistent chunk cache (shares the same DB file)
    # ---------------------------------------------------------------------------
    chunk_cache = ChunkCache(db_path=db_path, cache_dir=chunk_cache_dir)
    log.debug(
        'Chunk cache opened: %s  (%d chunks already cached)',
        chunk_cache_dir, len(chunk_cache)
    )

    # Build the shared HTTP session (large connection pool for parallel sub-ranges)
    global _http_session
    _http_session = _make_http_session(
        pool_connections=download_threads,
        pool_maxsize=download_threads * subchunk_connections + 4,
    )

    # Authenticate via cligoo
    global _client
    if degoo_email and degoo_pass:
        log.debug('Logging in with email/password via cligoo...')
        login(degoo_email, degoo_pass)

    _client = DegooClient(debug=options.debug)

    Path(options.mountpoint).mkdir(parents=True, exist_ok=True)

    operations = Operations(
        source=degoo_path,
        cache_size=cache_size,
        flood_sleep_time=options.flood_sleep_time,
        flood_time_to_check=options.flood_time_to_check,
        flood_max_requests=options.flood_max_requests,
        enable_flood_control=enable_flood_control,
        change_hostname=change_hostname,
        mode=mode,
        plex_split_file=plex_split_file,
        chunk_cache=chunk_cache,
        download_threads=download_threads,
        subchunk_connections=subchunk_connections,
        lookahead_chunks=lookahead_chunks,
    )

    log.debug('Reading Degoo content from directory %s', degoo_path)
    operations.load_degoo_content()

    log.debug('Mounting...')
    fuse_options = set(pyfuse3.default_options)
    fuse_options.add('fsname=fusedegoo')

    if options.allow_other:
        if _fuse3_allow_other_permitted():
            fuse_options.add('allow_other')
            log.debug('FUSE allow_other: enabled')
        else:
            log.warning(
                'allow_other requested but user_allow_other is not set in '
                '/etc/fuse3.conf — mounting without allow_other. '
                'Set user_allow_other in /etc/fuse3.conf to enable multi-user access.'
            )

    mimetypes.init()
    if options.debug_fuse:
        fuse_options.add('debug')

    pyfuse3.init(operations, options.mountpoint, fuse_options)

    if not disable_refresh:
        t1 = threading.Thread(target=operations.refresh_degoo_content, args=(refresh_interval,))
        t1.start()

    run_maintenance = None
    if plex_split_file or chunk_max_age > 0:
        run_maintenance = operations.run_maintenance(
            interval=60,
            max_chunk_age=chunk_max_age,
        )

    try:
        trio.run(pyfuse3.main)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.debug('Error: %s', str(e))
        raise e
    finally:
        if run_maintenance is not None:
            run_maintenance.set()
        pyfuse3.close(unmount=True)


if __name__ == '__main__':
    main()
