"""Filesystem layout + Telegram API credentials for FoggyGramm.

Everything lives inside the project folder so the encrypted vault can be
synced to other devices as a single folder (ciphertext only).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"

# The "sync" folder is what you point Syncthing / Drive at. It only ever
# contains encrypted material (accounts.enc) plus non-secret metadata.
SYNC_DIR = DATA_DIR / "sync"

# Downloaded chat media + profile photos (per-install cache, git-ignored).
MEDIA_DIR = DATA_DIR / "media"
AVATARS_DIR = DATA_DIR / "avatars"

DATA_DIR.mkdir(parents=True, exist_ok=True)
SYNC_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
AVATARS_DIR.mkdir(parents=True, exist_ok=True)


def _load_api_credentials() -> tuple[int | None, str | None]:
    """Return the Telegram (api_id, api_hash) for this install.

    Order of precedence:
      1. environment variables TG_API_ID / TG_API_HASH
      2. data/telegram_api.json  {"api_id": ..., "api_hash": "..."}

    Each person running FoggyGramm supplies their own keys (from
    https://my.telegram.org) at setup; they live only on that machine and
    are never part of the published code.
    """
    env_id = os.environ.get("TG_API_ID")
    env_hash = os.environ.get("TG_API_HASH")
    if env_id and env_hash:
        return int(env_id), env_hash.strip()

    creds_file = DATA_DIR / "telegram_api.json"
    if creds_file.exists():
        data = json.loads(creds_file.read_text("utf-8"))
        return int(data["api_id"]), str(data["api_hash"]).strip()

    return None, None


def save_api_credentials(api_id: int, api_hash: str) -> tuple[int, str]:
    """Validate and persist this install's Telegram API credentials."""
    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        raise ValueError("API ID must be a number.")
    if api_id <= 0:
        raise ValueError("API ID must be a number.")
    api_hash = str(api_hash or "").strip()
    if not api_hash:
        raise ValueError("API Hash is required.")
    creds_file = DATA_DIR / "telegram_api.json"
    creds_file.write_text(
        json.dumps({"api_id": api_id, "api_hash": api_hash}, indent=2), "utf-8"
    )
    return api_id, api_hash


API_ID, API_HASH = _load_api_credentials()
