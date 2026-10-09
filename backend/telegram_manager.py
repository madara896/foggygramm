"""Thin multi-account wrapper around Telethon.

M1 scope: log in accounts (phone -> code -> optional 2FA), keep them
connected, and hand back session strings so the vault can store them.
Feature modules (anti-delete, ghost, automation) plug into the event
handlers added in later milestones.
"""
from __future__ import annotations

import asyncio
import mimetypes
import secrets
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple

from telethon import TelegramClient, events, utils
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl import functions
from telethon.tl.types import (
    Channel,
    Chat,
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaPhoto,
    PeerNotifySettings,
    SendMessageCancelAction,
    UpdateChatUserTyping,
    UpdateUserTyping,
    User,
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

from .prefs import load_prefs, save_deleted as _persist_deleted


class PendingLogin:
    def __init__(self, phone: str, client: TelegramClient, phone_code_hash: str):
        self.phone = phone
        self.client = client
        self.phone_code_hash = phone_code_hash


class TelegramManager:
    def __init__(self, data_dir=None) -> None:
        self.clients: Dict[str, TelegramClient] = {}
        self.pending: Dict[str, PendingLogin] = {}
        self._lock = asyncio.Lock()
        # (account_id, peer_id) -> Telethon input entity, filled by get_dialogs
        # so history/send reuse the exact peer without extra lookups.
        self._entities: Dict[tuple, object] = {}
        # Real-time engine: per-account event queue + message cache.
        self._updates: Dict[str, list] = {}
        self._seq: Dict[str, int] = {}
        self._msg_cache: Dict[tuple, list] = {}
        self._data_dir = Path(data_dir) if data_dir else None

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
        # Real-time engine: live message / delete / typing events.
        client.add_event_handler(
            self._handle_new_message(account_id), events.NewMessage)
        client.add_event_handler(
            self._handle_deleted(account_id), events.MessageDeleted)
        client.add_event_handler(
            self._handle_user_update(account_id), events.UserUpdate)
        self.clients[account_id] = client
        await self._apply_ghost(account_id)
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

    # ------------------------------------------------------- real-time engine
    def _push_update(self, account_id: str, payload: dict) -> None:
        dq = self._updates.setdefault(account_id, [])
        seq = self._seq.get(account_id, 0) + 1
        self._seq[account_id] = seq
        payload = dict(payload)
        payload["i"] = seq
        dq.append(payload)
        del dq[:-300]

    def poll_updates(self, account_id: str, cursor: int) -> tuple:
        try:
            cursor = int(cursor or 0)
        except (TypeError, ValueError):
            cursor = 0
        evs = [e for e in self._updates.get(account_id, []) if e.get("i", 0) > cursor]
        return evs, (evs[-1]["i"] if evs else cursor)

    def _cache_message(self, account_id: str, peer: int, record: dict) -> None:
        key = (account_id, int(peer))
        cache = self._msg_cache.setdefault(key, [])
        cache = [r for r in cache if r.get("id") != record.get("id")]
        cache.append(record)
        self._msg_cache[key] = cache[-100:]

    def _find_cached(self, account_id: str, peer: int, msg_id: int) -> dict | None:
        for r in self._msg_cache.get((account_id, int(peer)), []):
            if r.get("id") == int(msg_id):
                return r
        return None

    def _handle_new_message(self, account_id: str):
        async def handler(event):
            try:
                m = event.message
                peer = utils.get_peer_id(m.peer_id)
                kind, extra = self._media_kind(m)
                rec = {
                    "id": m.id,
                    "text": m.message or "",
                    "date": m.date.isoformat() if m.date else None,
                    "out": bool(m.out),
                    "kind": kind,
                }
                rec.update(extra)
                self._cache_message(account_id, peer, rec)
                self._push_update(account_id, {"t": "msg", "peer": peer, **rec})
            except Exception:
                pass
        return handler

    def _handle_deleted(self, account_id: str):
        async def handler(event):
            try:
                peer = None
                raw_peer = getattr(event, "peer", None)
                if raw_peer is not None:
                    try:
                        peer = utils.get_peer_id(raw_peer)
                    except Exception:
                        peer = None
                if peer is None:
                    peer = getattr(event, "chat_id", None)
                if peer is None:
                    return
                items = []
                for mid in event.deleted_ids or []:
                    cached = self._find_cached(account_id, peer, mid)
                    items.append({
                        "id": int(mid),
                        "text": (cached or {}).get("text", ""),
                        "kind": (cached or {}).get("kind", "text"),
                        "date": (cached or {}).get("date"),
                        "out": bool((cached or {}).get("out")),
                    })
                if self._data_dir is not None:
                    _persist_deleted(self._data_dir, account_id, peer, items)
                self._push_update(account_id, {"t": "del", "peer": int(peer), "items": items})
            except Exception:
                pass
        return handler

    def _handle_user_update(self, account_id: str):
        async def handler(event):
            try:
                upd = event.original_update
                if isinstance(upd, (UpdateUserTyping, UpdateChatUserTyping)):
                    active = not isinstance(upd.action, SendMessageCancelAction)
                    if isinstance(upd, UpdateChatUserTyping):
                        bare = upd.chat_id
                        cands = [bare, -bare, -(1000000000000 + bare)]
                    else:
                        cands = [upd.user_id]
                    self._push_update(account_id, {
                        "t": "typing", "peers": cands,
                        "user_id": getattr(upd, "user_id", None),
                        "typing": bool(active),
                    })
                else:
                    self._push_update(account_id, {"t": "status"})
            except Exception:
                pass
        return handler

    # ------------------------------------------------------ ghost (offline)
    async def _apply_ghost(self, account_id: str) -> None:
        if self._data_dir is None:
            return
        try:
            if not load_prefs(self._data_dir).get("ghost"):
                return
        except Exception:
            return
        client = self.clients.get(account_id)
        if client and client.is_connected():
            try:
                await client(functions.account.UpdateStatusRequest(offline=True))
            except Exception:
                pass

    async def set_ghost(self, enabled: bool) -> None:
        for account_id, client in list(self.clients.items()):
            if client and client.is_connected():
                try:
                    await client(functions.account.UpdateStatusRequest(offline=bool(enabled)))
                except Exception:
                    pass

    # ------------------------------------------------------------- contacts
    async def get_contacts(self, account_id: str) -> list[dict]:
        client = self._live_client(account_id)
        contacts = await client.get_contacts()
        out = []
        for u in contacts or []:
            if not isinstance(u, User):
                continue
            out.append({
                "id": u.id,
                "name": utils.get_display_name(u) or "Unknown",
                "username": u.username,
                "phone": u.phone,
            })
        return out

    # --------------------------------------------------- read / mute / misc
    async def mark_read(self, account_id: str, peer_id: int) -> None:
        client = self._live_client(account_id)
        entity = await self._resolve_peer(account_id, peer_id)
        await client.send_read_acknowledge(entity)

    async def set_muted(self, account_id: str, peer_id: int, mute: bool) -> dict:
        client = self._live_client(account_id)
        entity = await self._resolve_peer(account_id, peer_id)
        try:
            peer = await client.get_input_entity(entity)
        except Exception:
            peer = entity
        until = 2147483647 if mute else 0
        await client(functions.account.UpdateNotifySettingsRequest(
            peer=peer, settings=PeerNotifySettings(mute_until=until)))
        return {"muted": bool(mute)}

    async def is_muted(self, account_id: str, peer_id: int) -> bool:
        import time as _time

        client = self._live_client(account_id)
        entity = await self._resolve_peer(account_id, peer_id)
        try:
            peer = await client.get_input_entity(entity)
        except Exception:
            peer = entity
        try:
            settings = await client(functions.account.GetNotifySettingsRequest(peer=peer))
            until = getattr(settings, "mute_until", 0) or 0
            return bool(until == 2147483647 or until > int(_time.time()))
        except Exception:
            return False

    async def export_history(self, account_id: str, peer_id: int,
                             limit: int = 200) -> str:
        client = self._live_client(account_id)
        entity = await self._resolve_peer(account_id, peer_id)
        messages = await client.get_messages(entity, limit=max(1, min(int(limit or 200), 500)))
        lines = []
        for m in reversed(messages or []):
            if m is None:
                continue
            who = "You" if m.out else "Them"
            when = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "?"
            text = m.message or ""
            if not text and m.media:
                kind, _ = self._media_kind(m)
                text = f"[{kind}]"
            lines.append(f"[{when}] {who}: {text}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------- M3 chats
    def _live_client(self, account_id: str):
        from telethon import TelegramClient  # noqa: F401  (type hint only)

        client = self.clients.get(account_id)
        if not client or not client.is_connected():
            raise RuntimeError("Account is offline")
        return client

    @staticmethod
    def _preview_text(msg) -> str:
        text = (msg.message or "")[:100]
        if text:
            return text
        if msg.media is None:
            return "[message]"
        kind, _ = TelegramManager._media_kind(msg)
        return {
            "photo": "[photo]", "video": "[video]", "round": "[video message]",
            "voice": "[voice message]", "audio": "[audio]", "sticker": "[sticker]",
        }.get(kind, "[file]")

    @staticmethod
    def _media_kind(m) -> Tuple[str, dict]:
        """Classify message media: text/photo/video/round/voice/audio/file/sticker."""
        media = m.media
        if media is None:
            return "text", {}
        if isinstance(media, MessageMediaPhoto):
            return "photo", {}
        doc = getattr(media, "document", None)
        if doc is None:
            return "file", {}
        mime = (doc.mime_type or "").lower()
        is_video = is_voice = is_audio = is_round = is_sticker = False
        duration = None
        for attr in doc.attributes or []:
            if isinstance(attr, DocumentAttributeVideo):
                duration = attr.duration
                if getattr(attr, "round_message", False):
                    is_round = True
                else:
                    is_video = True
            elif isinstance(attr, DocumentAttributeAudio):
                duration = attr.duration
                if getattr(attr, "voice", False):
                    is_voice = True
                else:
                    is_audio = True
            elif isinstance(attr, DocumentAttributeSticker):
                is_sticker = True
        if is_round:
            kind = "round"
        elif is_sticker:
            kind = "sticker"
        elif is_video or (mime.startswith("video/") and not is_audio):
            kind = "video"
        elif is_voice or mime == "audio/ogg":
            kind = "voice"
        elif is_audio or mime.startswith("audio/"):
            kind = "audio"
        elif mime.startswith("image/"):
            kind = "photo"
        else:
            kind = "file"
        extra: dict = {"mime": doc.mime_type}
        if duration:
            extra["duration"] = int(duration)
        try:
            name = m.file.name
            if name:
                extra["file_name"] = name
        except Exception:
            pass
        try:
            size = m.file.size
            if size:
                extra["file_size"] = size
        except Exception:
            pass
        return kind, extra

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
            msg = d.message
            last_text = self._preview_text(msg) if msg is not None else ""
            out.append({
                "peer_id": d.id,
                "title": d.name,
                "unread": d.unread_count,
                "is_user": d.is_user,
                "is_group": d.is_group,
                "is_channel": d.is_channel,
                "is_saved": False,
                "last_text": last_text,
                "last_date": msg.date.isoformat() if msg and msg.date else None,
                "last_out": bool(msg.out) if msg else False,
            })
        # Saved Messages never appears in get_dialogs — pin it on top.
        try:
            me = await client.get_me()
            self._entities[(account_id, me.id)] = await client.get_input_entity(me)
            last = await client.get_messages(me, limit=1)
            msg = last[0] if last else None
            out.insert(0, {
                "peer_id": me.id,
                "title": "Saved Messages",
                "unread": 0,
                "is_user": True,
                "is_group": False,
                "is_channel": False,
                "is_saved": True,
                "last_text": self._preview_text(msg) if msg else "",
                "last_date": msg.date.isoformat() if msg and msg.date else None,
                "last_out": True,
            })
        except Exception:
            pass
        return out

    async def _resolve_peer(self, account_id: str, peer_id: int):
        key = (account_id, int(peer_id))
        entity = self._entities.get(key)
        if entity is not None:
            return entity
        client = self._live_client(account_id)
        # Dialog IDs are "marked" (e.g. -100xxx for channels); get_entity
        # needs the bare ID, so convert first.
        bare = int(peer_id)
        try:
            bare, _ = utils.resolve_id(bare)
        except Exception:
            pass
        entity = await client.get_entity(bare)
        self._entities[key] = entity
        return entity

    async def get_history(self, account_id: str, peer_id: int,
                          limit: int = 30, offset_id: int = 0) -> list[dict]:
        """Oldest-first messages for one conversation, media classified.

        Saved deleted messages (anti-delete vault) are merged in and flagged.
        """
        from .prefs import deleted_for, load_prefs

        client = self._live_client(account_id)
        limit = max(1, min(int(limit or 30), 50))
        entity = await self._resolve_peer(account_id, peer_id)
        messages = await client.get_messages(
            entity, limit=limit, offset_id=int(offset_id or 0)
        )
        out = []
        seen = set()
        for m in messages or []:
            if m is None:
                continue
            kind, extra = self._media_kind(m)
            record = {
                "id": m.id,
                "text": m.message or "",
                "date": m.date.isoformat() if m.date else None,
                "out": bool(m.out),
                "has_media": m.media is not None,
                "kind": kind,
                "deleted": False,
            }
            record.update(extra)
            out.append(record)
            seen.add(m.id)
            self._cache_message(account_id, peer_id, {
                "id": m.id, "text": m.message or "", "kind": kind,
                "date": record["date"], "out": bool(m.out), **extra,
            })
        if self._data_dir is not None:
            try:
                show = load_prefs(self._data_dir).get("show_deleted", True)
            except Exception:
                show = True
            if show:
                try:
                    saved = deleted_for(self._data_dir, account_id, peer_id)
                except Exception:
                    saved = []
                for r in saved:
                    if r.get("id") in seen:
                        continue
                    out.append({
                        "id": r.get("id"),
                        "text": r.get("text", ""),
                        "date": r.get("date"),
                        "out": bool(r.get("out")),
                        "has_media": False,
                        "kind": r.get("kind", "text"),
                        "deleted": True,
                    })
        out.sort(key=lambda r: (r.get("id") or 0))
        return out

    async def send_message(self, account_id: str, peer_id: int, text: str,
                           reply_to: int | None = None) -> dict:
        """Send a text message as one connected account (admin only)."""
        text = (text or "").strip()
        if not text:
            raise RuntimeError("Message is empty")
        if len(text) > 4096:
            raise RuntimeError("Message is too long (max 4096 characters)")
        client = self._live_client(account_id)
        entity = await self._resolve_peer(account_id, peer_id)
        kwargs = {}
        if reply_to:
            kwargs["reply_to"] = int(reply_to)
        sent = await client.send_message(entity, text, **kwargs)
        return {"id": sent.id, "date": sent.date.isoformat() if sent.date else None}

    @staticmethod
    def _marked_id(entity) -> int:
        """Marked peer ID, the same scheme Dialog.id uses (e.g. -100xxx)."""
        if isinstance(entity, Chat):
            return -entity.id
        if isinstance(entity, Channel):
            return -(1000000000000 + entity.id)
        return entity.id

    async def resolve_username(self, account_id: str, username: str) -> dict:
        """Turn @username into a peer_id + title so a chat can be opened."""
        client = self._live_client(account_id)
        username = (username or "").strip().lstrip("@")
        if not username:
            raise RuntimeError("Enter a username")
        try:
            entity = await client.get_entity(username)
        except Exception:
            raise RuntimeError(f"No Telegram chat found for @{username}")
        pid = self._marked_id(entity)
        self._entities[(account_id, pid)] = await client.get_input_entity(entity)
        return {"peer_id": pid, "title": utils.get_display_name(entity) or username}

    async def get_presence(self, account_id: str, peer_id: int) -> dict:
        """Online / last-seen line for a thread header."""
        entity = await self._resolve_peer(account_id, peer_id)
        if isinstance(entity, User):
            st = entity.status
            if isinstance(st, UserStatusOnline):
                return {"status": "online"}
            if isinstance(st, UserStatusOffline) and st.was_online:
                wo = st.was_online
                if wo.tzinfo is None:
                    wo = wo.replace(tzinfo=timezone.utc)
                diff = (datetime.now(timezone.utc) - wo).total_seconds()
                if diff < 60:
                    return {"status": "last seen just now"}
                if diff < 3600:
                    mins = max(1, int(diff // 60))
                    return {"status": f"last seen {mins} minute{'s' if mins != 1 else ''} ago"}
                if diff < 86400:
                    hours = int(diff // 3600)
                    return {"status": f"last seen {hours} hour{'s' if hours != 1 else ''} ago"}
                return {"status": f"last seen {wo.strftime('%b %d')}"}
            if isinstance(st, UserStatusRecently):
                return {"status": "last seen recently"}
            if isinstance(st, UserStatusLastWeek):
                return {"status": "last seen within a week"}
            if isinstance(st, UserStatusLastMonth):
                return {"status": "last seen within a month"}
            if isinstance(st, UserStatusEmpty):
                return {"status": "last seen a long time ago"}
            return {"status": ""}
        if isinstance(entity, Channel):
            return {"status": "group" if getattr(entity, "megagroup", False) else "channel"}
        if isinstance(entity, Chat):
            return {"status": "group"}
        return {"status": ""}

    @staticmethod
    def _safe_name(*parts: str) -> str:
        name = "_".join(parts)
        return "".join(c for c in name if c.isalnum() or c in "._-")[:64]

    async def download_media(self, account_id: str, peer_id: int, msg_id: int,
                             media_dir: Path) -> Tuple[str, str]:
        """Download (once, then cache) one message's media. Returns (path, mime)."""
        client = self._live_client(account_id)
        entity = await self._resolve_peer(account_id, peer_id)
        found = await client.get_messages(entity, ids=int(msg_id))
        m = found[0] if isinstance(found, list) else found
        if m is None or not m.media:
            raise RuntimeError("No media on that message")
        ext = ""
        try:
            ext = m.file.ext or ""
        except Exception:
            ext = ""
        if ext and not ext.startswith("."):
            ext = "." + ext
        ext = "".join(c for c in ext if c.isalnum() or c in "._")[:8] or ".bin"
        path = media_dir / self._safe_name(str(account_id), str(int(msg_id)) + ext)
        if not path.exists() or path.stat().st_size == 0:
            saved = await client.download_media(m, file=str(path))
            if not saved:
                raise RuntimeError("Download failed")
        mime, _ = mimetypes.guess_type(str(path))
        if not mime:
            try:
                mime = m.document.mime_type
            except Exception:
                mime = "application/octet-stream"
        return str(path), mime or "application/octet-stream"

    async def download_photo(self, account_id: str, peer_id: int,
                             avatars_dir: Path) -> str:
        """Download (once, then cache) a profile photo. Returns the path."""
        client = self._live_client(account_id)
        # Profile photos need the FULL entity (with .photo); input entities
        # from the dialog cache are not enough, so fetch it by bare ID.
        bare = int(peer_id)
        try:
            bare, _ = utils.resolve_id(bare)
        except Exception:
            pass
        try:
            full = await client.get_entity(bare)
        except Exception:
            raise RuntimeError("No profile photo")
        path = avatars_dir / self._safe_name(str(account_id), str(int(peer_id)) + ".jpg")
        if not path.exists() or path.stat().st_size == 0:
            saved = await client.download_profile_photo(full, file=str(path))
            if not saved:
                raise RuntimeError("No profile photo")
        return str(path)

    async def send_file(self, account_id: str, peer_id: int, data: bytes,
                        filename: str, caption: str = "",
                        reply_to: int | None = None) -> dict:
        """Send a photo/video/file with an optional caption (admin only)."""
        if not data:
            raise RuntimeError("Empty file")
        if len(data) > 100 * 1024 * 1024:
            raise RuntimeError("File is too big (max 100 MB)")
        safe = "".join(c for c in (filename or "file") if c.isalnum() or c in "._-")[:80] or "file"
        tmp = Path(tempfile.gettempdir()) / f"foggygramm-upload-{uuid.uuid4().hex}-{safe}"
        client = self._live_client(account_id)
        entity = await self._resolve_peer(account_id, peer_id)
        try:
            tmp.write_bytes(data)
            kwargs = {"caption": (caption or "").strip() or None}
            if reply_to:
                kwargs["reply_to"] = int(reply_to)
            sent = await client.send_file(entity, str(tmp), **kwargs)
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
        return {"id": sent.id, "date": sent.date.isoformat() if sent.date else None}
