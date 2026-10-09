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
        # (account_id, peer_id) -> Telethon input entity, filled by get_dialogs
        # so history/send reuse the exact peer without extra lookups.
        self._entities: Dict[tuple, object] = {}

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

    # ------------------------------------------------------------- M3 chats
    def _live_client(self, account_id: str):
        from telethon import TelegramClient  # noqa: F401  (type hint only)

        client = self.clients.get(account_id)
        if not client or not client.is_connected():
            raise RuntimeError("Account is offline")
        return client

    async def get_dialogs(self, account_id: str, limit: int = 30) -> list[dict]:
        """Newest-first conversation list for one connected account."""
        client = self._live_client(account_id)
        limit = max(1, min(int(limit or 30), 50))
        dialogs = await client.get_dialogs(limit=limit)
        out = []
        for d in dialogs:
            try:
                self._entities[(account_id, d.id)] = d.input_entity
            except Exception:
                pass
            last_text, last_date, last_out = "", None, False
            msg = d.message
            if msg is not None:
                last_text = (msg.message or "")[:100]
                if not last_text:
                    last_text = "[media]" if msg.media else "[message]"
                last_date = msg.date.isoformat() if msg.date else None
                last_out = bool(msg.out)
            out.append({
                "peer_id": d.id,
                "title": d.name,
                "unread": d.unread_count,
                "is_user": d.is_user,
                "is_group": d.is_group,
                "is_channel": d.is_channel,
                "last_text": last_text,
                "last_date": last_date,
                "last_out": last_out,
            })
        return out

    async def _resolve_peer(self, account_id: str, peer_id: int):
        key = (account_id, int(peer_id))
        entity = self._entities.get(key)
        if entity is not None:
            return entity
        client = self._live_client(account_id)
        entity = await client.get_entity(int(peer_id))
        self._entities[key] = entity
        return entity

    async def get_history(self, account_id: str, peer_id: int,
                          limit: int = 30, offset_id: int = 0) -> list[dict]:
        """Oldest-first messages for one conversation."""
        client = self._live_client(account_id)
        limit = max(1, min(int(limit or 30), 50))
        entity = await self._resolve_peer(account_id, peer_id)
        messages = await client.get_messages(
            entity, limit=limit, offset_id=int(offset_id or 0)
        )
        out = []
        for m in messages or []:
            if m is None:
                continue
            out.append({
                "id": m.id,
                "text": m.message or ("[media]" if m.media else ""),
                "date": m.date.isoformat() if m.date else None,
                "out": bool(m.out),
                "has_media": m.media is not None,
            })
        out.reverse()
        return out

    async def send_message(self, account_id: str, peer_id: int, text: str) -> dict:
        """Send a text message as one connected account (admin only)."""
        text = (text or "").strip()
        if not text:
            raise RuntimeError("Message is empty")
        if len(text) > 4096:
            raise RuntimeError("Message is too long (max 4096 characters)")
        client = self._live_client(account_id)
        entity = await self._resolve_peer(account_id, peer_id)
        sent = await client.send_message(entity, text)
        return {"id": sent.id, "date": sent.date.isoformat() if sent.date else None}
