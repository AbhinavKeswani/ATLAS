"""Hand-editable user configuration: theme, rail icons, widget layout, intro.

The single source of truth is a real JSON file (`USER_CONFIG`, default
~/Library/Application Support/Atlas/user_config.json) so the user can edit it in
any text editor. The server:

  - loads it on boot (migrating the legacy SQLite `layout_state` setting into it
    the first time, so existing layouts survive),
  - writes it atomically on every PUT /api/config,
  - polls its mtime (2s) and broadcasts `config_changed` over the WS bus when the
    file is edited externally — the UI hot-reloads theme/layout live.

Schema (all keys optional; defaults fill in):
{
  "theme":  { "preset": "emerald", "custom": null | {"c1": "#..", "c2": "#..", "c3": "#.."} },
  "intro":  "always" | "session" | "off",
  "icons":  { "<view>": "<icon-key>" },
  "layout": { "live": {"order": {...}, "span": {...}}, "presets": {...}, "active": "Default" }
}
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import time
from typing import Any

from .config import USER_CONFIG
from .store import Store

log = logging.getLogger("atlas.userconfig")

DEFAULTS: dict[str, Any] = {
    "theme": {"preset": "emerald", "custom": None},
    "intro": "always",
    "icons": {},
    "layout": {"live": {"order": {}, "span": {}}, "presets": {}, "active": "Default"},
}

_state: dict[str, Any] = {}
_mtime: float | None = None


def _merged(raw: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULTS)
    for k, v in (raw or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def load(store: Store | None = None) -> dict[str, Any]:
    """Read the config file (migrating legacy SQLite layout on first boot)."""
    global _state, _mtime
    if USER_CONFIG.exists():
        try:
            _state = _merged(json.loads(USER_CONFIG.read_text(encoding="utf-8")))
            _mtime = USER_CONFIG.stat().st_mtime
            return _state
        except (json.JSONDecodeError, OSError) as e:
            # Never clobber a file the user is mid-editing; serve last-good/defaults.
            log.warning("user_config.json unreadable (%s) — using last-known state", e)
            return _state or copy.deepcopy(DEFAULTS)

    # First boot: seed from the legacy SQLite layout_state so nothing is lost.
    seed = copy.deepcopy(DEFAULTS)
    if store is not None:
        legacy = store.get_setting("layout_state", None)
        if isinstance(legacy, dict):
            seed["layout"] = {
                "live": legacy.get("live") or {"order": {}, "span": {}},
                "presets": legacy.get("presets") or {},
                "active": legacy.get("active") or "Default",
            }
            seed["icons"] = legacy.get("icons") or {}
            log.info("migrated legacy layout_state into user_config.json")
    _state = seed
    save(seed)
    return _state


def save(cfg: dict[str, Any]) -> dict[str, Any]:
    """Atomically write the config file (temp file + rename) and refresh state."""
    global _state, _mtime
    _state = _merged(cfg)
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(USER_CONFIG.parent), prefix=".user_config.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2)
            f.write("\n")
        os.replace(tmp, USER_CONFIG)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _mtime = USER_CONFIG.stat().st_mtime
    return _state


def patch(partial: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge a partial update (top-level keys replaced; dicts merged one level)."""
    cur = copy.deepcopy(_state or DEFAULTS)
    for k, v in (partial or {}).items():
        if k in cur and isinstance(cur[k], dict) and isinstance(v, dict):
            cur[k] = {**cur[k], **v}
        else:
            cur[k] = v
    return save(cur)


def current() -> dict[str, Any]:
    return _state or copy.deepcopy(DEFAULTS)


async def watch(bus, poll_s: float = 2.0) -> None:
    """Poll the file's mtime; on external edit, reload and broadcast config_changed."""
    global _mtime
    while True:
        await asyncio.sleep(poll_s)
        try:
            m = USER_CONFIG.stat().st_mtime if USER_CONFIG.exists() else None
        except OSError:
            continue
        if m is not None and _mtime is not None and m > _mtime + 1e-6:
            log.info("user_config.json changed on disk — hot-reloading")
            load()
            try:
                bus.publish("config_changed", {"at": time.time()})
            except Exception:  # noqa: BLE001 - watcher must never die
                pass
