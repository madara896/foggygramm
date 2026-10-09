"""FoggyGramm local server (open source, self-hosted).

Single-admin app: setup, login, account add (phone -> code -> 2FA),
account list/restore, encrypted backups (manual/on-change/daily),
sync-folder export/import, and chats (list/history/send).

Run:  python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .backup import BackupError, BackupManager
from .config import API_HASH, API_ID, AVATARS_DIR, DATA_DIR, FRONTEND_DIR, MEDIA_DIR, SYNC_DIR, save_api_credentials
from .prefs import load_prefs, save_prefs
from .telegram_manager import TelegramManager
from .vault import Vault, VaultError

app = FastAPI(title="FoggyGramm")
vault = Vault(DATA_DIR)
manager = TelegramManager(DATA_DIR)
backups = BackupManager(DATA_DIR, SYNC_DIR, vault)

# In-memory login tokens (cookie -> {username, role}). Cleared on restart.
SESSIONS: dict[str, dict] = {}
COOKIE = "foggygramm_session"


# --------------------------------------------------------------------- models
class SetupBody(BaseModel):
    api_id: int
    api_hash: str
    admin_username: str
    admin_password: str


class LoginBody(BaseModel):
    username: str
    password: str


class StartLoginBody(BaseModel):
    phone: str


class VerifyBody(BaseModel):
    login_id: str
    code: str


class VerifyPasswordBody(BaseModel):
    login_id: str
    password: str


class BackupSettingsBody(BaseModel):
    mode: str
    keep: int = 10


class BackupNowBody(BaseModel):
    label: str = ""


class BackupRestoreBody(BaseModel):
    id: str


class SendBody(BaseModel):
    account_id: str
    peer_id: int
    text: str
    reply_to: Optional[int] = None


class PrefsBody(BaseModel):
    ghost: Optional[bool] = None
    show_deleted: Optional[bool] = None


class ReadBody(BaseModel):
    account_id: str
    peer_id: int


class MuteBody(BaseModel):
    account_id: str
    peer_id: int
    mute: bool


# -------------------------------------------------------------------- helpers
def current_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(COOKIE)
    return SESSIONS.get(token) if token else None


def require_login(request: Request) -> dict:
    session = current_session(request)
    if not session:
        raise HTTPException(401, "Login required")
    return session


def require_admin(request: Request) -> dict:
    session = current_session(request)
    if not session or session["role"] != "admin":
        raise HTTPException(401, "Admin login required")
    return session


def _public_account(account: dict) -> dict:
    account["connected"] = manager.is_connected(account["id"])
    return account


async def connect_all_accounts() -> None:
    for account in vault.list_accounts():
        if manager.is_connected(account["id"]):
            continue
        try:
            session = vault.get_account_session(account["id"])
            await manager.connect_account(account["id"], session, API_ID, API_HASH)
            print(f"[foggygramm] connected: {account.get('label')}")
        except Exception as exc:  # noqa: BLE001
            print(f"[foggygramm] could not connect {account.get('label')}: {exc}")


def _persist_new_account(result: dict) -> dict:
    label = (
        " ".join(filter(None, [result.get("first_name"), result.get("last_name")])).strip()
        or result.get("username")
        or result["phone"]
    )
    saved = vault.save_account({
        "label": label,
        "phone": result["phone"],
        "session": result["session"],
        "user_id": result["user_id"],
        "username": result.get("username"),
        "first_name": result.get("first_name"),
        "last_name": result.get("last_name"),
    })
    backups.auto_backup_on_change()
    asyncio.create_task(_connect_one(saved["id"]))
    return saved


async def _connect_one(account_id: str) -> None:
    try:
        session = vault.get_account_session(account_id)
        await manager.connect_account(account_id, session, API_ID, API_HASH)
    except Exception as exc:  # noqa: BLE001
        print(f"[foggygramm] connect failed for {account_id}: {exc}")


def _require_unlocked() -> None:
    if not vault.is_unlocked:
        raise HTTPException(403, "Vault locked")


async def _after_vault_replace() -> None:
    """Account list may have changed: reconnect everything."""
    await manager.disconnect_all()
    asyncio.create_task(connect_all_accounts())


@app.on_event("startup")
async def _background_loops() -> None:
    async def daily() -> None:
        while True:
            try:
                if backups.daily_check():
                    print("[foggygramm] daily auto-backup created")
            except Exception as exc:  # noqa: BLE001
                print(f"[foggygramm] daily backup check failed: {exc}")
            await asyncio.sleep(1800)

    async def ghost() -> None:
        # Re-assert offline status: any Telegram activity can flip it back.
        while True:
            try:
                if load_prefs(DATA_DIR).get("ghost"):
                    await manager.set_ghost(True)
            except Exception as exc:  # noqa: BLE001
                print(f"[foggygramm] ghost re-apply failed: {exc}")
            await asyncio.sleep(60)

    asyncio.create_task(daily())
    asyncio.create_task(ghost())


# ------------------------------------------------------------------ endpoints
@app.get("/api/status")
def status():
    return {
        "initialized": vault.is_initialized(),
        "unlocked": vault.is_unlocked,
        "api_configured": bool(API_ID and API_HASH),
    }


@app.post("/api/setup")
def setup(body: SetupBody):
    global API_ID, API_HASH
    if vault.is_initialized():
        raise HTTPException(400, "Already set up")
    try:
        API_ID, API_HASH = save_api_credentials(body.api_id, body.api_hash)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        vault.initialize(body.admin_username, body.admin_password)
    except VaultError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/login")
async def login(body: LoginBody, response: Response):
    if not vault.is_initialized():
        raise HTTPException(400, "Not set up yet")

    if body.username != vault.admin_username or not vault.unlock(body.username, body.password):
        raise HTTPException(401, "Wrong username or password")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"username": body.username, "role": "admin"}
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax")
    asyncio.create_task(connect_all_accounts())
    return {"ok": True, "role": "admin"}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        SESSIONS.pop(token, None)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    session = require_login(request)
    return {"username": session["username"], "role": session["role"]}


@app.get("/api/accounts")
def accounts(request: Request):
    require_login(request)
    return {"accounts": [_public_account(a) for a in vault.list_accounts()]}


@app.post("/api/accounts/start")
async def account_start(body: StartLoginBody, request: Request):
    require_admin(request)
    if not vault.is_unlocked:
        raise HTTPException(403, "Vault locked")
    try:
        login_id = await manager.start_login(API_ID, API_HASH, body.phone.strip())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return {"login_id": login_id}


@app.post("/api/accounts/verify")
async def account_verify(body: VerifyBody, request: Request):
    require_admin(request)
    try:
        result = await manager.submit_code(body.login_id, body.code.strip())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    if result.get("need_password"):
        return {"need_password": True}
    return {"ok": True, "account": _persist_new_account(result)}


@app.post("/api/accounts/verify_password")
async def account_verify_password(body: VerifyPasswordBody, request: Request):
    require_admin(request)
    try:
        result = await manager.submit_password(body.login_id, body.password)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return {"ok": True, "account": _persist_new_account(result)}


@app.delete("/api/accounts/{account_id}")
async def account_delete(account_id: str, request: Request):
    require_admin(request)
    vault.delete_account(account_id)
    backups.auto_backup_on_change()
    await manager.disconnect_account(account_id)
    return {"ok": True}


# ------------------------------------------------- M2 backup & sync endpoints
@app.get("/api/backup/settings")
def backup_settings(request: Request):
    require_admin(request)
    return backups.get_settings()


@app.post("/api/backup/settings")
def backup_save_settings(body: BackupSettingsBody, request: Request):
    require_admin(request)
    _require_unlocked()
    try:
        return backups.save_settings(body.mode, body.keep)
    except BackupError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/backup/now")
def backup_now(body: BackupNowBody, request: Request):
    require_admin(request)
    _require_unlocked()
    try:
        return backups.create_backup(label=body.label)
    except BackupError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/backup/list")
def backup_list(request: Request):
    require_admin(request)
    return {"backups": backups.list_backups()}


@app.post("/api/backup/restore")
async def backup_restore(body: BackupRestoreBody, request: Request):
    require_admin(request)
    _require_unlocked()
    try:
        result = backups.restore_backup(body.id)
    except BackupError as exc:
        raise HTTPException(400, str(exc))
    await _after_vault_replace()
    return result


@app.delete("/api/backup/{backup_id}")
def backup_delete(backup_id: str, request: Request):
    require_admin(request)
    try:
        backups.delete_backup(backup_id)
    except BackupError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/sync/export")
def sync_export(request: Request):
    require_admin(request)
    _require_unlocked()
    try:
        return backups.export_sync()
    except BackupError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/sync/status")
def sync_status(request: Request):
    require_admin(request)
    return backups.sync_status()


@app.post("/api/sync/import")
async def sync_import(request: Request):
    require_admin(request)
    _require_unlocked()
    try:
        result = backups.import_sync()
    except BackupError as exc:
        raise HTTPException(400, str(exc))
    await _after_vault_replace()
    return result


# ------------------------------------------------------- M3 chat endpoints
@app.get("/api/chats")
async def chats(account_id: str, request: Request, limit: int = 30):
    require_admin(request)
    try:
        return {"dialogs": await manager.get_dialogs(account_id, limit)}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/chats/history")
async def chat_history(account_id: str, peer_id: int, request: Request,
                       limit: int = 30, offset_id: int = 0):
    require_admin(request)
    try:
        return {"messages": await manager.get_history(account_id, peer_id, limit, offset_id)}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/chats/send")
async def chat_send(body: SendBody, request: Request):
    require_admin(request)
    _require_unlocked()
    try:
        return await manager.send_message(
            body.account_id, body.peer_id, body.text, body.reply_to)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/chats/send_file")
async def chat_send_file(request: Request, account_id: str = Form(...),
                         peer_id: int = Form(...), caption: str = Form(""),
                         reply_to: int = Form(0), file: UploadFile = File(...)):
    require_admin(request)
    _require_unlocked()
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    try:
        return await manager.send_file(
            account_id, peer_id, data, file.filename or "file", caption,
            reply_to or None)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/chats/read")
async def chat_read(body: ReadBody, request: Request):
    require_admin(request)
    try:
        await manager.mark_read(body.account_id, body.peer_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/chats/mute")
async def chat_mute(body: MuteBody, request: Request):
    require_admin(request)
    try:
        return await manager.set_muted(body.account_id, body.peer_id, body.mute)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/chats/export")
async def chat_export(account_id: str, peer_id: int, request: Request,
                      limit: int = 200):
    require_admin(request)
    try:
        text = await manager.export_history(account_id, peer_id, limit)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    return PlainTextResponse(
        text, headers={"Content-Disposition": f"attachment; filename=chat-{peer_id}.txt"})


@app.get("/api/chats/resolve")
async def chat_resolve(account_id: str, username: str, request: Request):
    require_admin(request)
    try:
        return await manager.resolve_username(account_id, username)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/chats/presence")
async def chat_presence(account_id: str, peer_id: int, request: Request):
    require_admin(request)
    try:
        info = await manager.get_presence(account_id, peer_id)
        try:
            info["muted"] = await manager.is_muted(account_id, peer_id)
        except RuntimeError:
            pass
        return info
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/contacts")
async def contacts(account_id: str, request: Request):
    require_admin(request)
    try:
        return {"contacts": await manager.get_contacts(account_id)}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/updates")
async def updates(account_id: str, request: Request, cursor: int = 0):
    """Long-poll real-time events (messages, deletes, typing)."""
    require_admin(request)
    deadline = time.time() + 20
    while time.time() < deadline:
        events, new_cursor = manager.poll_updates(account_id, cursor)
        if events:
            return {"events": events, "cursor": new_cursor}
        await asyncio.sleep(0.5)
    return {"events": [], "cursor": cursor}


@app.get("/api/prefs")
def prefs_get(request: Request):
    require_admin(request)
    return load_prefs(DATA_DIR)


@app.post("/api/prefs")
async def prefs_set(body: PrefsBody, request: Request):
    require_admin(request)
    _require_unlocked()
    patch = {k: v for k, v in
             {"ghost": body.ghost, "show_deleted": body.show_deleted}.items()
             if v is not None}
    prefs = save_prefs(DATA_DIR, patch)
    if "ghost" in patch:
        await manager.set_ghost(patch["ghost"])
    return prefs


@app.get("/api/media")
async def chat_media(account_id: str, peer_id: int, msg_id: int, request: Request):
    require_admin(request)
    try:
        path, mime = await manager.download_media(account_id, peer_id, msg_id, MEDIA_DIR)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc))
    path = Path(path)
    return FileResponse(path, media_type=mime, filename=path.name,
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/photo")
async def chat_photo(account_id: str, peer_id: int, request: Request):
    require_admin(request)
    try:
        path = await manager.download_photo(account_id, peer_id, AVATARS_DIR)
    except RuntimeError as exc:
        raise HTTPException(404, str(exc))
    return FileResponse(Path(path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})


# ------------------------------------------------------------------ frontend
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
