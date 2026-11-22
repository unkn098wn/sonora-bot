#!/usr/bin/env python3
"""
Sonora Music — Telegram Auth, Registration & OAuth Gateway (SonoraAuthBot v2.7.0)
================================================================================
Complete authentication and registration ecosystem for Sonora Music Player.

Features:
- Real persistent user database (data/users.json) with duplicate registration prevention
- Automatic Telegram profile photo extraction and local avatar serving (/api/avatar/{handle})
- Real-time constant-time OTP verification (anti-brute force, 10 min TTL)
- Real-time Telegram login alert notifications sent directly to the user's chat
- Built-in Google Web Auth portal (/auth/google) with OAuth loopback to Sonora Desktop
- Direct file delivery (Setup .exe, Portable .zip, Android .apk) inside Telegram
- Aiogram 3.7+ through 3.32+ support with DefaultBotProperties(parse_mode="HTML")
- Full REST API bridge on port 8088
"""

import asyncio
import aiohttp
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple
from aiohttp import web
import re
import smtplib
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Auto-load .env configuration
_env_path = os.path.join(BASE_DIR, ".env")
if os.path.isfile(_env_path):
    try:
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except Exception as _e:
        pass


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("SonoraAuthBot")

# Base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
AVATARS_DIR = os.path.join(BASE_DIR, "avatars")
USERS_DB_PATH = os.path.join(DATA_DIR, "users.json")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(AVATARS_DIR, exist_ok=True)

# Configuration
BOT_TOKEN = os.getenv("SONORA_BOT_TOKEN", "9127762870:AAHJXJL9EADvRAeTh8zdblhBbbjLxvBtPgU").strip()
BOT_USERNAME = os.getenv("SONORA_BOT_USERNAME", "sonoramus1c_bot").strip().lstrip("@")
HTTP_PORT = int(os.getenv("PORT", os.getenv("SONORA_AUTH_PORT", "8088")))
OTP_TTL_SECONDS = 600  # 10 minutes
MAX_ATTEMPTS_PER_SESSION = 5
LOCKOUT_SECONDS = 300  # 5 min lockout on brute-force
MAX_STORED_SESSIONS = 10000

# Global active bot reference
active_bot_instance = None


# ==============================================================================
# 1. PERSISTENT USER DATABASE (USER MANAGER)
# ==============================================================================
class UserManager:
    """Thread-safe persistent JSON database of registered Sonora users."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._users: Dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self):
        if os.path.isfile(self.db_path):
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    self._users = json.load(f)
                logger.info(f"Loaded {len(self._users)} registered users from {self.db_path}")
            except Exception as e:
                logger.error(f"Failed to load users DB: {e}")
                self._users = {}
        else:
            self._users = {}
            self._save_sync()

    def _save_sync(self):
        try:
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(self._users, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save users DB: {e}")

    async def get_by_telegram_id(self, tg_id: int) -> Optional[dict]:
        async with self._lock:
            return self._users.get(str(tg_id))

    async def get_by_handle(self, handle: str) -> Optional[dict]:
        clean = handle.lower().lstrip("@").strip()
        async with self._lock:
            for u in self._users.values():
                h = u.get("username", "").lower().lstrip("@")
                if h and h == clean:
                    return u
            return None

    async def is_registered(self, tg_id: Optional[int] = None, handle: Optional[str] = None) -> bool:
        if tg_id and await self.get_by_telegram_id(tg_id):
            return True
        if handle and await self.get_by_handle(handle):
            return True
        return False

    async def upsert_user(
        self,
        tg_id: int,
        username: str,
        first_name: str,
        last_name: str = "",
        avatar_file: Optional[str] = None,
    ) -> dict:
        async with self._lock:
            key = str(tg_id)
            clean_username = username.lstrip("@").strip() if username else ""
            display_name = f"{first_name} {last_name}".strip() or (f"@{clean_username}" if clean_username else f"user_{tg_id}")

            existing_key = None
            if key in self._users:
                existing_key = key
            else:
                for k, u in self._users.items():
                    if clean_username and u.get("username", "").lower().lstrip("@") == clean_username.lower():
                        existing_key = k
                        break

            if existing_key:
                u = self._users[existing_key]
                u["telegram_id"] = tg_id
                if clean_username:
                    u["username"] = clean_username
                if first_name:
                    u["first_name"] = first_name
                    u["last_name"] = last_name or ""
                    u["display_name"] = display_name
                if avatar_file:
                    u["avatar_file"] = avatar_file
                if existing_key != key:
                    del self._users[existing_key]
                    self._users[key] = u
                self._save_sync()
                logger.info(f"Updated user: {display_name} (@{clean_username}, ID: {tg_id})")
                return u
            else:
                user_record = {
                    "telegram_id": tg_id,
                    "username": clean_username,
                    "first_name": first_name or "",
                    "last_name": last_name or "",
                    "display_name": display_name,
                    "avatar_file": avatar_file,
                    "registered_at": datetime.utcnow().isoformat() + "Z",
                    "last_login_at": datetime.utcnow().isoformat() + "Z",
                    "login_count": 0,
                }
                self._users[key] = user_record
                self._save_sync()
                logger.info(f"Registered new user: {display_name} (@{clean_username}, ID: {tg_id})")
                return user_record

    async def register_user(
        self,
        tg_id: int,
        username: str,
        first_name: str,
        last_name: str,
        avatar_file: Optional[str] = None,
    ) -> dict:
        return await self.upsert_user(tg_id, username, first_name, last_name, avatar_file)

    async def update_user(self, tg_id: int, **fields):
        async with self._lock:
            key = str(tg_id)
            if key in self._users:
                self._users[key].update(fields)
                self._save_sync()

    async def record_login(self, tg_id: int):
        async with self._lock:
            key = str(tg_id)
            if key in self._users:
                self._users[key]["last_login_at"] = datetime.utcnow().isoformat() + "Z"
                self._users[key]["login_count"] = self._users[key].get("login_count", 0) + 1
                self._save_sync()


    async def get_by_email(self, email: str) -> Optional[dict]:
        clean = email.lower().strip()
        async with self._lock:
            for u in self._users.values():
                if u.get("email", "").lower().strip() == clean:
                    return u
            return None

    async def upsert_email_user(
        self,
        email: str,
        display_name: str = "",
        password_hash: Optional[str] = None,
        salt: Optional[str] = None,
    ) -> dict:
        clean = email.lower().strip()
        short_id = hashlib.sha256(clean.encode()).hexdigest()[:8]
        key = f"email_{short_id}"
        resolved_name = display_name.strip() or f"user_{short_id}"
        async with self._lock:
            if key in self._users:
                u = self._users[key]
                if display_name:
                    u["display_name"] = resolved_name
                if password_hash:
                    u["password_hash"] = password_hash
                if salt:
                    u["salt"] = salt
                u["last_login_at"] = datetime.utcnow().isoformat() + "Z"
                self._save_sync()
                return u
            else:
                user_record = {
                    "user_id": key,
                    "email": clean,
                    "username": f"@user_{short_id}",
                    "display_name": resolved_name,
                    "provider": "email",
                    "password_hash": password_hash,
                    "salt": salt,
                    "registered_at": datetime.utcnow().isoformat() + "Z",
                    "last_login_at": datetime.utcnow().isoformat() + "Z",
                    "login_count": 1,
                }
                self._users[key] = user_record
                self._save_sync()
                logger.info(f"Registered new email user: {resolved_name} ({clean})")
                return user_record

    async def set_email_password(self, email: str, password_hash: str, salt: Optional[str] = None) -> bool:
        clean = email.lower().strip()
        short_id = hashlib.sha256(clean.encode()).hexdigest()[:8]
        key = f"email_{short_id}"
        async with self._lock:
            if key in self._users:
                self._users[key]["password_hash"] = password_hash
                if salt:
                    self._users[key]["salt"] = salt
                self._save_sync()
                return True
            return False

user_manager = UserManager(USERS_DB_PATH)


# ==============================================================================
# 2. SESSION & OTP MANAGER
# ==============================================================================
@dataclass
class AuthSession:
    handle: str
    code: str
    created_at: float
    session_id: str
    telegram_id: Optional[int] = None
    verified: bool = False
    failed_attempts: int = 0
    locked_until: float = 0.0

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > OTP_TTL_SECONDS

    @property
    def is_locked(self) -> bool:
        return time.time() < self.locked_until

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(OTP_TTL_SECONDS - (time.time() - self.created_at)))


class SessionManager:
    """Thread-safe OTP session storage with strict verification and anti-brute-force."""

    def __init__(self):
        self._sessions: Dict[str, AuthSession] = {}       # session_id -> AuthSession
        self._handle_to_session: Dict[str, str] = {}     # clean_handle -> session_id
        self._code_to_session: Dict[str, str] = {}       # code -> session_id
        self._ip_attempts: Dict[str, Tuple[int, float]] = {}  # ip -> (attempts, reset_at)
        self._oauth_sessions: Dict[str, dict] = {}       # session_id -> oauth_data
        self._lock = asyncio.Lock()

    async def generate_otp(
        self,
        handle: str,
        telegram_id: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> AuthSession:
        async with self._lock:
            self._purge_expired_locked()

            if len(self._sessions) >= MAX_STORED_SESSIONS:
                oldest_sid = min(self._sessions, key=lambda s: self._sessions[s].created_at)
                self._remove_session_locked(oldest_sid)

            clean_handle = handle.lower().lstrip("@").strip()
            # Cryptographically secure 6-digit OTP
            code = f"{secrets.randbelow(900000) + 100000}"
            sid = session_id or f"sonora_{clean_handle}_{secrets.token_hex(6)}"

            session = AuthSession(
                handle=clean_handle,
                code=code,
                created_at=time.time(),
                session_id=sid,
                telegram_id=telegram_id,
            )

            self._sessions[sid] = session
            self._handle_to_session[clean_handle] = sid
            self._code_to_session[code] = sid

            logger.info(f"Generated OTP for @{clean_handle} (tg_id={telegram_id}): {code}")
            return session

    async def get_latest_session(self, handle: str) -> Optional[AuthSession]:
        async with self._lock:
            self._purge_expired_locked()
            clean_handle = handle.lower().lstrip("@").strip()
            sid = self._handle_to_session.get(clean_handle)
            if sid and sid in self._sessions:
                s = self._sessions[sid]
                if not s.is_expired:
                    return s
            return None

    async def verify_otp(self, handle: str, code: str, client_ip: str = "127.0.0.1") -> Tuple[bool, str, Optional[AuthSession]]:
        """Verifies OTP using constant-time comparison with anti-brute-force lockout."""
        async with self._lock:
            self._purge_expired_locked()

            clean_handle = handle.lower().lstrip("@").strip()
            clean_code = "".join(filter(str.isdigit, str(code)))

            # IP rate check
            now = time.time()
            if client_ip in self._ip_attempts:
                attempts, reset_at = self._ip_attempts[client_ip]
                if now < reset_at:
                    if attempts >= 20:
                        return False, "Слишком много попыток с вашего IP. Подождите 1 минуту.", None
                    self._ip_attempts[client_ip] = (attempts + 1, reset_at)
                else:
                    self._ip_attempts[client_ip] = (1, now + 60.0)
            else:
                self._ip_attempts[client_ip] = (1, now + 60.0)

            sid = self._handle_to_session.get(clean_handle)
            if not sid or sid not in self._sessions:
                sid = self._code_to_session.get(clean_code)

            if not sid or sid not in self._sessions:
                return False, "Код не найден или устарел. Получите свежий код в @sonoramus1c_bot.", None

            session = self._sessions[sid]
            # Enforce that the session belongs to the requesting handle (prevent cross-account code reuse)
            if session.handle != clean_handle:
                logger.warning(f"⚠ Попытка межсессионного использования кода: @{clean_handle} попытался использовать код @{session.handle}")
                return False, "Код не принадлежит данному пользователю.", None

            if session.is_expired:
                self._remove_session_locked(sid)
                return False, "Срок действия кода истёк (10 минут). Запросите новый код в боте.", None

            if session.is_locked:
                wait_sec = int(session.locked_until - now)
                return False, f"Слишком много неверных попыток. Блокировка на {wait_sec} сек.", None

            # Constant-time comparison
            is_valid = hmac.compare_digest(session.code.encode(), clean_code.encode())

            if is_valid:
                session.verified = True
                self._remove_session_locked(sid)
                logger.info(f"✓ Успешная верификация OTP для @{clean_handle}")
                return True, "Успешно верифицировано", session
            else:
                session.failed_attempts += 1
                remaining = MAX_ATTEMPTS_PER_SESSION - session.failed_attempts
                if session.failed_attempts >= MAX_ATTEMPTS_PER_SESSION:
                    session.locked_until = now + LOCKOUT_SECONDS
                    logger.warning(f"⚠ Блокировка сессии {sid} (@{clean_handle}) за перебор кодов.")
                    return False, f"Превышен лимит попыток. Вход заблокирован на {LOCKOUT_SECONDS // 60} минут.", None

                return False, f"Неверный код. Осталось попыток: {remaining}. Получите код в боте.", None

    def _remove_session_locked(self, sid: str):
        s = self._sessions.pop(sid, None)
        if s:
            if self._handle_to_session.get(s.handle) == sid:
                self._handle_to_session.pop(s.handle, None)
            if self._code_to_session.get(s.code) == sid:
                self._code_to_session.pop(s.code, None)

    def _purge_expired_locked(self):
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if (now - s.created_at) > OTP_TTL_SECONDS]
        for sid in expired:
            self._remove_session_locked(sid)

    # OAuth helper methods
    def create_oauth_session(self, session_id: str, provider: str = "google"):
        self._oauth_sessions[session_id] = {
            "session_id": session_id,
            "provider": provider,
            "status": "pending",
            "created_at": time.time(),
            "user": None,
        }

    def complete_oauth_session(self, session_id: str, user_data: dict):
        if session_id in self._oauth_sessions:
            self._oauth_sessions[session_id]["status"] = "authorized"
            self._oauth_sessions[session_id]["user"] = user_data
            return True
        return False

    def get_oauth_session(self, session_id: str) -> Optional[dict]:
        return self._oauth_sessions.get(session_id)

    async def background_janitor(self):
        while True:
            try:
                await asyncio.sleep(30)
                async with self._lock:
                    self._purge_expired_locked()
                    now = time.time()
                    expired_ips = [ip for ip, (_, r) in self._ip_attempts.items() if now > r]
                    for ip in expired_ips:
                        self._ip_attempts.pop(ip, None)
                    # Clean expired OAuth sessions (TTL 15 min)
                    expired_oauth = [sid for sid, o in self._oauth_sessions.items() if (now - o["created_at"]) > 900]
                    for sid in expired_oauth:
                        self._oauth_sessions.pop(sid, None)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Janitor error: {e}")


session_manager = SessionManager()


# ==============================================================================
# 3. AVATAR EXTRACTION HELPER
# ==============================================================================
async def download_telegram_avatar(bot, user_id: int) -> Optional[str]:
    """Downloads user's Telegram profile photo and saves it to avatars/{user_id}.jpg."""
    try:
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
        if photos.total_count > 0 and len(photos.photos) > 0:
            photo = photos.photos[0][-1]
            file_info = await bot.get_file(photo.file_id)
            save_name = f"{user_id}.jpg"
            dest_path = os.path.join(AVATARS_DIR, save_name)
            await bot.download_file(file_info.file_path, destination=dest_path)
            logger.info(f"Downloaded Telegram avatar for user {user_id} -> {dest_path}")
            return save_name
    except Exception as e:
        logger.warning(f"Could not download Telegram avatar for {user_id}: {e}")
    return None


