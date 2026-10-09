"""M2: local encrypted backups + sync-folder transport for FoggyGramm.

Everything handled here is ciphertext or non-secret metadata, so it is safe
to point Syncthing / Drive at the sync folder and to keep local backups.

Layout
------
* `data/backups/<backup_id>/` -> vault.json + accounts.enc + manifest.json
* `data/sync/`                -> vault.json + accounts.enc + manifest.json
                                (this is the folder you sync between devices)
* `data/settings.json`        -> backup mode, keep-limit, device name

Rules
-----
* Restoring a backup or importing from the sync folder NEVER deletes
  anything silently: a safety backup of the current vault is taken first.
* Sync conflicts resolve as last-write-wins by file time, but the loser is
  always preserved as a safety backup, so nothing is ever lost.
"""
from __future__ import annotations

import hashlib
import json
import platform
import secrets
import shutil
import time
from pathlib import Path

BACKUP_MODES = ("manual", "on-change", "daily")
DAILY_INTERVAL = 24 * 3600
SAFETY_PREFIX = ("pre-restore-", "pre-import-")


def _now() -> int:
    return int(time.time())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class BackupError(Exception):
    pass


class BackupManager:
    def __init__(self, data_dir: Path, sync_dir: Path, vault=None):
        self.data_dir = Path(data_dir)
        self.sync_dir = Path(sync_dir)
        self.backups_dir = self.data_dir / "backups"
        self.settings_file = self.data_dir / "settings.json"
        self.vault = vault  # optional; used for init/unlock guards
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------- settings
    def _default_settings(self) -> dict:
        return {"backup_mode": "manual", "keep": 10, "device": platform.node() or "foggygramm"}

    def get_settings(self) -> dict:
        if not self.settings_file.exists():
            return self._default_settings()
        try:
            settings = json.loads(self.settings_file.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return self._default_settings()
        defaults = self._default_settings()
        defaults.update({k: v for k, v in settings.items() if k in defaults})
        return defaults

    def save_settings(self, mode: str, keep: int) -> dict:
        if mode not in BACKUP_MODES:
            raise BackupError(f"Unknown backup mode: {mode}")
        keep = max(1, min(int(keep), 30))
        settings = self.get_settings()
        settings["backup_mode"] = mode
        settings["keep"] = keep
        self.settings_file.write_text(json.dumps(settings, indent=2), "utf-8")
        return settings

    # ---------------------------------------------------------------- guards
    def _require_ready(self) -> None:
        if self.vault is not None:
            if not self.vault.is_initialized():
                raise BackupError("Vault is not set up yet")
            if not self.vault.is_unlocked:
                raise BackupError("Vault is locked")
        meta, enc = self._vault_files()
        if not meta.exists() or not enc.exists():
            raise BackupError("Vault files are missing")

    def _vault_files(self) -> tuple[Path, Path]:
        return self.data_dir / "vault.json", self.data_dir / "accounts.enc"

    # ---------------------------------------------------------------- backups
    def _counts(self) -> dict:
        # Counts are best-effort (the store is encrypted); -1 means unknown.
        if self.vault is not None and self.vault.is_unlocked:
            try:
                accounts = self.vault.list_accounts()
                return {"accounts": len(accounts)}
            except BackupError:
                pass
            except Exception:
                pass
        return {"accounts": -1}

    def create_backup(self, label: str = "", mode: str = "manual") -> dict:
        self._require_ready()
        meta_file, enc_file = self._vault_files()
        backup_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        dest = self.backups_dir / backup_id
        dest.mkdir(parents=True, exist_ok=False)
        shutil.copy2(meta_file, dest / "vault.json")
        shutil.copy2(enc_file, dest / "accounts.enc")
        manifest = {
            "id": backup_id,
            "created_at": _now(),
            "label": (label or "").strip() or backup_id,
            "mode": mode,
            "device": self.get_settings()["device"],
            **self._counts(),
        }
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
        self._prune()
        return manifest

    def list_backups(self) -> list[dict]:
        out = []
        if not self.backups_dir.exists():
            return out
        for child in self.backups_dir.iterdir():
            manifest_file = child / "manifest.json"
            if child.is_dir() and manifest_file.exists():
                try:
                    out.append(json.loads(manifest_file.read_text("utf-8")))
                except (json.JSONDecodeError, OSError):
                    continue
        out.sort(key=lambda m: m.get("created_at", 0), reverse=True)
        return out

    def _prune(self) -> None:
        keep = self.get_settings()["keep"]
        for manifest in self.list_backups()[keep:]:
            self.delete_backup(manifest["id"])

    def delete_backup(self, backup_id: str) -> None:
        target = self.backups_dir / backup_id
        if not target.is_dir():
            raise BackupError("Backup not found")
        shutil.rmtree(target)

    def restore_backup(self, backup_id: str) -> dict:
        self._require_ready()
        src = self.backups_dir / backup_id
        if not src.is_dir() or not (src / "accounts.enc").exists():
            raise BackupError("Backup not found")
        self.create_backup(label=f"pre-restore-{time.strftime('%Y%m%d-%H%M%S')}", mode="safety")
        meta_file, enc_file = self._vault_files()
        shutil.copy2(src / "vault.json", meta_file)
        shutil.copy2(src / "accounts.enc", enc_file)
        return {"ok": True, "restored": backup_id}

    # ------------------------------------------------------------ auto modes
    def auto_backup_on_change(self) -> None:
        """Called after every vault write; only acts in on-change mode."""
        try:
            if self.get_settings()["backup_mode"] != "on-change":
                return
            self.create_backup(label="auto (on change)", mode="on-change")
        except BackupError:
            pass

    def daily_check(self) -> bool:
        """Called by the background loop; returns True if a backup was made."""
        try:
            if self.get_settings()["backup_mode"] != "daily":
                return False
            if self.vault is not None and (
                not self.vault.is_initialized() or not self.vault.is_unlocked
            ):
                return False
            backups = self.list_backups()
            if backups and _now() - backups[0].get("created_at", 0) < DAILY_INTERVAL:
                return False
            self.create_backup(label="auto (daily)", mode="daily")
            return True
        except BackupError:
            return False

    # ------------------------------------------------------------------- sync
    def export_sync(self) -> dict:
        """Copy the current vault into the sync folder for other devices."""
        self._require_ready()
        meta_file, enc_file = self._vault_files()
        shutil.copy2(meta_file, self.sync_dir / "vault.json")
        shutil.copy2(enc_file, self.sync_dir / "accounts.enc")
        manifest = {
            "exported_at": _now(),
            "device": self.get_settings()["device"],
            **self._counts(),
        }
        (self.sync_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), "utf-8"
        )
        return manifest

    def _export_manifest(self) -> dict | None:
        manifest_file = self.sync_dir / "manifest.json"
        if not manifest_file.exists():
            return None
        try:
            return json.loads(manifest_file.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def sync_status(self) -> dict:
        manifest = self._export_manifest()
        has_export = bool(
            manifest
            and (self.sync_dir / "vault.json").exists()
            and (self.sync_dir / "accounts.enc").exists()
        )
        status: dict = {
            "has_export": has_export,
            "exported_at": manifest.get("exported_at") if manifest else None,
            "device": manifest.get("device") if manifest else None,
            "export_accounts": manifest.get("accounts") if manifest else None,
            "local_accounts": self._counts()["accounts"],
            "local_newer": None,
            "differs": None,
        }
        if not has_export:
            return status
        try:
            local_meta, local_enc = self._vault_files()
            if local_meta.exists() and local_enc.exists():
                local_mtime = max(local_meta.stat().st_mtime, local_enc.stat().st_mtime)
                export_mtime = max(
                    (self.sync_dir / "vault.json").stat().st_mtime,
                    (self.sync_dir / "accounts.enc").stat().st_mtime,
                )
                status["local_newer"] = local_mtime > export_mtime
                local_hash = _sha256(local_meta) + _sha256(local_enc)
                export_hash = _sha256(self.sync_dir / "vault.json") + _sha256(
                    self.sync_dir / "accounts.enc"
                )
                status["differs"] = local_hash != export_hash
        except OSError:
            pass
        return status

    def import_sync(self) -> dict:
        """Bring the sync folder's vault in; current vault kept as safety backup."""
        self._require_ready()
        if not (self.sync_dir / "vault.json").exists() or not (
            self.sync_dir / "accounts.enc"
        ).exists():
            raise BackupError("Sync folder is empty — export first")
        self.create_backup(label=f"pre-import-{time.strftime('%Y%m%d-%H%M%S')}", mode="safety")
        meta_file, enc_file = self._vault_files()
        shutil.copy2(self.sync_dir / "vault.json", meta_file)
        shutil.copy2(self.sync_dir / "accounts.enc", enc_file)
        return {"ok": True}
