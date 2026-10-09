"""Thin multi-account wrapper around Telethon.

M1 scope: log in accounts (phone -> code -> optional 2FA), keep them
connected, and hand back session strings so the vault can store them.
Feature modules (anti-delete, ghost, automation) plug into the event
handlers added in later milestones.
"""
from __future__ import annotations

import asyncio
import secrets
from typing import Dict

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession


class PendingLogin:
    def __init__(self, phone: str, client: TelegramClient, phone_code_hash: str):
        self.phone = phone
        self.client = client
        self.phone_code_hash = phone_code_hash


class TelegramManager:
    def __init__(self) -> None:
        self.clients: Dict[str, TelegramClient] = {}
        self.pending: Dict[str, PendingLogin] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ login flow
    async def start_login(self, api_id: int, api_hash: str, phone: str) -> str:
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except FloodWaitError as exc:
            await client.disconnect()
            raise RuntimeError(f"Too many attempts. Try again in {exc.seconds}s.")
        login_id = secrets.token_hex(8)
        self.pending[login_id] = PendingLogin(phone, client, sent.phone_code_hash)
        return login_id

    async def submit_code(self, login_id: str, code: str) -> dict:
        pending = self.pending.get(login_id)
        if not pending:
            raise RuntimeError("This login session expired. Start again.")
        try:
            await pending.client.sign_in(
                pending.phone, code=code, phone_code_hash=pending.phone_code_hash
            )
        except SessionPasswordNeededError:
            return {"need_password": True}
        except PhoneCodeInvalidError:
            raise RuntimeError("Invalid code.")
        except PhoneCodeExpiredError:
            raise RuntimeError("Code expired. Start again.")
        return await self._finish(login_id)

    async def submit_password(self, login_id: str, password: str) -> dict:
        pending = self.pending.get(login_id)
        if not pending:
            raise RuntimeError("This login session expired. Start again.")
        await pending.client.sign_in(password=password)
        return await self._finish(login_id)

    async def _finish(self, login_id: str) -> dict:
        pending = self.pending.pop(login_id)
        try:
            me = await pending.client.get_me()
        finally:
            session = pending.client.session.save()
            await pending.client.disconnect()
        return {
            "need_password": False,
            "session": session,
            "user_id": me.id,
            "username": me.username,
            "first_name": me.first_name,
            "last_name": me.last_name,
            "phone": pending.phone,
        }

    # ------------------------------------------------------------- lifecycle
    async def connect_account(self, account_id: str, session_str: str,
                              api_id: int, api_hash: str) -> TelegramClient:
        existing = self.clients.get(account_id)
        if existing and existing.is_connected():
            return existing
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Session no longer valid")
        # Feature handlers get attached here in later milestones.
        self.clients[account_id] = client
        return client

    def is_connected(self, account_id: str) -> bool:
        client = self.clients.get(account_id)
        return bool(client and client.is_connected())

    async def disconnect_account(self, account_id: str) -> None:
        client = self.clients.pop(account_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def disconnect_all(self) -> None:
        for account_id in list(self.clients.keys()):
            await self.disconnect_account(account_id)