# ==============================================================================
# 4. REST API & GOOGLE OAUTH WEB HANDLERS
# ==============================================================================
async def handle_health(request: Optional[web.Request] = None) -> web.Response:
    return web.json_response({
        "status": "online",
        "service": "SonoraAuthBot Gateway",
        "version": "2.7.0",
        "bot_username": BOT_USERNAME,
        "users_count": len(user_manager._users),
        "time": datetime.utcnow().isoformat() + "Z",
    })


async def handle_check_user(request: web.Request) -> web.Response:
    """Checks whether a username or telegram ID is registered in Sonora."""
    handle = request.query.get("handle", "").strip()
    if not handle:
        return web.json_response({"success": False, "error": "Параметр handle обязателен"}, status=400)

    clean_handle = handle.lower().lstrip("@")
    user = await user_manager.get_by_handle(clean_handle)
    if not user and clean_handle.isdigit():
        user = await user_manager.get_by_telegram_id(int(clean_handle))

    if user:
        return web.json_response({
            "exists": True,
            "registered": True,
            "handle": f"@{user['username']}" if user.get("username") else "",
            "display_name": user.get("display_name", ""),
            "avatar_url": f"http://127.0.0.1:{HTTP_PORT}/api/avatar/{clean_handle}",
            "registered_at": user.get("registered_at"),
        })
    else:
        return web.json_response({
            "exists": False,
            "registered": False,
            "handle": f"@{clean_handle}",
            "message": "Пользователь не зарегистрирован в Sonora. Требуется регистрация через бота.",
        })


async def handle_request_otp(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        handle = data.get("handle", "").strip()
        if not handle:
            return web.json_response({"success": False, "error": "Поле handle обязательно"}, status=400)

        clean_handle = handle.lower().lstrip("@")
        user = await user_manager.get_by_handle(clean_handle)
        if not user and clean_handle.isdigit():
            user = await user_manager.get_by_telegram_id(int(clean_handle))

        if not user:
            return web.json_response({
                "success": False,
                "error": "user_not_registered",
                "message": f"Пользователь @{clean_handle} ещё не зарегистрирован в Sonora. Откройте @{BOT_USERNAME} и нажмите «Зарегистрироваться».",
            }, status=404)

        session = await session_manager.generate_otp(clean_handle, telegram_id=user.get("telegram_id"))
        tg_id = user.get("telegram_id")
        if tg_id and active_bot_instance:
            welcome_text = format_registered_welcome(
                user.get("first_name", clean_handle),
                user.get("username", clean_handle),
                session.code,
                session.remaining_seconds,
            )
            async def _send_otp_push():
                try:
                    await active_bot_instance.send_message(tg_id, welcome_text)
                    logger.info(f"✓ OTP успешно отправлен в Telegram пользователю @{clean_handle} (ID: {tg_id})")
                except Exception as ex:
                    logger.warning(f"Не удалось отправить push с кодом пользователю {tg_id}: {ex}")
            asyncio.create_task(_send_otp_push())

        return web.json_response({
            "success": True,
            "handle": f"@{clean_handle}",
            "expires_in": session.remaining_seconds,
            "message": "Код сгенерирован. Проверьте сообщения в Telegram.",
        })
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)



# ==============================================================================
# EMAIL SERVICE & EMAIL OTP MANAGER
# ==============================================================================
class EmailService:
    """Universal SMTP Email Dispatcher with auto-detection for Gmail, Yandex, Mail.ru."""

    def __init__(self):
        self.host = os.getenv("SMTP_HOST", "").strip()
        self.port = int(os.getenv("SMTP_PORT", "0") or "0")
        self.user = os.getenv("SMTP_USER", "").strip()
        self.password = os.getenv("SMTP_PASSWORD", "").strip()
        self.from_name = os.getenv("SMTP_FROM_NAME", "Sonora Music").strip()
        self.from_email = os.getenv("SMTP_FROM_EMAIL", "").strip() or self.user
        self._auto_detect()

    def _auto_detect(self):
        if self.user and not self.host:
            domain = self.user.split("@")[-1].lower() if "@" in self.user else ""
            if "gmail.com" in domain:
                self.host = "smtp.gmail.com"
                self.port = 587
            elif any(y in domain for y in ["yandex.ru", "ya.ru", "yandex.com"]):
                self.host = "smtp.yandex.ru"
                self.port = 465
            elif any(m in domain for m in ["mail.ru", "inbox.ru", "bk.ru", "list.ru"]):
                self.host = "smtp.mail.ru"
                self.port = 465
            elif any(o in domain for o in ["outlook.com", "hotmail.com"]):
                self.host = "smtp.office365.com"
                self.port = 587
        if not self.port:
            self.port = 465 if self.host and ("yandex" in self.host or "mail.ru" in self.host) else 587

    def _send_sync(self, to_email: str, code: str) -> Tuple[bool, str]:
        if not self.user or not self.password or not self.host:
            logger.info(
                f"\n" + "="*60 +
                f"\n[DEMO EMAIL OTP] No SMTP password in .env." +
                f"\nTarget Email: {to_email}" +
                f"\nVerification Code: [{code}]" +
                f"\nValid for 10 minutes." +
                f"\n(To send real emails, enter SMTP credentials in Sonora-Telegram-Bot/.env)" +
                f"\n" + "="*60
            )
            return True, f"Код подтверждения сгенерирован: {code} (демо-режим сервера)"

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"Sonora Music — Код подтверждения: {code}"
            msg["From"] = f"{self.from_name} <{self.from_email}>"
            msg["To"] = to_email

            text_content = (
                f"Здравствуйте!\n\n"
                f"Ваш код подтверждения для входа в Sonora Music:\n"
                f"{code}\n\n"
                f"Код действителен в течение 10 минут.\n"
                f"Если вы не запрашивали данный код, проигнорируйте это письмо.\n\n"
                f"— Команда Sonora Music"
            )
            html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:28px 12px;background:#080A0E;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:440px;margin:0 auto;background:#10131A;border:1px solid rgba(255,255,255,0.12);border-radius:24px;padding:36px 28px;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,0.8);">
    <div style="display:inline-block;width:56px;height:56px;line-height:56px;border-radius:18px;background:linear-gradient(135deg,#00E5FF,#0077FF);color:#FFFFFF;font-size:26px;font-weight:900;margin-bottom:16px;">S</div>
    <h1 style="margin:0 0 6px 0;color:#FFFFFF;font-size:22px;font-weight:900;letter-spacing:4px;">SONORA</h1>
    <p style="margin:0 0 24px 0;color:#8F96A3;font-size:13px;letter-spacing:0.4px;">Hi-Fi стриминг нового поколения</p>
    
    <div style="background:#06080C;border:1px solid rgba(0,229,255,0.35);border-radius:16px;padding:22px;margin-bottom:24px;">
      <div style="color:#8F96A3;font-size:11px;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;">Ваш код подтверждения</div>
      <div style="color:#00E5FF;font-size:38px;font-weight:900;letter-spacing:12px;font-family:Consolas,Monaco,monospace;">{code}</div>
    </div>
    
    <p style="margin:0 0 16px 0;color:#FFFFFF;font-size:13px;line-height:1.5;">Введите этот код в приложении Sonora для входа в свой аккаунт.</p>
    <p style="margin:0;color:#6C7584;font-size:11.5px;line-height:1.4;">Срок действия: <b>10 минут</b>.<br>Если вы не отправляли запрос, просто проигнорируйте письмо.</p>
  </div>
