"""Tiny per-install preferences + deleted-message vault for FoggyGramm.

* `data/prefs.json`            -> {ghost: bool, show_deleted: bool}
* `data/deleted/<acct>.json`  -> {peer_id: [{id, text, kind, date, out}, ...]}

All files here are local-only and git-ignored.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULTS = {"ghost": False, "show_deleted": True}
MAX_PER_PEER = 200
MAX_PEERS = 100


def _prefs_file(data_dir: Path) -> Path:
    return Path(data_dir) / "prefs.json"


def load_prefs(data_dir: Path) -> dict:
    prefs = dict(DEFAULTS)
    try:
        raw = json.loads(_prefs_file(data_dir).read_text("utf-8"))
        for key in DEFAULTS:
            if key in raw:
                prefs[key] = bool(raw[key])
    except (OSError, ValueError):
        pass
    return prefs


def save_prefs(data_dir: Path, patch: dict) -> dict:
    prefs = load_prefs(data_dir)
    for key in DEFAULTS:
        if key in patch:
            prefs[key] = bool(patch[key])
    _prefs_file(data_dir).write_text(json.dumps(prefs, indent=2), "utf-8")
    return prefs


def _deleted_file(data_dir: Path, account_id: str) -> Path:
    safe = "".join(c for c in str(account_id) if c.isalnum() or c in "-_")[:48]
    folder = Path(data_dir) / "deleted"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{safe or 'unknown'}.json"


def load_deleted(data_dir: Path, account_id: str) -> dict:
    try:
        data = json.loads(_deleted_file(data_dir, account_id).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def deleted_for(data_dir: Path, account_id: str, peer_id: int) -> list[dict]:
    return load_deleted(data_dir, account_id).get(str(int(peer_id)), [])


def save_deleted(data_dir: Path, account_id: str, peer_id: int, items: list[dict]) -> None:
    if not items:
        return
    store = load_deleted(data_dir, account_id)
    key = str(int(peer_id))
    existing = store.get(key, [])
    seen = {r.get("id") for r in existing}
    for item in items:
        if item.get("id") not in seen:
            existing.append({
                "id": item.get("id"),
                "text": item.get("text", ""),
                "kind": item.get("kind", "text"),
                "date": item.get("date") or int(time.time()),
                "out": bool(item.get("out")),
            })
            seen.add(item.get("id"))
    store[key] = existing[-MAX_PER_PEER:]
    while len(store) > MAX_PEERS:
        store.pop(next(iter(store)))
    _deleted_file(data_dir, account_id).write_text(json.dumps(store), "utf-8")
