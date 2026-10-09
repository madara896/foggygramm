"""Encrypted vault: stores Telegram sessions behind one admin password.

Design
------
* `vault.json`      -> NON-secret metadata (salt, KDF params, admin username,
                       a verification token). Safe to sync.
* `accounts.enc`    -> Fernet-encrypted JSON (accounts). Ciphertext
                       only, so it is safe to sync through a cloud folder.

The encryption key is derived from the admin password with PBKDF2-SHA256.
A session string is equivalent to full control of an account, so it is never
written to disk unencrypted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

PBKDF2_ITERATIONS = 600_000


class VaultError(Exception):
    pass


def _empty_store() -> dict:
    return {"accounts": {}, "users": {}}


class Vault:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.meta_file = self.data_dir / "vault.json"
        self.accounts_file = self.data_dir / "accounts.enc"
        self._fernet: Optional[Fernet] = None

    # ------------------------------------------------------------------ meta
    def _read_meta(self) -> dict:
        if not self.meta_file.exists():
            return {}
        return json.loads(self.meta_file.read_text("utf-8"))

    def _write_meta(self, meta: dict) -> None:
        self.meta_file.write_text(json.dumps(meta, indent=2), "utf-8")

    def is_initialized(self) -> bool:
        return bool(self._read_meta().get("initialized"))

    @property
    def is_unlocked(self) -> bool:
        return self._fernet is not None

    @property
    def admin_username(self) -> Optional[str]:
        return self._read_meta().get("admin_username")

    # ------------------------------------------------------------------- kdf
    @staticmethod
    def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations, dklen=32
        )

    @staticmethod
    def _fernet_from_key(key: bytes) -> Fernet:
        return Fernet(base64.urlsafe_b64encode(key))

    # --------------------------------------------------------------- setup
    def initialize(self, admin_username: str, admin_password: str) -> None:
        if self.is_initialized():
            raise VaultError("Vault is already initialized")
        if not admin_username.strip():
            raise VaultError("Admin username is required")
        if len(admin_password) < 8:
            raise VaultError("Admin password must be at least 8 characters")

        salt = secrets.token_bytes(16)
        key = self._derive_key(admin_password, salt, PBKDF2_ITERATIONS)
        f = self._fernet_from_key(key)

        meta = {
            "initialized": True,
            "created_at": int(time.time()),
            "kdf": {
                "algo": "pbkdf2_sha256",
                "iterations": PBKDF2_ITERATIONS,
                "salt": salt.hex(),
            },
            "admin_username": admin_username.strip(),
            "check": f.encrypt(b"vault-check-ok").decode("utf-8"),
        }
        self._write_meta(meta)
        self._fernet = f
        self._write_store(_empty_store())

    def unlock(self, admin_username: str, admin_password: str) -> bool:
        meta = self._read_meta()
        if not meta.get("initialized"):
            raise VaultError("Vault is not initialized")
        if meta.get("admin_username") != admin_username:
            return False
        salt = bytes.fromhex(meta["kdf"]["salt"])
        key = self._derive_key(admin_password, salt, meta["kdf"]["iterations"])
        f = self._fernet_from_key(key)
        try:
            if f.decrypt(meta["check"].encode("utf-8")) != b"vault-check-ok":
                return False
        except InvalidToken:
            return False
        self._fernet = f
        return True

    def lock(self) -> None:
        self._fernet = None

    # ------------------------------------------------------------- storage
    def _write_store(self, store: dict) -> None:
        if self._fernet is None:
            raise VaultError("Vault is locked")
        blob = json.dumps(store).encode("utf-8")
        self.accounts_file.write_bytes(self._fernet.encrypt(blob))

    def _read_store(self) -> dict:
        if self._fernet is None:
            raise VaultError("Vault is locked")
        if not self.accounts_file.exists():
            return _empty_store()
        try:
            blob = self._fernet.decrypt(self.accounts_file.read_bytes())
        except InvalidToken:
            raise VaultError("Could not decrypt vault (wrong password or corrupt file)")
        store = json.loads(blob.decode("utf-8"))
        store.setdefault("accounts", {})
        store.setdefault("users", {})
        return store

    # ------------------------------------------------------------ accounts
    def list_accounts(self) -> list[dict]:
        store = self._read_store()
        return [
            {k: v for k, v in acc.items() if k != "session"}
            for acc in store["accounts"].values()
        ]

    def get_account_session(self, account_id: str) -> str:
        store = self._read_store()
        acc = store["accounts"].get(account_id)
        if not acc:
            raise VaultError("Account not found")
        return acc["session"]

    def save_account(self, data: dict) -> dict:
        store = self._read_store()
        account_id = data.get("id") or secrets.token_hex(6)
        record = dict(data)
        record["id"] = account_id
        record.setdefault("added_at", int(time.time()))
        store["accounts"][account_id] = record
        self._write_store(store)
        return {k: v for k, v in record.items() if k != "session"}

    def delete_account(self, account_id: str) -> None:
        store = self._read_store()
        store["accounts"].pop(account_id, None)
        self._write_store(store)