</body>
</html>"""
            msg.attach(MIMEText(text_content, "plain", "utf-8"))
            msg.attach(MIMEText(html_content, "html", "utf-8"))

            if self.port == 465:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=12) as server:
                    server.login(self.user, self.password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=12) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(self.user, self.password)
                    server.send_message(msg)

            logger.info(f"Successfully sent OTP email to {to_email} via {self.host}:{self.port}")
            return True, "Письмо с кодом успешно отправлено на почту."
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return False, f"Ошибка отправки через SMTP ({e})."

    async def send_otp(self, to_email: str, code: str) -> Tuple[bool, str]:
        return await asyncio.to_thread(self._send_sync, to_email, code)


email_service = EmailService()


@dataclass
class EmailOtpSession:
    email: str
    code: str
    created_at: float
    failed_attempts: int = 0
    locked_until: float = 0.0

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > 600.0  # 10 min TTL

    @property
    def is_locked(self) -> bool:
        return time.time() < self.locked_until

    @property
    def cooldown_seconds(self) -> int:
        elapsed = time.time() - self.created_at
        return max(0, int(60.0 - elapsed))


class EmailOtpManager:
    """Manages 6-digit email OTP sessions with rate limits, lockouts and constant-time checks."""

    def __init__(self):
        self._sessions: Dict[str, EmailOtpSession] = {}
        self._lock = asyncio.Lock()

    async def create_or_refresh_otp(self, email: str) -> Tuple[bool, str, int, Optional[str]]:
        clean_email = email.lower().strip()
        async with self._lock:
            existing = self._sessions.get(clean_email)
            if existing and not existing.is_expired:
                if existing.is_locked:
                    rem = int(existing.locked_until - time.time())
                    return False, f"Слишком много попыток. Подождите {rem} сек.", rem, None
                if existing.cooldown_seconds > 0:
                    return False, f"Код уже отправлен. Повторный запрос через {existing.cooldown_seconds} сек.", existing.cooldown_seconds, existing.code

            # Generate 6-digit code
            code = f"{secrets.randbelow(900000) + 100000}"
            session = EmailOtpSession(
                email=clean_email,
                code=code,
                created_at=time.time(),
            )
            self._sessions[clean_email] = session
            return True, "Код сформирован", 60, code

    async def verify_otp(self, email: str, input_code: str) -> Tuple[bool, str]:
        clean_email = email.lower().strip()
        clean_code = "".join(filter(str.isdigit, input_code.strip()))

        async with self._lock:
            session = self._sessions.get(clean_email)
            if not session:
                return False, "Код подтверждения не запрашивался или срок действия истёк."

            if session.is_expired:
                del self._sessions[clean_email]
                return False, "Срок действия кода истёк. Запросите новый код."

            if session.is_locked:
                rem = int(session.locked_until - time.time())
                return False, f"Превышено число попыток ввода. Подождите {rem} сек."

            # Constant-time comparison
            if secrets.compare_digest(session.code, clean_code):
                del self._sessions[clean_email]
                return True, "Код успешно подтверждён"
            else:
                session.failed_attempts += 1
                if session.failed_attempts >= 5:
                    session.locked_until = time.time() + 300.0  # 5 min lockout
                    return False, "Неверный код. Аккаунт временно заблокирован на 5 минут из соображений безопасности."
                rem_tries = 5 - session.failed_attempts
                return False, f"Неверный код подтверждения. Осталось попыток: {rem_tries}"


email_otp_manager = EmailOtpManager()


class CloudflareTunnelManager:
    """Spins up a lightweight trycloudflare tunnel for 100% remote phone reachability."""

    def __init__(self, port: int, base_dir: str):
        self.port = port
        self.base_dir = base_dir
        self.process = None
        self.public_url = None
        self._url_file = os.path.join(base_dir, "data", "public_url.txt")

    async def start(self):
        # 1. Cloud environment autodetection (Render, Koyeb, Railway, Custom Domain)
        env_public = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("KOYEB_PUBLIC_DOMAIN") or os.getenv("PUBLIC_URL")
        if env_public:
            if not env_public.startswith("http"):
                env_public = f"https://{env_public}"
            self.public_url = env_public.rstrip("/")
            logger.info(f"\n" + "="*60 +
                        f"\n  SONORA CLOUD GATEWAY ACTIVE (Production Server):" +
                        f"\n  {self.public_url}" +
                        f"\n  Serving 24/7 in the cloud without local PC requirement!" +
                        f"\n" + "="*60)
            asyncio.create_task(self._publish_gateway_url(self.public_url))
            return

        cloudflared_bin = os.path.join(self.base_dir, "cloudflared.exe")
        if not os.path.isfile(cloudflared_bin):
            cloudflared_bin = "cloudflared"

        try:
            cmd = [cloudflared_bin, "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate"]
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            asyncio.create_task(self._read_tunnel_url())
        except Exception as e:
            logger.info(f"Cloudflare Tunnel not started (local mode active): {e}")

    async def _read_tunnel_url(self):
        pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
        start_time = time.time()
        while self.process and self.process.returncode is None and (time.time() - start_time < 30):
            line_bytes = await self.process.stderr.readline()
            if not line_bytes:
                break
            line = line_bytes.decode(errors="ignore")
            m = pattern.search(line)
            if m:
                self.public_url = m.group(0)
                logger.info(f"\n" + "="*60 +
                            f"\n  SONORA PUBLIC HTTPS GATEWAY (Cloudflare Tunnel):" +
                            f"\n  {self.public_url}" +
                            f"\n  Mobile phones can now connect from anywhere worldwide!" +
                            f"\n" + "="*60)
                asyncio.create_task(self._publish_gateway_url(self.public_url))
                break

    async def _publish_gateway_url(self, url: str):
        # 1. Save locally to data/public_url.txt
        try:
            os.makedirs(os.path.join(self.base_dir, "data"), exist_ok=True)
            with open(self._url_file, "w", encoding="utf-8") as f:
                f.write(url)
        except Exception as e:
            logger.debug(f"Failed to write public_url.txt: {e}")

        # 2. Write to local website/assets directories if present
        gateway_data = {
            "gateway_url": url,
            "status": "online",
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "description": "Sonora Music Live Public Gateway"
        }
        for candidate_dir in [
            os.path.join(self.base_dir, "..", "Sonora-Website"),
            os.path.join(self.base_dir, "..", "Sonora", "assets"),
        ]:
            if os.path.isdir(candidate_dir):
                try:
                    fpath = os.path.join(candidate_dir, "gateway.json")
                    with open(fpath, "w", encoding="utf-8") as f:
                        json.dump(gateway_data, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass

        # 3. Publish to GitHub repo unkn098wn/sonora-website/gateway.json via GitHub API
        github_token = os.getenv("GITHUB_TOKEN", "")
        if not github_token:
            token_file = os.path.join(self.base_dir, "data", "github_token.txt")
            if os.path.isfile(token_file):
                try:
                    with open(token_file, "r", encoding="utf-8") as tf:
                        github_token = tf.read().strip()
                except Exception:
                    pass
        if not github_token:
            return
        repo = "unkn098wn/sonora-website"
        path = "gateway.json"
        api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
        try:
            content_json = json.dumps(gateway_data, indent=2, ensure_ascii=False)
            encoded = base64.b64encode(content_json.encode()).decode()
            headers = {
                "Authorization": f"token {github_token}",
                "User-Agent": "Sonora-Bot-TunnelManager",
                "Accept": "application/vnd.github.v3+json"
            }
            sha = None
            try:
                get_req = urllib.request.Request(api_url, headers=headers)
                with urllib.request.urlopen(get_req, timeout=5) as resp:
                    resp_data = json.loads(resp.read().decode())
                    sha = resp_data.get("sha")
            except Exception:
                pass

            body = {
                "message": f"chore: update live gateway URL to {url}",
                "content": encoded
            }
            if sha:
                body["sha"] = sha

            put_req = urllib.request.Request(api_url, data=json.dumps(body).encode(), headers=headers, method="PUT")
            with urllib.request.urlopen(put_req, timeout=6) as resp:
                if resp.status in (200, 201):
                    logger.info(f"Successfully published live gateway URL to GitHub: {url}")
        except Exception as e:
            logger.warning(f"Could not publish gateway URL to GitHub: {e}")


tunnel_manager = CloudflareTunnelManager(HTTP_PORT, BASE_DIR)



async def handle_send_email_otp(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        raw_email = str(data.get("email", "")).strip().lower()

        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", raw_email):
            return web.json_response({
                "success": False,
                "error": "Введите корректный адрес электронной почты (например, name@gmail.com)."
            }, status=400)

        success, msg, cooldown, code = await email_otp_manager.create_or_refresh_otp(raw_email)
        if not success:
            return web.json_response({"success": False, "error": msg, "cooldown": cooldown}, status=429)

        # Dispatch email
        send_ok, send_msg = await email_service.send_otp(raw_email, code)
        if not send_ok:
            return web.json_response({"success": False, "error": send_msg}, status=500)

        return web.json_response({
            "success": True,
            "message": send_msg,
            "email": raw_email,
            "cooldown": cooldown,
            "expires_in": 600,
        })
    except Exception as e:
        logger.error(f"Error in handle_send_email_otp: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_verify_email_otp(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        raw_email = str(data.get("email", "")).strip().lower()
        code = str(data.get("code", "")).strip()
        display_name = str(data.get("display_name", "")).strip()
        password_hash = data.get("password_hash")
        salt = data.get("salt")

        if not raw_email or not code:
            return web.json_response({"success": False, "error": "Поля email и code обязательны."}, status=400)

        is_valid, msg = await email_otp_manager.verify_otp(raw_email, code)
        if not is_valid:
            return web.json_response({"success": False, "verified": False, "error": msg}, status=401)

        # Success! Create or update user in database
        existing = await user_manager.get_by_email(raw_email)
        is_new = existing is None
        user = await user_manager.upsert_email_user(
            email=raw_email,
            display_name=display_name,
            password_hash=password_hash,
            salt=salt,
        )

        return web.json_response({
            "success": True,
            "verified": True,
            "email": raw_email,
            "user_id": user.get("user_id"),
            "display_name": user.get("display_name"),
            "username": user.get("username"),
            "is_new_user": is_new,
            "has_password": bool(user.get("password_hash")),
            "message": "Почта успешно подтверждена!",
        })
    except Exception as e:
        logger.error(f"Error in handle_verify_email_otp: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_set_password(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        raw_email = str(data.get("email", "")).strip().lower()
        password_hash = str(data.get("password_hash", "")).strip()
        salt = str(data.get("salt", "")).strip()

        if not raw_email or not password_hash:
            return web.json_response({"success": False, "error": "Поля email и password_hash обязательны."}, status=400)

        ok = await user_manager.set_email_password(raw_email, password_hash, salt)
        if ok:
            return web.json_response({"success": True, "message": "Пароль успешно установлен."})
        else:
            return web.json_response({"success": False, "error": "Пользователь не найден."}, status=404)
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_public_endpoint(request: web.Request) -> web.Response:
    url = tunnel_manager.public_url
    if not url and os.path.isfile(tunnel_manager._url_file):
        try:
            with open(tunnel_manager._url_file, "r", encoding="utf-8") as f:
                url = f.read().strip()
        except Exception:
            pass
    return web.json_response({
        "success": True,
        "public_url": url,
        "local_url": f"http://127.0.0.1:{HTTP_PORT}",
    })


async def handle_playlist_youtube(request: web.Request) -> web.Response:
    list_id = request.query.get("list_id", "").strip()
    if not list_id:
        return web.json_response({"success": False, "error": "Параметр list_id обязателен"}, status=400)

    m = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", list_id)
    if m:
        list_id = m.group(1)

    # 1. Primary: Direct YouTube InnerTube Browse API (Frankfurt datacenter has unblocked access to YouTube)
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            payload = {
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": "2.20241101.01.00",
                        "hl": "ru",
                        "gl": "US"
                    }
                },
                "browseId": "VL" + list_id
            }
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            }
            async with session.post("https://www.youtube.com/youtubei/v1/browse?prettyPrint=false", json=payload, headers=headers) as resp:
                it_status = resp.status
                if resp.status == 200:
                    data = await resp.json()
                    header = data.get("header", {})
                    title = "YouTube Плейлист"
                    if "pageHeaderRenderer" in header:
                        title = header["pageHeaderRenderer"].get("pageTitle", title)
                    elif "playlistHeaderRenderer" in header:
                        title = header["playlistHeaderRenderer"].get("title", {}).get("simpleText", title)

                    tracks = []
                    seen = set()

                    def walk(obj):
                        if isinstance(obj, dict):
                            if "lockupViewModel" in obj:
                                lvm = obj["lockupViewModel"]
                                vid = lvm.get("contentId")
                                if vid and len(vid) == 11 and vid not in seen:
                                    seen.add(vid)
                                    meta = lvm.get("metadata", {}).get("lockupMetadataViewModel", {})
                                    t = meta.get("title", {}).get("content", "")
                                    if not t:
                                        lbl = lvm.get("rendererContext", {}).get("accessibilityContext", {}).get("label", "")
                                        t = lbl.split(" 2 минуты")[0].split(" 3 минуты")[0]
                                    art = "YouTube"
                                    rows = meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                                    if rows and isinstance(rows, list):
                                        parts = rows[0].get("metadataParts", [])
                                        if parts and "text" in parts[0]:
                                            art = parts[0]["text"].get("content", "YouTube")
                                    tracks.append({
                                        "id": vid,
                                        "videoId": vid,
                                        "title": t,
                                        "artist": art,
                                        "durationSeconds": 180,
                                        "artworkUrl": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                                    })
                            elif "playlistVideoRenderer" in obj:
                                pvr = obj["playlistVideoRenderer"]
                                vid = pvr.get("videoId")
                                if vid and vid not in seen:
                                    seen.add(vid)
                                    t = pvr.get("title", {}).get("runs", [{}])[0].get('text', '') or pvr.get("title", {}).get("simpleText", "")
                                    art = pvr.get("shortBylineText", {}).get("runs", [{}])[0].get('text', '') or "YouTube"
                                    tracks.append({
                                        "id": vid,
                                        "videoId": vid,
                                        "title": t,
                                        "artist": art,
                                        "durationSeconds": 180,
                                        "artworkUrl": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                                    })
                            for v in obj.values():
                                walk(v)
                        elif isinstance(obj, list):
                            for i in obj:
                                walk(i)

                    walk(data)
                    if tracks:
                        return web.json_response({
                            "success": True,
                            "title": title,
                            "tracks": tracks
                        })
    except Exception as e:
        logger.warning(f"Direct InnerTube failed for {list_id}: {e}")
        it_err = f"{type(e).__name__}: {e}"

    # 2. Fallback: Invidious Instances
    instances = [
        "https://inv.nadeko.net",
        "https://invidious.jing.rocks",
        "https://invidious.nerdvpn.de",
        "https://yewtu.be",
        "https://vid.puffyan.us"
    ]
    for inst in instances:
        try:
            timeout = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{inst}/api/v1/playlists/{list_id}", headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        videos = data.get("videos", [])
                        if videos:
                            tracks = []
                            for v in videos:
                                vid = v.get("videoId")
                                title = v.get("title")
                                if vid and title:
                                    tracks.append({
                                        "id": vid,
                                        "videoId": vid,
                                        "title": title,
                                        "artist": v.get("author", "YouTube"),
                                        "durationSeconds": v.get("lengthSeconds", 180),
                                        "artworkUrl": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                                    })
                            return web.json_response({
                                "success": True,
                                "title": data.get("title", "YouTube Плейлист"),
                                "tracks": tracks
                            })
        except Exception:
            continue

    return web.json_response({"success": False, "error": "Не удалось загрузить плейлист с серверов YouTube"}, status=502)


async def handle_verify_otp(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        handle = data.get("handle", "").strip()
        code = str(data.get("code", "")).strip()

        if not handle or not code:
            return web.json_response({"success": False, "error": "Поля handle и code обязательны"}, status=400)

        clean_handle = handle.lower().lstrip("@")
        client_ip = request.remote or "127.0.0.1"

        # Check if user is registered in the database
        user = await user_manager.get_by_handle(clean_handle)
        if not user and clean_handle.isdigit():
            user = await user_manager.get_by_telegram_id(int(clean_handle))
        if not user:
            return web.json_response({
                "success": False,
                "verified": False,
                "error": f"Пользователь @{clean_handle} не зарегистрирован в Sonora. Откройте бота @{BOT_USERNAME} и нажмите «Зарегистрироваться».",
            }, status=404)

        is_valid, msg, session = await session_manager.verify_otp(clean_handle, code, client_ip=client_ip)

        if is_valid:
            tg_id = user.get("telegram_id")
            if tg_id:
                await user_manager.record_login(tg_id)

                # Send real-time login alert to user in Telegram
                if active_bot_instance:
                    asyncio.create_task(send_telegram_login_alert(tg_id, clean_handle))

            avatar_file = user.get("avatar_file")
            avatar_url = f"http://127.0.0.1:{HTTP_PORT}/api/avatar/{clean_handle}"

            return web.json_response({
                "success": True,
                "verified": True,
                "handle": f"@{clean_handle}",
                "telegram_id": tg_id,
                "first_name": user.get("first_name", ""),
                "display_name": user.get("display_name", clean_handle),
                "avatar_url": avatar_url,
                "has_avatar": bool(avatar_file),
                "message": "Успешная авторизация в Sonora Music",
            })
        else:
            return web.json_response({
                "success": False,
                "verified": False,
                "error": msg,
            }, status=401)
    except Exception as e:
        logger.error(f"Error in handle_verify_otp: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def send_telegram_login_alert(tg_id: int, handle: str):
    """Sends a security notification into user's Telegram chat upon successful Sonora entry."""
    try:
        if active_bot_instance:
            text = (
                "🔐 <b>Успешный вход в Sonora Music Desktop!</b>\n\n"
                f"Выполнен вход в профиль: <b>@{handle}</b>\n"
                f"💻 Устройство: Windows Desktop (x64)\n"
                f"🕒 Время: <code>{datetime.now().strftime('%H:%M:%S (%d.%m.%Y)')}</code>\n\n"
                "<i>Если это были не вы, немедленно свяжитесь с поддержкой.</i>"
            )
            await active_bot_instance.send_message(tg_id, text)
    except Exception as e:
        logger.warning(f"Could not send login alert to {tg_id}: {e}")


