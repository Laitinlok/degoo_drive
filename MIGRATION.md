# Migration Guide — cligoo Backend

This document describes how `degoo_drive` was updated to use
[cligoo](https://github.com/marcomc/cligoo) as its Degoo API backend,
replacing the previously bundled `degoo/__init__.py` module.

---

## Why cligoo?

The old bundled `degoo` module is no longer actively maintained and relies
on internal Degoo API details that have drifted. `cligoo` is a
well-maintained, standalone Degoo client library that exposes a clean
`DegooClient` class covering all operations `degoo_drive` needs.

---

## API Call Mapping

| Old (`degoo/__init__.py`) | New (`cligoo.api.DegooClient`) |
|---|---|
| `degoo.DegooConfig(email, password)` | `login(email, password)` from `cligoo.auth` |
| `degoo.tree(dir_id=…, mode=…)` | `_client.list_dir(str(parent_id))` via `_build_tree_recursive()` |
| `degoo.tree_cache(mode=…)` | Same — recursive `list_dir` populates `degoo_tree_content` dict |
| `degoo.rm(file_id)` | `_client.delete([str(file_id)])` |
| `degoo.rename(path_old, name_new)` | `_client.rename(str(inode), name_new)` |
| `degoo.mv(path_old, path)` | `_client.move([str(inode)], str(new_parent_id))` |
| `degoo.mkdir(name, element_id)` | `_client.mkdir(name, str(element_id))` |
| `degoo.put(source, target_path)` | `_client.upload(source, str(parent_id), name=filename)` |
| `degoo.get_url_file(path)` | `_client.get_item(str(item_id))` → HTTP range request |

---

## Key Structural Changes

### `_cligoo_item_to_tree(item, parent_path)`
New adapter that converts a cligoo `list_dir` response dict (which uses
a `Category` int to distinguish folders) into the `degoo_tree_content`
format `degoo_drive` expects (`isFolder` bool, `FilePath` string, etc.).

### `_build_tree_recursive(parent_id, parent_path, mode)`
Replaces the old `degoo.tree_cache()` call. Walks the remote directory
tree via `list_dir` and populates the in-memory cache. Respects `lazy`
vs `full` mode.

### `_fetch_dir_if_needed(dir_id, mode)`
On-demand sub-directory fetch for lazy mode, called by `readdir()`.

### Authentication
If `--degoo-email` / `--degoo-pass` are supplied, `login()` from
`cligoo.auth` is called before constructing `DegooClient`. Otherwise,
`DegooClient` reads the token cligoo stored at `~/.config/cligoo/`
automatically. The `--config-path` flag overrides this directory.

### `requirements.txt`
Removed: `PyJWT`, `appdirs`, `wget`, `clint`, `humanfriendly`, `humanize`
(these were used by the old bundled module).  
Added: `cligoo>=1.0.0`.

---

## Installation

```bash
# 1. Install cligoo
pip install git+https://github.com/marcomc/cligoo.git

# 2. Login once (stores token in ~/.config/cligoo/)
cligoo login
# or: cligoo login --email you@example.com --password secret

# 3. Install remaining FUSE driver deps
pip install -r requirements.txt

# 4. Mount
python fuse_degoo.py --mountpoint /mnt/degoo --degoo-path /
```

---

## New CLI Flags

| Flag | Description |
|---|---|
| `--config-path PATH` | Path to cligoo config directory (default: `~/.config/cligoo/`) |
| `--download-threads N` | Parallel chunk-download threads (default: 8) |
| `--subchunk-connections N` | Parallel TCP connections per chunk for higher throughput (default: 8) |
| `--lookahead-chunks N` | Chunks to pre-fetch ahead of the read pointer (default: 2) |
