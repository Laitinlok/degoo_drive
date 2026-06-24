# Migration: degoo_drive → cligoo backend

This branch replaces the bundled `degoo/__init__.py` API module with
[cligoo](https://github.com/marcomc/cligoo), a maintained CLI/library for
the Degoo GraphQL API.

## Why?

| Concern | bundled `degoo/` | cligoo |
|---|---|---|
| Maintenance | unmaintained | actively maintained |
| Auth | raw token file | TOML config, browser/password login |
| Upload | custom GCS logic | battle-tested multi-worker upload |
| Token refresh | manual | automatic (`auto_relogin`) |
| Config | flat JSON | structured TOML + env-var overrides |

## Installation

```bash
# 1. Install cligoo
pip install git+https://github.com/marcomc/cligoo.git

# 2. Install remaining dependencies
pip install -r requirements.txt

# 3. Login (first time – stores token in ~/.config/cligoo/)
cligoo login            # browser-based
# or
cligoo login --email you@example.com --password secret

# 4. Mount
python fuse_degoo.py --mountpoint /mnt/degoo --degoo-path /
```

## New CLI flags

| Flag | Purpose |
|---|---|
| `--config-path DIR` | Override cligoo config dir (default `~/.config/cligoo/`) |
| *(removed)* `--degoo-email` | Still accepted; calls `cligoo.auth.login_password()` |
| *(removed)* `--degoo-refresh-token` | Removed – cligoo manages token refresh automatically |

## What changed in `fuse_degoo.py`

* `import degoo` → `from cligoo.api import DegooClient, DegooAPIError` + `from cligoo.auth import login_password`
* `degoo.tree()` / `degoo.tree_cache()` → `_client.list_dir()` via `_build_tree_recursive()`
* `degoo.rm()` → `_client.delete([str(file_id)])`
* `degoo.rename()` → `_client.rename(str(inode), name_new)`
* `degoo.mv()` → `_client.move([str(inode)], str(new_parent_id))`
* `degoo.mkdir()` → `_client.mkdir(name, str(parent_id))`
* `degoo.put()` → `_client.upload(source_file, str(parent_id), name=filename)`
* `degoo.get_url_file()` → `_client.get_item(str(item_id))` then HTTP range request
* Authentication → `login_password()` / `DegooClient()` (reads stored token automatically)