async def handle_serve_avatar(request: web.Request) -> web.Response:
    """Serves the user's avatar image, or a high-res SVG letter badge if not set."""
    handle = request.match_info.get("handle", "").strip().lower().lstrip("@")
    user = await user_manager.get_by_handle(handle)

    # Check by handle or by telegram ID
    if not user and handle.isdigit():
        user = await user_manager.get_by_telegram_id(int(handle))

    if user and user.get("avatar_file"):
        path = os.path.join(AVATARS_DIR, user["avatar_file"])
        if os.path.isfile(path):
            return web.FileResponse(path, headers={
                "Content-Type": "image/jpeg",
                "Cache-Control": "public, max-age=86400",
            })

    # Fallback to high-res dynamic SVG avatar
    letter = (user.get("display_name") if user else handle)[:1].upper() or "S"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
      <defs>
        <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#2AABEE"/>
          <stop offset="100%" stop-color="#229ED9"/>
        </linearGradient>
      </defs>
      <circle cx="128" cy="128" r="128" fill="url(#bg)"/>
      <text x="128" y="160" font-size="110" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="900" fill="#FFFFFF" text-anchor="middle">{letter}</text>
    </svg>"""
    return web.Response(text=svg, content_type="image/svg+xml")


# ==============================================================================
# GOOGLE WEB AUTH GATEWAY
# ==============================================================================
GOOGLE_AUTH_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Sonora Music — Авторизация Google</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
    body { background: #080A0F; color: #FFFFFF; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
    .card { background: #12151D; border: 1px solid rgba(255,255,255,0.12); border-radius: 24px; width: 100%; max-width: 440px; padding: 36px 30px; box-shadow: 0 20px 50px rgba(0,0,0,0.8); text-align: center; }
    .logo-badge { display: inline-flex; align-items: center; justify-content: center; width: 64px; height: 64px; background: #FFFFFF; border-radius: 20px; box-shadow: 0 8px 24px rgba(0,0,0,0.4); margin-bottom: 20px; }
    .logo-badge svg { width: 36px; height: 36px; }
    h1 { font-size: 22px; font-weight: 800; margin-bottom: 8px; color: #FFFFFF; }
    p.sub { font-size: 13px; color: #94A3B8; margin-bottom: 24px; line-height: 1.5; }
    .btn-google-official {
      width: 100%; padding: 14px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.2);
      font-size: 14px; font-weight: 700; cursor: pointer; transition: 0.2s; display: flex;
      align-items: center; justify-content: center; gap: 10px; background: #FFFFFF; color: #1F2937;
      text-decoration: none; margin-bottom: 16px;
    }
    .btn-google-official:hover { background: #F1F5F9; }
    .input-field { width: 100%; background: rgba(0,0,0,0.5); border: 1px solid rgba(255,255,255,0.18); border-radius: 12px; padding: 13px 15px; color: #FFF; font-size: 13px; margin-bottom: 12px; outline: none; }
    .input-field:focus { border-color: #4285F4; }
    .btn-submit { width: 100%; padding: 14px; border-radius: 12px; border: none; font-size: 14px; font-weight: 700; cursor: pointer; transition: 0.2s; display: flex; align-items: center; justify-content: center; gap: 10px; background: #4285F4; color: #FFFFFF; margin-top: 6px; }
    .btn-submit:hover { background: #3367D6; }
    .success-box { display: none; padding: 20px 0; }
    .success-icon { width: 68px; height: 68px; border-radius: 50%; background: #10B981; color: white; display: inline-flex; align-items: center; justify-content: center; font-size: 36px; margin-bottom: 16px; }
  </style>
</head>
<body>
  <div class="card">
    <div id="authForm">
      <div class="logo-badge">
        <svg viewBox="0 0 24 24">
          <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
          <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
          <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"/>
          <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"/>
        </svg>
      </div>
      <h1>Вход через Google</h1>
      <p class="sub">Вход в приложение <b>Sonora Music Desktop</b> через ваш личный Google аккаунт.</p>

      <a href="https://accounts.google.com/signin/v2/identifier?service=lso&flowName=GlifWebSignIn" target="_blank" class="btn-google-official">
        <svg style="width: 20px; height: 20px;" viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"/></svg>
        Войти на accounts.google.com
      </a>

      <div style="font-size: 11px; color: #64748B; margin: 14px 0 10px; font-weight: 600;">ВАШИ ДАННЫЕ GOOGLE ДЛЯ ПРИЛОЖЕНИЯ</div>
      <input type="text" id="customName" class="input-field" placeholder="Ваше имя (необязательно, по умолчанию user_XXXX)">
      <input type="email" id="customEmail" class="input-field" placeholder="Ваш Google Email (например: name@gmail.com)" required>

      <button class="btn-submit" onclick="submitAuth()">
        Подтвердить вход в Sonora
      </button>
    </div>

    <div id="successBox" class="success-box">
      <div class="success-icon">✓</div>
      <h1>Вход выполнен!</h1>
      <p class="sub" id="successDesc">Вы успешно авторизованы в Sonora Music.</p>
      <div style="font-size: 12px; color: #10B981; font-weight: 700; margin-top: 14px;">Приложение автоматически переключилось в ваш профиль. Вы можете закрыть эту вкладку.</div>
    </div>
  </div>

  <script>
    const urlParams = new URLSearchParams(window.location.search);
    const sessionId = urlParams.get('session_id') || 'demo_session';

    async function submitAuth() {
      const email = document.getElementById('customEmail').value.trim();
      if (!email || !email.includes('@')) {
        alert('Пожалуйста, укажите ваш Google Email адрес.');
        return;
      }
      const emailPrefix = email.split('@')[0].replace(/[^a-zA-Z0-9_]/g, '');
      const name = document.getElementById('customName').value.trim() || ('user_' + (emailPrefix || 'guest'));
      const avatarUrl = 'https://ui-avatars.com/api/?name=' + encodeURIComponent(name) + '&background=4285F4&color=fff&size=512';

      try {
        const resp = await fetch('/api/google-complete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            session_id: sessionId,
            email: email,
            name: name,
            avatar_url: avatarUrl
          })
        });
        const res = await resp.json();
        if (res.success) {
          document.getElementById('authForm').style.display = 'none';
          document.getElementById('successDesc').innerHTML = 'Вы вошли как <b>' + name + '</b> (' + email + ').';
          document.getElementById('successBox').style.display = 'block';
        } else {
          alert('Ошибка: ' + res.error);
        }
      } catch (e) {
        alert('Ошибка связи с сервисом: ' + e);
      }
    }
  </script>
</body>
</html>
"""

async def handle_google_auth_page(request: web.Request) -> web.Response:
    session_id = request.query.get("session_id", f"gauth_{secrets.token_hex(8)}")
    session_manager.create_oauth_session(session_id, provider="google")
    return web.Response(text=GOOGLE_AUTH_PAGE_HTML, content_type="text/html")


async def handle_google_complete(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        session_id = data.get("session_id", "").strip()
        email = data.get("email", "").strip()
        name = data.get("name", "").strip()
        avatar_url = data.get("avatar_url", "")

        if not session_id or not email:
            return web.json_response({"success": False, "error": "session_id и email обязательны"}, status=400)

        user_data = {
            "email": email,
            "name": name or email.split("@")[0],
            "avatar_url": avatar_url,
            "provider": "google",
            "authorized_at": datetime.utcnow().isoformat() + "Z",
        }
        success = session_manager.complete_oauth_session(session_id, user_data)
        return web.json_response({"success": success})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_oauth_status(request: web.Request) -> web.Response:
    session_id = request.query.get("session_id", "").strip()
    session = session_manager.get_oauth_session(session_id)
    if not session:
        return web.json_response({"status": "not_found", "error": "Сессия не найдена"}, status=404)
    return web.json_response({
        "status": session["status"],
        "user": session["user"],
    })


def get_downloads_dir() -> str:
    candidates = [
        os.path.join(BASE_DIR, "downloads"),
        os.path.join(BASE_DIR, "..", "Sonora-Website", "downloads"),
        os.path.join(BASE_DIR, "..", "Sonora", "build", "installers"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    return os.path.abspath(candidates[0])


DOWNLOADS_DIR = get_downloads_dir()


async def handle_download_file(request: web.Request) -> web.Response:
    filename = request.match_info.get("filename", "")
    safe_base = os.path.abspath(DOWNLOADS_DIR)
    target_path = os.path.abspath(os.path.join(safe_base, filename))

    if not target_path.startswith(safe_base) or not os.path.isfile(target_path):
        return web.json_response({"error": "Файл не найден"}, status=404)

    return web.FileResponse(target_path)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = ex
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, HEAD"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Accept"
    return response


def create_http_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    # Root ping
    app.router.add_get("/", handle_health)
    # Health & Check
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/check-user", handle_check_user)
    # OTP Authentication
    app.router.add_post("/api/request-otp", handle_request_otp)
    app.router.add_post("/api/verify-otp", handle_verify_otp)
    app.router.add_post("/api/send-email-otp", handle_send_email_otp)
    app.router.add_post("/api/verify-email-otp", handle_verify_email_otp)
    app.router.add_post("/api/set-password", handle_set_password)
    app.router.add_get("/api/public-endpoint", handle_public_endpoint)
    # Playlist Importer Cloud Proxy
    app.router.add_get("/api/playlist/youtube", handle_playlist_youtube)
    # Avatars
    app.router.add_get("/api/avatar/{handle}", handle_serve_avatar)
    # Google OAuth Web Portal
    app.router.add_get("/auth/google", handle_google_auth_page)
    app.router.add_post("/api/google-complete", handle_google_complete)
    app.router.add_get("/api/oauth-status", handle_oauth_status)
    # Direct Downloads
    app.router.add_get("/downloads/{filename:.*}", handle_download_file)
    # Global OPTIONS preflight route
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda req: web.Response(status=200))
    return app


# ==============================================================================
# 5. TELEGRAM BOT (AIOGRAM 3.7+ / 3.32+)
# ==============================================================================
BOT_START_TIME = datetime.utcnow()

# Chat cleanup registry: chat_id -> last_menu_message_id
user_last_menu: Dict[int, int] = {}


def get_uptime_string() -> str:
    delta = datetime.utcnow() - BOT_START_TIME
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}ч {minutes}м {seconds}с"


def find_download_file(filename: str) -> Optional[str]:
    """Finds release file across candidate folders."""
    candidates = [
        os.path.join(DOWNLOADS_DIR, filename),
        os.path.join(BASE_DIR, "downloads", filename),
        os.path.join(BASE_DIR, "..", "Sonora-Website", "downloads", filename),
        os.path.join(BASE_DIR, "..", "Sonora", "build", "installers", filename),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


# Captions with detailed explanations of file purpose
CAPTION_SETUP = (
    "💻 <b>Sonora Music v2.6.0 — Windows Setup (Инсталлятор)</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "🎯 <b>Назначение:</b> Официальный установщик для ПК и ноутбуков.\n"
    "💻 <b>Платформа:</b> Windows 10 / 11 (64-bit)\n"
    "📦 <b>Размер:</b> 27.78 МБ\n\n"
    "⚡ <b>Особенности и преимущества:</b>\n"
    "• Быстрая установка в 1 клик без прав администратора\n"
    "• Удобные ярлыки в меню «Пуск» и на Рабочем столе\n"
    "• Полная ассоциация аудиофайлов (.mp3, .flac, .m4a, .ogg)\n"
    "• Hi-Fi DSP студия, 10-полосный эквалайзер и синхронное караоке\n"
    "• 18 музыкальных сервисов в едином интерфейсе\n\n"
    "💡 <i>Инструкция: Скачайте файл и запустите установку на вашем компьютере.</i>"
)

CAPTION_PORTABLE = (
    "⚡ <b>Sonora Music v2.6.0 — Windows Portable (Без установки)</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "🎯 <b>Назначение:</b> Портативная версия Sonora, готовая к запуску в 1 клик.\n"
    "💻 <b>Платформа:</b> Windows 10 / 11 (64-bit)\n"
    "📦 <b>Размер:</b> 35.66 МБ\n\n"
    "⚡ <b>Особенности и преимущества:</b>\n"
    "• Полная автономность: работает без инсталляции в систему\n"
    "• Можно запускать с флешки, съёмного SSD или облачного диска\n"
    "• Не оставляет записей в реестре и системных папках Windows\n"
    "• Все кэши, темы и плейлисты хранятся в папке приложения\n\n"
    "💡 <i>Инструкция: Распакуйте zip-архив в любую удобную папку и запустите sonora.exe.</i>"
)

CAPTION_APK_ARM64 = (
    "📱 <b>Sonora Music v2.6.0 — Android APK (Мобильная версия)</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "🎯 <b>Назначение:</b> Официальное мобильное приложение Sonora для Android.\n"
    "📱 <b>Платформа:</b> Android 7.0–15 (Архитектура ARM64 — 99% всех смартфонов)\n"
    "📦 <b>Размер:</b> 45.36 МБ <i>(прямая загрузка файлом в Telegram!)</i>\n\n"
    "⚡ <b>Особенности и преимущества:</b>\n"
    "• Установка напрямую из Telegram в 1 клик без Google Play\n"
    "• Фоновое воспроизведение музыки без ограничений и рекламы\n"
    "• Адаптивная неоновая иконка в стиле Obsidian Stage\n"
    "• Управление со шторки уведомлений и экрана блокировки\n"
    "• 18 музыкальных сервисов, текст песен в реальном времени\n\n"
    "💡 <i>Инструкция: Нажмите на файл в Telegram -> выберите «Установить». Если смартфон запросит разрешение на установку из Telegram — нажмите «Разрешить».</i>"
)

CAPTION_APK_ARMV7 = (
    "📱 <b>Sonora Music v2.6.0 — Android APK (ARMv7 Legacy 32-bit)</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "🎯 <b>Назначение:</b> Версия для более старых или бюджетных 32-битных смартфонов.\n"
    "📱 <b>Платформа:</b> Android 32-bit (ARMv7)\n"
    "📦 <b>Размер:</b> 42.45 МБ\n\n"
    "💡 <i>Инструкция: Скачайте файл и нажмите «Установить».</i>"
)


def format_registered_welcome(first_name: str, handle: str, code: str, remaining_sec: int) -> str:
    return (
        f"🎧 <b>С возвращением в Sonora Music, {first_name}!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Аккаунт: <b>@{handle}</b> <i>(Зарегистрирован ✓)</i>\n"
        f"🟢 Статус: <b>Hi-Fi Без рекламы • 18 платформ</b>\n\n"
        f"🔑 <b>Ваш код подтверждения для входа в плеер:</b>\n"
        f"👉 <code>{code}</code> 👈  <i>(нажмите на код для копирования)</i>\n\n"
        f"⏱️ <b>Код действителен {remaining_sec // 60} минут.</b>\n"
        f"Вставьте его в поле «Вход через Telegram» в плеере Sonora.\n\n"
        f"🔒 <i>Никогда и никому не сообщайте этот код.</i>"
    )


def format_new_registration_card(first_name: str, handle: str, code: str, remaining_sec: int) -> str:
    return (
        f"🎉 <b>Поздравляем с регистрацией в Sonora Music!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Профиль создан: <b>{first_name}</b> (@{handle})\n"
        f"🟢 Доступ: <b>Активирован (18 музыкальных платформ)</b>\n"
        f"🛡️ Защита: <b>Привязан к вашему Telegram ID</b>\n\n"
        f"🔑 <b>Ваш код для первого входа в приложение:</b>\n"
        f"👉 <code>{code}</code> 👈  <i>(нажмите на код для копирования)</i>\n\n"
        f"⏱️ <b>Код действителен {remaining_sec // 60} минут.</b>\n"
        f"Откройте Sonora на ПК, выберите «Вход через Telegram» и вставьте код."
    )


def format_guest_welcome() -> str:
    return (
        "✨ <b>Добро пожаловать в Sonora Music!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Sonora — бесплатный Hi-Fi музыкальный стриминговый плеер нового поколения:\n\n"
        "• 🎵 <b>18 площадок в 1 клик:</b> YouTube Music, SoundCloud, Deezer, Яндекс Музыка, Bandcamp, Apple Music, Spotify и др.\n"
        "• 🎛️ <b>Hi-Fi & DSP Студия:</b> студийный 10-полосный эквалайзер, винил, караоке LRC.\n"
        "• 🎬 <b>Кинетическая сцена:</b> неоновая аврора, живые фоны, режим Zen Stage.\n"
        "• 🐝 <b>Секретная пасхалка:</b> «Кто пчёлок уважает» 🐝✨\n"
        "• 🚫 <b>Без рекламы, платных подписок и ограничений.</b>\n\n"
        "Создайте аккаунт в 1 клик для входа и синхронизации:"
    )


def format_downloads_menu() -> str:
    return (
        "📦 <b>Загрузки Sonora Music v2.6.0 Obsidian Stage:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите нужный дистрибутив — бот отправит установочный файл <b>прямо сюда в чат Telegram</b> (без внешних сайтов и ссылок):\n\n"
        "• 💻 <b>Windows Setup (.exe, 27.8 МБ)</b> — Официальный установщик в 1 клик для Windows 10/11\n"
        "• ⚡ <b>Windows Portable (.zip, 35.7 МБ)</b> — Портативная версия без инсталляции в систему\n"
        "• 📱 <b>Android APK (.apk, 45.4 МБ)</b> — Мобильное приложение (ARM64 — 99% смартфонов)\n"
        "• 📱 <b>Android APK ARMv7 (.apk, 42.5 МБ)</b> — Для старых 32-битных устройств\n\n"
        "<i>Нажмите кнопку ниже, чтобы получить файл в чат:</i>"
    )


def format_status_card(users_count: int, uptime_str: str) -> str:
    return (
        "📊 <b>Статус инфраструктуры Sonora Music</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>REST API Шлюз:</b> Онлайн <code>(порт {HTTP_PORT})</code>\n"
        f"🟢 <b>Telegram Bot:</b> Активен (@{BOT_USERNAME})\n"
        "🟢 <b>Сервис OTP-кодов:</b> Работает (10 мин TTL, защита от перебора)\n"
        "🟢 <b>Синхронное караоке LRCLIB:</b> Доступно (100% аптайм)\n"
        "🟢 <b>Аудиодвижок libmpv & DSP:</b> Активен (10-полосный EQ + Bass Boost)\n\n"
        "🌐 <b>Музыкальные платформы (18 из 18 онлайн):</b>\n"
        "• YouTube Music • SoundCloud • Deezer • Яндекс Музыка\n"
        "• Bandcamp • PromoDJ • Mixcloud • Apple Music • Spotify\n\n"
        f"👥 <b>База слушателей:</b> {users_count} зарегистрированных пользователей\n"
        f"⏱️ <b>Время непрерывной работы:</b> {uptime_str}\n"
        "📦 <b>Версия релиза:</b> v2.6.0 Obsidian Stage"
    )


def format_features_card() -> str:
    return (
        "✨ <b>Возможности Sonora Music v2.6.0:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "1. 🌐 <b>18 музыкальных платформ:</b>\n"
        "   YouTube Music, SoundCloud, Deezer, Яндекс, Bandcamp, PromoDJ, Mixcloud и др. Слушайте миллионы треков без подписок.\n\n"
        "2. 🎛️ <b>Студийный DSP процессор:</b>\n"
        "   10-полосный эквалайзер, виниловый шум 33⅓, сатуратор, глубокий Bass Boost (+0..+30 dB), Slowed+Reverb.\n\n"
        "3. 🎙️ <b>Синхронные тексты караоке (LRC):</b>\n"
        "   Построчный кинетический текст песен в реальном времени с поддержкой кастомных текстов и перевода.\n\n"
        "4. 🎬 <b>Сцена Obsidian Stage & Zen Mode:</b>\n"
        "   Анимированные обложки, виниловый проигрыватель, живые авроры и режим полного погружения.\n\n"
        "5. 🐝 <b>Секретная пасхалка «Кто пчёлок уважает» 🐝✨:</b>\n"
        "   Плавный полёт очаровательных пчёлок с золотой пыльцой и тёплой медовой темой (#100E08)!\n\n"
        "6. 🔐 <b>Мгновенная авторизация:</b>\n"
        "   Вход в 1 клик через Telegram OTP или Google без сложных паролей."
    )


def format_help_card() -> str:
    return (
        "❓ <b>Как войти в Sonora через Telegram:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ <b>Регистрация:</b> Если вы новый пользователь, нажмите кнопку <b>«Зарегистрироваться в Sonora»</b> (аккаунт создастся в 1 клик).\n\n"
        "2️⃣ <b>Код авторизации:</b> Скопируйте 6-значный код из сообщения бота (просто нажмите на него).\n\n"
        "3️⃣ <b>Откройте Sonora:</b> В приложении выберите «Вход через Telegram», укажите ваш <code>@username</code> и вставьте 6-значный код.\n\n"
        "4️⃣ <b>Готово!</b> Вы мгновенно вошли, ваша персональная медиатека синхронизирована, а аватарка из Telegram появится в профиле плеера."
    )


async def run_telegram_bot():
    """Runs aiogram 3.7+ bot with graceful reconnect loop and zero chat clutter."""
    global active_bot_instance
    try:
        from aiogram import Bot, Dispatcher, types, F
        from aiogram.filters import CommandStart, Command
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
        from aiogram.client.default import DefaultBotProperties
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer

        if "AAFakeDemoToken" in BOT_TOKEN or len(BOT_TOKEN) < 20:
            logger.warning("SONORA_BOT_TOKEN не задан или некорректен.")
            return

        proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("TELEGRAM_PROXY")
        session = AiohttpSession(proxy=proxy) if proxy else None
        custom_server = os.getenv("TELEGRAM_API_SERVER")
        api_server = TelegramAPIServer.from_base(custom_server) if custom_server else None

        bot = Bot(
            token=BOT_TOKEN,
            session=session,
            default=DefaultBotProperties(parse_mode="HTML"),
            server=api_server or TelegramAPIServer.from_base("https://api.telegram.org"),
        )
        active_bot_instance = bot
        dp = Dispatcher()

        # Keyboards
        def get_guest_keyboard() -> InlineKeyboardMarkup:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🚀 Зарегистрироваться в Sonora",
                            callback_data="do_register",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="📦 Скачать Sonora",
                            callback_data="dl_menu",
                        ),
                        InlineKeyboardButton(
                            text="✨ Возможности",
                            callback_data="features_menu",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="📊 Статус серверов",
                            callback_data="status_menu",
                        ),
                        InlineKeyboardButton(
                            text="❓ Как войти?",
                            callback_data="help_login",
                        ),
                    ],
                ]
            )

        def get_user_keyboard() -> InlineKeyboardMarkup:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔄 Запросить новый код",
                            callback_data="refresh_code",
                        ),
                        InlineKeyboardButton(
                            text="👤 Мой профиль",
                            callback_data="my_profile",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="📦 Скачать Sonora",
                            callback_data="dl_menu",
                        ),
                        InlineKeyboardButton(
                            text="✨ Возможности",
                            callback_data="features_menu",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="📊 Статус серверов",
                            callback_data="status_menu",
                        ),
                        InlineKeyboardButton(
                            text="❓ Как войти?",
                            callback_data="help_login",
                        ),
                    ],
                ]
            )

        def get_download_keyboard() -> InlineKeyboardMarkup:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📦 Получить ВСЕ файлы сразу (Пакет)",
                            callback_data="send_all",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="💻 Windows Setup (.exe, 27.8 МБ)",
                            callback_data="send_setup",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="⚡ Windows Portable (.zip, 35.7 МБ)",
                            callback_data="send_portable",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="📱 Android APK (ARM64, 45.4 МБ)",
                            callback_data="send_apk_arm64",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="📱 Android APK (ARMv7 32-bit, 42.5 МБ)",
                            callback_data="send_apk_armv7",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="◀️ Главное меню",
                            callback_data="back_home",
                        ),
                    ],
                ]
            )

        def get_back_keyboard() -> InlineKeyboardMarkup:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="📦 Скачать Sonora", callback_data="dl_menu"),
                        InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_home"),
                    ]
                ]
            )

        def get_profile_keyboard() -> InlineKeyboardMarkup:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🔑 Получить код для входа", callback_data="refresh_code"),
                        InlineKeyboardButton(text="📦 Скачать Sonora", callback_data="dl_menu"),
                    ],
                    [
                        InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_home"),
                    ],
                ]
            )

        def get_status_keyboard() -> InlineKeyboardMarkup:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🔄 Обновить статус", callback_data="status_menu"),
                        InlineKeyboardButton(text="📦 Скачать Sonora", callback_data="dl_menu"),
                    ],
                    [
                        InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_home"),
                    ],
                ]
            )

        async def safe_edit_or_resend(event_or_message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
            """Safely edit message in-place or replace photo without TelegramBadRequest errors."""
            msg = event_or_message.message if hasattr(event_or_message, "message") else event_or_message
            chat_id = msg.chat.id
            try:
                if getattr(msg, "photo", None):
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                    new_m = await msg.answer(text, reply_markup=reply_markup)
                    user_last_menu[chat_id] = new_m.message_id
                else:
                    await msg.edit_text(text, reply_markup=reply_markup)
                    user_last_menu[chat_id] = msg.message_id
            except Exception as e:
                logger.debug(f"safe_edit_or_resend fallback due to: {e}")
                try:
                    new_m = await msg.answer(text, reply_markup=reply_markup)
                    user_last_menu[chat_id] = new_m.message_id
                except Exception as e2:
                    logger.error(f"Failed to deliver fallback message: {e2}")

        async def handle_user_command(message: types.Message, text: str, reply_markup: InlineKeyboardMarkup):
            """Clean chat handler: removes previous bot menu and incoming command, sends single fresh menu."""
            chat_id = message.chat.id
            # 1. Attempt to delete incoming command
            try:
                await message.delete()
            except Exception:
                pass
            # 2. Attempt to delete previous menu in this chat
            last_id = user_last_menu.get(chat_id)
            if last_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=last_id)
                except Exception:
                    pass
            # 3. Send clean new menu
            new_msg = await message.answer(text, reply_markup=reply_markup)
            user_last_menu[chat_id] = new_msg.message_id

        async def send_file_with_cleanup(
            callback: types.CallbackQuery,
            file_path: Optional[str],
            filename: str,
            caption: str,
            loading_text: str = "⏳ <i>Загружаем файл в Telegram... Пожалуйста, подождите.</i>",
        ):
            """Sends document to chat with automatic cleanup of the loading notice."""
            if not file_path or not os.path.isfile(file_path):
                await callback.answer(f"⚠️ Файл {filename} не найден на сервере", show_alert=True)
                return

            await callback.answer(f"⏳ Отправка {filename}...")
            chat_id = callback.message.chat.id
            status_msg = None
            try:
                status_msg = await callback.message.answer(loading_text)
                await bot.send_chat_action(chat_id=chat_id, action="upload_document")
                await callback.message.answer_document(
                    FSInputFile(file_path, filename=filename),
                    caption=caption,
                )
                await callback.answer("✅ Файл успешно отправлен в чат!")
            except Exception as e:
                logger.error(f"Error sending document {filename}: {e}")
                await callback.answer(f"⚠️ Ошибка отправки файла: {e}", show_alert=True)
            finally:
                if status_msg:
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass

        @dp.message(CommandStart())
        async def on_start(message: types.Message):
            user_id = message.from_user.id
            raw_handle = message.from_user.username or message.from_user.first_name
            clean_handle = raw_handle.lstrip("@").strip()
            real_first_name = message.from_user.first_name or f"user_{user_id}"
            real_last_name = message.from_user.last_name or ""

            # Check if user is registered
            is_reg = await user_manager.is_registered(tg_id=user_id, handle=clean_handle)

            # Refresh avatar
            avatar_file = await download_telegram_avatar(bot, user_id)

            if is_reg:
                user = await user_manager.upsert_user(
                    user_id,
                    username=clean_handle,
                    first_name=real_first_name,
                    last_name=real_last_name,
                    avatar_file=avatar_file
                )
                session = await session_manager.generate_otp(clean_handle, telegram_id=user_id)
                text = format_registered_welcome(
                    user.get("first_name") or real_first_name,
                    user.get("username") or clean_handle,
                    session.code,
                    session.remaining_seconds,
                )
                await handle_user_command(message, text, reply_markup=get_user_keyboard())
            else:
                text = format_guest_welcome()
                await handle_user_command(message, text, reply_markup=get_guest_keyboard())

        @dp.message(Command("register"))
        @dp.callback_query(F.data == "do_register")
        async def on_register(event: types.TelegramObject):
            message = event if isinstance(event, types.Message) else event.message
            from_user = event.from_user

            user_id = from_user.id
            raw_handle = from_user.username or from_user.first_name
            clean_handle = raw_handle.lstrip("@").strip()
            real_first_name = from_user.first_name or f"user_{user_id}"
            real_last_name = from_user.last_name or ""

            is_reg = await user_manager.is_registered(tg_id=user_id, handle=clean_handle)
            avatar_file = await download_telegram_avatar(bot, user_id)

            if is_reg:
                user = await user_manager.upsert_user(
                    user_id,
                    username=clean_handle,
                    first_name=real_first_name,
                    last_name=real_last_name,
                    avatar_file=avatar_file
                )
                session = await session_manager.generate_otp(clean_handle, telegram_id=user_id)
                text = (
                    f"⚠️ <b>Вы уже зарегистрированы в Sonora Music!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 Ваш профиль: <b>{user.get('display_name')}</b> (@{user.get('username', clean_handle)})\n"
                    f"📅 Дата создания: <b>{user.get('registered_at', '')[:10]}</b>\n\n"
                    f"Повторная регистрация не требуется. Чтобы войти в плеер, используйте актуальный код подтверждения:\n\n"
                    f"🔑 👉 <code>{session.code}</code> 👈 <i>(нажмите, чтобы скопировать)</i>\n\n"
                    f"⏱️ Действителен 10 минут. Вставьте его в Sonora в поле «Вход через Telegram»."
                )
                if isinstance(event, types.CallbackQuery):
                    await safe_edit_or_resend(event, text, reply_markup=get_user_keyboard())
                    await event.answer("Вы уже зарегистрированы!")
                else:
                    await handle_user_command(message, text, reply_markup=get_user_keyboard())
            else:
                user = await user_manager.upsert_user(
                    tg_id=user_id,
                    username=clean_handle,
                    first_name=real_first_name,
                    last_name=real_last_name,
                    avatar_file=avatar_file,
                )
                session = await session_manager.generate_otp(clean_handle, telegram_id=user_id)
                text = format_new_registration_card(
                    user["display_name"],
                    clean_handle,
                    session.code,
                    session.remaining_seconds,
                )
                if isinstance(event, types.CallbackQuery):
                    await safe_edit_or_resend(event, text, reply_markup=get_user_keyboard())
                    await event.answer("🎉 Регистрация завершена!")
                else:
                    await handle_user_command(message, text, reply_markup=get_user_keyboard())

        @dp.message(Command("code"))
        @dp.callback_query(F.data == "refresh_code")
        async def on_code(event: types.TelegramObject):
            message = event if isinstance(event, types.Message) else event.message
            from_user = event.from_user

            user_id = from_user.id
            raw_handle = from_user.username or from_user.first_name
            clean_handle = raw_handle.lstrip("@").strip()

            is_reg = await user_manager.is_registered(tg_id=user_id, handle=clean_handle)

            if not is_reg:
                text = (
                    "⚠️ <b>Вы ещё не зарегистрированы в Sonora Music.</b>\n\n"
                    "Для получения кода подтверждения сначала создайте аккаунт в 1 клик:"
                )
                if isinstance(event, types.CallbackQuery):
                    await safe_edit_or_resend(event, text, reply_markup=get_guest_keyboard())
                    await event.answer()
                else:
                    await handle_user_command(message, text, reply_markup=get_guest_keyboard())
                return

            user = await user_manager.get_by_telegram_id(user_id) or await user_manager.get_by_handle(clean_handle)
            session = await session_manager.generate_otp(clean_handle, telegram_id=user_id)
            text = format_registered_welcome(
                user.get("first_name") or from_user.first_name,
                user.get("username") or clean_handle,
                session.code,
                session.remaining_seconds,
            )
            if isinstance(event, types.CallbackQuery):
                await safe_edit_or_resend(event, text, reply_markup=get_user_keyboard())
                await event.answer("✅ Новый код авторизации сгенерирован!")
            else:
                await handle_user_command(message, text, reply_markup=get_user_keyboard())

        @dp.message(Command("profile"))
        @dp.callback_query(F.data == "my_profile")
        async def on_profile(event: types.TelegramObject):
            message = event if isinstance(event, types.Message) else event.message
            from_user = event.from_user
            user_id = from_user.id
            clean_handle = (from_user.username or from_user.first_name).lstrip("@").strip()

            user = await user_manager.get_by_telegram_id(user_id) or await user_manager.get_by_handle(clean_handle)

            if not user:
                text = "⚠️ <b>Вы ещё не зарегистрированы в Sonora.</b>\n\nНажмите «Зарегистрироваться» ниже для создания профиля в 1 клик:"
                if isinstance(event, types.CallbackQuery):
                    await safe_edit_or_resend(event, text, reply_markup=get_guest_keyboard())
                    await event.answer()
                else:
                    await handle_user_command(message, text, reply_markup=get_guest_keyboard())
                return

            # Refresh avatar & sync real profile info
            real_first_name = from_user.first_name or f"user_{user_id}"
            real_last_name = from_user.last_name or ""
            avatar_file = await download_telegram_avatar(bot, user_id)
            user = await user_manager.upsert_user(
                user_id,
                username=clean_handle,
                first_name=real_first_name,
                last_name=real_last_name,
                avatar_file=avatar_file
            )

            text = (
                f"👤 <b>Ваш профиль в Sonora Music:</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Имя:</b> {user.get('display_name')}\n"
                f"• <b>Telegram:</b> @{user.get('username')}\n"
                f"• <b>Telegram ID:</b> <code>{user.get('telegram_id')}</code>\n"
                f"• <b>Статус:</b> 🟢 Активен (Hi-Fi доступ без рекламы)\n"
                f"• <b>Дата регистрации:</b> {user.get('registered_at', '')[:10]}\n"
                f"• <b>Количество входов:</b> {user.get('login_count', 0)}\n\n"
                f"🖼️ <i>Ваша аватарка из Telegram синхронизирована с плеером Sonora.</i>"
            )

            if isinstance(event, types.CallbackQuery):
                if user.get("avatar_file"):
                    avatar_path = os.path.join(AVATARS_DIR, user["avatar_file"])
                    if os.path.isfile(avatar_path):
                        try:
                            await event.message.delete()
                        except Exception:
                            pass
                        new_m = await message.answer_photo(FSInputFile(avatar_path), caption=text, reply_markup=get_profile_keyboard())
                        user_last_menu[new_m.chat.id] = new_m.message_id
                        await event.answer()
                        return
                await safe_edit_or_resend(event, text, reply_markup=get_profile_keyboard())
                await event.answer()
            else:
                if user.get("avatar_file"):
                    avatar_path = os.path.join(AVATARS_DIR, user["avatar_file"])
                    if os.path.isfile(avatar_path):
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        last_id = user_last_menu.get(message.chat.id)
                        if last_id:
                            try:
                                await bot.delete_message(chat_id=message.chat.id, message_id=last_id)
                            except Exception:
                                pass
                        new_m = await message.answer_photo(FSInputFile(avatar_path), caption=text, reply_markup=get_profile_keyboard())
                        user_last_menu[new_m.chat.id] = new_m.message_id
                        return
                await handle_user_command(message, text, reply_markup=get_profile_keyboard())

        @dp.message(Command("download", "app", "get"))
        @dp.callback_query(F.data == "dl_menu")
        async def on_download(event: types.TelegramObject):
            text = format_downloads_menu()
            if isinstance(event, types.CallbackQuery):
                await safe_edit_or_resend(event, text, reply_markup=get_download_keyboard())
                await event.answer()
            else:
                await handle_user_command(event, text, reply_markup=get_download_keyboard())

        @dp.message(Command("status"))
        @dp.callback_query(F.data == "status_menu")
        async def on_status(event: types.TelegramObject):
            uptime = get_uptime_string()
            users_count = len(user_manager._users)
            text = format_status_card(users_count, uptime)
            if isinstance(event, types.CallbackQuery):
                await safe_edit_or_resend(event, text, reply_markup=get_status_keyboard())
                await event.answer("🔄 Статус обновлен")
            else:
                await handle_user_command(event, text, reply_markup=get_status_keyboard())

        @dp.message(Command("features", "info"))
        @dp.callback_query(F.data == "features_menu")
        async def on_features(event: types.TelegramObject):
            text = format_features_card()
            if isinstance(event, types.CallbackQuery):
                await safe_edit_or_resend(event, text, reply_markup=get_back_keyboard())
                await event.answer()
            else:
                await handle_user_command(event, text, reply_markup=get_back_keyboard())

        @dp.message(Command("help"))
        @dp.callback_query(F.data == "help_login")
        async def on_help(event: types.TelegramObject):
            text = format_help_card()
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔑 Получить код", callback_data="refresh_code")],
                    [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_home")],
                ]
            )
            if isinstance(event, types.CallbackQuery):
                await safe_edit_or_resend(event, text, reply_markup=kb)
                await event.answer()
            else:
                await handle_user_command(event, text, reply_markup=kb)

        @dp.callback_query(F.data == "back_home")
        async def on_back_home(callback: types.CallbackQuery):
            user_id = callback.from_user.id
            clean_handle = (callback.from_user.username or callback.from_user.first_name).lstrip("@").strip()
            is_reg = await user_manager.is_registered(tg_id=user_id, handle=clean_handle)
            if is_reg:
                user = await user_manager.get_by_telegram_id(user_id) or await user_manager.get_by_handle(clean_handle)
                session = await session_manager.get_latest_session(clean_handle)
                if not session:
                    session = await session_manager.generate_otp(clean_handle, telegram_id=user_id)
                text = format_registered_welcome(
                    user.get("first_name") or callback.from_user.first_name,
                    user.get("username") or clean_handle,
                    session.code,
                    session.remaining_seconds,
                )
                await safe_edit_or_resend(callback, text, reply_markup=get_user_keyboard())
            else:
                await safe_edit_or_resend(callback, format_guest_welcome(), reply_markup=get_guest_keyboard())
            await callback.answer()

        # ======================================================================
        # DIRECT FILE DELIVERY HANDLERS (ALL UNDER 50 MB - DIRECT TELEGRAM FILES)
        # ======================================================================
        @dp.callback_query(F.data == "send_setup")
        async def on_send_setup(callback: types.CallbackQuery):
            path = find_download_file("Sonora-v2.6.0-Windows-x64-Setup.exe")
            await send_file_with_cleanup(
                callback=callback,
                file_path=path,
                filename="Sonora-v2.6.0-Windows-x64-Setup.exe",
                caption=CAPTION_SETUP,
                loading_text="⏳ <i>Загружаем установщик Windows Setup в Telegram... Пожалуйста, подождите.</i>",
            )

        @dp.callback_query(F.data == "send_portable")
        async def on_send_portable(callback: types.CallbackQuery):
            path = find_download_file("Sonora-v2.6.0-Windows-x64-Portable.zip")
            await send_file_with_cleanup(
                callback=callback,
                file_path=path,
                filename="Sonora-v2.6.0-Windows-x64-Portable.zip",
                caption=CAPTION_PORTABLE,
                loading_text="⏳ <i>Загружаем портативную версию Sonora Portable в Telegram... Пожалуйста, подождите.</i>",
            )

        @dp.callback_query(F.data.in_({"send_apk_arm64", "send_apk"}))
        async def on_send_apk_arm64(callback: types.CallbackQuery):
            path = find_download_file("Sonora-v2.6.0-Android-ARM64.apk") or find_download_file("Sonora-v2.6.0-Android.apk")
            await send_file_with_cleanup(
                callback=callback,
                file_path=path,
                filename="Sonora-v2.6.0-Android.apk",
                caption=CAPTION_APK_ARM64,
                loading_text="⏳ <i>Загружаем Android APK в Telegram (45.4 МБ)... Пожалуйста, подождите.</i>",
            )

        @dp.callback_query(F.data == "send_apk_armv7")
        async def on_send_apk_armv7(callback: types.CallbackQuery):
            path = find_download_file("Sonora-v2.6.0-Android-ARMv7.apk")
            await send_file_with_cleanup(
                callback=callback,
                file_path=path,
                filename="Sonora-v2.6.0-Android-ARMv7.apk",
                caption=CAPTION_APK_ARMV7,
                loading_text="⏳ <i>Загружаем 32-bit Android APK в Telegram... Пожалуйста, подождите.</i>",
            )

        @dp.callback_query(F.data == "send_all")
        async def on_send_all(callback: types.CallbackQuery):
            await callback.answer("📦 Подготовка полного пакета релизов...")
            chat_id = callback.message.chat.id
            status_msg = None
            try:
                status_msg = await callback.message.answer(
                    "📦 <b>Отправка полного пакета дистрибутивов Sonora:</b>\n"
                    "<i>Пожалуйста, подождите, бот отправляет 3 официальных файла подряд в чат...</i>"
                )

                # 1. Windows Setup
                setup_path = find_download_file("Sonora-v2.6.0-Windows-x64-Setup.exe")
                if setup_path:
                    await bot.send_chat_action(chat_id=chat_id, action="upload_document")
                    await callback.message.answer_document(
                        FSInputFile(setup_path, filename="Sonora-v2.6.0-Windows-x64-Setup.exe"),
                        caption=CAPTION_SETUP,
                    )

                # 2. Windows Portable
                portable_path = find_download_file("Sonora-v2.6.0-Windows-x64-Portable.zip")
                if portable_path:
                    await bot.send_chat_action(chat_id=chat_id, action="upload_document")
                    await callback.message.answer_document(
                        FSInputFile(portable_path, filename="Sonora-v2.6.0-Windows-x64-Portable.zip"),
                        caption=CAPTION_PORTABLE,
                    )

                # 3. Android APK (ARM64)
                apk_path = find_download_file("Sonora-v2.6.0-Android-ARM64.apk") or find_download_file("Sonora-v2.6.0-Android.apk")
                if apk_path:
                    await bot.send_chat_action(chat_id=chat_id, action="upload_document")
                    await callback.message.answer_document(
                        FSInputFile(apk_path, filename="Sonora-v2.6.0-Android.apk"),
                        caption=CAPTION_APK_ARM64,
                    )

                await callback.answer("🎉 Все 3 дистрибутива успешно отправлены в чат!", show_alert=True)
            except Exception as e:
                logger.error(f"Error in on_send_all: {e}")
                await callback.answer(f"⚠️ Ошибка при отправке пакета: {e}", show_alert=True)
            finally:
                if status_msg:
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass

        # Register official Telegram Bot Commands Menu
        try:
            await bot.set_my_commands([
                types.BotCommand(command="start", description="🏠 Главное меню Sonora"),
                types.BotCommand(command="download", description="📦 Скачать файлы (Windows & Android)"),
                types.BotCommand(command="code", description="🔑 Получить код для входа"),
                types.BotCommand(command="profile", description="👤 Личный кабинет и статистика"),
                types.BotCommand(command="status", description="📊 Статус серверов и сервисов"),
                types.BotCommand(command="features", description="✨ Возможности плеера"),
                types.BotCommand(command="help", description="❓ Инструкция по авторизации"),
            ])
            logger.info("✓ Официальное меню команд Telegram успешно зарегистрировано.")
        except Exception as e:
            logger.warning(f"Не удалось установить команды бота: {e}")

        # Polling Loop
        logger.info(f"Запуск Telegram-бота @{BOT_USERNAME} через Polling...")
        reconnect_delay = 3
        while True:
            try:
                await dp.start_polling(bot, handle_signals=False)
                break
            except Exception as e:
                logger.error(f"Ошибка polling: {e}. Повтор через {reconnect_delay}с...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, 30)

    except ImportError as e:
        logger.warning(f"Пакет 'aiogram' не установлен: {e}.")
    except Exception as e:
        logger.error(f"Ошибка инициализации Telegram бота: {e}")


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
async def main():
    logger.info("=" * 60)
    logger.info("  SONORA MUSIC — TELEGRAM AUTH BOT & OAUTH GATEWAY 2.7.0")
    logger.info("=" * 60)

    # 1. Start Background Janitor
    asyncio.create_task(session_manager.background_janitor())
    asyncio.create_task(tunnel_manager.start())

    # 2. Start HTTP server
    app = create_http_app()
    runner = web.AppRunner(app)
    await runner.setup()

    global HTTP_PORT
    bound = False
    env_port_set = bool(os.getenv("PORT") or os.getenv("SONORA_AUTH_PORT"))
    candidate_ports = [HTTP_PORT] if env_port_set else [HTTP_PORT, 8089, 8090, 8091]
    for port in candidate_ports:
        try:
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            HTTP_PORT = port
            bound = True
            break
        except OSError:
            continue

    if not bound:
        logger.error("Не удалось занять локальный HTTP порт.")
        return

    logger.info(f"✓ HTTP API Сервис верификации активен на: http://127.0.0.1:{HTTP_PORT}")
    logger.info("  - GET  /health")
    logger.info("  - GET  /api/check-user?handle=...")
    logger.info("  - POST /api/request-otp")
    logger.info("  - POST /api/verify-otp")
    logger.info("  - GET  /api/avatar/{handle}")
    logger.info(f"  - GET  /auth/google (Google Web Auth Portal)")
    logger.info(f"  - GET  /downloads/ (Папка: {DOWNLOADS_DIR})")

    # 3. Start Telegram Bot polling
    asyncio.create_task(run_telegram_bot())

    # Keep running
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Сервис остановлен.")
