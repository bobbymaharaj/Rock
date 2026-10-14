"""
Group Voice Call Server — Packet-Level Stress Tester
Author: Bobby Maharaj
Purpose: Send realistic UDP load (STUN/RTP) to a self-owned voice
         chat server to measure capacity and failure thresholds.
"""

import asyncio
import json
import logging
import os
import random
import socket
import struct
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set

from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)

# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("VoiceStress")

# -------------------------------------------------------------------
# Config (from environment)
# -------------------------------------------------------------------
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", 0))

if not all([API_ID, API_HASH, BOT_TOKEN, SUPER_ADMIN_ID]):
    raise SystemExit("Missing required env vars: API_ID, API_HASH, BOT_TOKEN, SUPER_ADMIN_ID")

ACCESS_FILE = Path("authorized_users.json")
STATE_FILE = Path("stress_state.json")

# -------------------------------------------------------------------
# Access Control (persistent)
# -------------------------------------------------------------------
class AccessStore:
    def __init__(self, path: Path, super_admin: int):
        self.path = path
        self.super_admin = super_admin
        self.users: Set[int] = {super_admin}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.users = set(data.get("authorized", [])) | {self.super_admin}
            except Exception as e:
                logger.warning("Failed to load access file: %s", e)

    def _save(self):
        self.path.write_text(json.dumps({"authorized": sorted(self.users)}, indent=2))

    def add(self, user_id: int) -> bool:
        if user_id in self.users:
            return False
        self.users.add(user_id)
        self._save()
        return True

    def revoke(self, user_id: int) -> bool:
        if user_id == self.super_admin:
            return False
        if user_id not in self.users:
            return False
        self.users.remove(user_id)
        self._save()
        return True

    def is_authorized(self, user_id: int) -> bool:
        return user_id in self.users

    def list(self) -> List[int]:
        return sorted(self.users)

# -------------------------------------------------------------------
# Packet builders
# -------------------------------------------------------------------
def build_stun_binding_request() -> bytes:
    """RFC 5389 STUN Binding Request."""
    msg_type = 0x0001        # Binding Request
    msg_len = 0x0000
    magic_cookie = 0x2112A442
    transaction_id = os.urandom(12)
    return struct.pack(">HHI", msg_type, msg_len, magic_cookie) + transaction_id

def build_rtp_packet(seq: int, ssrc: int) -> bytes:
    """Minimal RTP packet with opus payload type (111)."""
    version, padding, extension, cc = 2, 0, 0, 0
    b1 = (version << 6) | (padding << 5) | (extension << 4) | cc
    marker, pt = 0, 111
    b2 = (marker << 7) | pt
    timestamp = random.randint(0, 0xFFFFFFFF)
    payload = os.urandom(80)  # ~10 ms opus frame
    header = struct.pack(">BBHII", b1, b2, seq & 0xFFFF, timestamp, ssrc)
    return header + payload

def build_random_packet() -> bytes:
    return os.urandom(random.randint(64, 512))

# -------------------------------------------------------------------
# Packet Sender
# -------------------------------------------------------------------
class PacketSender:
    def __init__(self, target_ip: str, target_port: int, pps: int, packet_type: str):
        self.target_ip = target_ip
        self.target_port = target_port
        self.pps = max(1, pps)
        self.packet_type = packet_type
        self.sent = 0
        self.failed = 0
        self.running = False
        self._seq = 0
        self._ssrc = random.randint(1, 0xFFFFFFFF)
        self._task: Optional[asyncio.Task] = None

    def _build_packet(self) -> bytes:
        if self.packet_type == "stun":
            return build_stun_binding_request()
        if self.packet_type == "rtp":
            self._seq = (self._seq + 1) & 0xFFFF
            return build_rtp_packet(self._seq, self._ssrc)
        return build_random_packet()

    async def _loop(self):
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        interval = 1.0 / self.pps
        next_send = loop.time()
        try:
            while self.running:
                now = loop.time()
                if now >= next_send:
                    try:
                        await loop.sock_sendto(
                            sock, self._build_packet(),
                            (self.target_ip, self.target_port)
                        )
                        self.sent += 1
                    except Exception:
                        self.failed += 1
                    next_send += interval
                    if next_send < now:  # we're behind
                        next_send = now + interval
                else:
                    await asyncio.sleep(min(next_send - now, 0.001))
        finally:
            sock.close()

    def start(self):
        self.running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

# -------------------------------------------------------------------
# Stress Manager
# -------------------------------------------------------------------
@dataclass
class StressConfig:
    target_ip: Optional[str] = None
    target_port: Optional[int] = None
    pps: int = 200                # packets per second (total across all senders)
    senders: int = 1              # parallel sender tasks
    duration: int = 60            # seconds; 0 = until stopped manually
    packet_type: str = "stun"     # stun | rtp | random
    running: bool = False
    started_at: float = 0.0

class StressManager:
    def __init__(self):
        self.config = StressConfig()
        self.access = AccessStore(ACCESS_FILE, SUPER_ADMIN_ID)
        self.senders: List[PacketSender] = []
        self._auto_stop_task: Optional[asyncio.Task] = None
        self.bot = Client("voice_stress_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
        self._register_handlers()

    # ---------------- Bot handlers ----------------
    def _register_handlers(self):
        @self.bot.on_message(filters.command(["start", "admin"]) & filters.private)
        async def cmd_start(_, msg: Message):
            if not self.access.is_authorized(msg.from_user.id):
                return await msg.reply_text(
                    "🚫 Unauthorized.\nAsk the super admin to grant you access."
                )
            await self.show_panel(msg)

        @self.bot.on_message(filters.command("help") & filters.private)
        async def cmd_help(_, msg: Message):
            if not self.access.is_authorized(msg.from_user.id):
                return
            await msg.reply_text(
                "**Help**\n\n"
                "• /admin — open the admin panel\n"
                "• /help — this message\n\n"
                "Use the buttons in /admin to set target, rate, duration, and type, "
                "then press **Start**.\n\n"
                "Author: **Bobby Maharaj**"
            )

        @self.bot.on_callback_query()
        async def on_callback(_, query: CallbackQuery):
            if not self.access.is_authorized(query.from_user.id):
                return await query.answer("Unauthorized.", show_alert=True)
            await self.handle_callback(query)

    # ---------------- Panel ----------------
    async def panel_text(self) -> str:
        c = self.config
        status = "🟢 RUNNING" if c.running else "🔴 STOPPED"
        elapsed = int(time.time() - c.started_at) if c.running and c.started_at else 0
        total_sent = sum(s.sent for s in self.senders)
        total_failed = sum(s.failed for s in self.senders)
        return (
            "**🎙️ Voice Server Stress Tester**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"**Status:** {status}\n"
            f"**Target:** `{c.target_ip or '—'}:{c.target_port or '—'}`\n"
            f"**Rate:** `{c.pps} PPS` × `{c.senders}` sender(s)\n"
            f"**Duration:** `{c.duration if c.duration else '∞'}s` (elapsed: `{elapsed}s`)\n"
            f"**Packet Type:** `{c.packet_type}`\n"
            f"**Sent:** `{total_sent}`  **Failed:** `{total_failed}`\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_Author: Bobby Maharaj_"
        )

    def panel_kb(self, user_id: int) -> InlineKeyboardMarkup:
        c = self.config
        rows = [
            [InlineKeyboardButton("🎯 Set Target", callback_data="cfg_target")],
            [InlineKeyboardButton("📶 Set Rate (PPS)", callback_data="cfg_pps"),
             InlineKeyboardButton("🔀 Senders", callback_data="cfg_senders")],
            [InlineKeyboardButton("⏱️ Set Duration", callback_data="cfg_duration")],
            [InlineKeyboardButton(
                f"📦 Packet: {c.packet_type.upper()}", callback_data="cfg_type")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
        ]
        if c.running:
            rows.append([InlineKeyboardButton("⛔ STOP", callback_data="stop")])
        else:
            rows.append([InlineKeyboardButton("▶️ START", callback_data="start")])
        if user_id == SUPER_ADMIN_ID:
            rows.append([InlineKeyboardButton("👥 User Access", callback_data="users")])
        return InlineKeyboardMarkup(rows)

    async def show_panel(self, target):
        text = await self.panel_text()
        kb = self.panel_kb(target.from_user.id if isinstance(target, Message) else target.from_user.id)
        if isinstance(target, Message):
            await target.reply_text(text, reply_markup=kb)
        else:
            try:
                await target.edit_message_text(text, reply_markup=kb)
            except Exception:
                pass

    # ---------------- Callbacks ----------------
    async def handle_callback(self, q: CallbackQuery):
        data = q.data
        uid = q.from_user.id

        if data == "refresh":
            return await self.show_panel(q)

        if data == "cfg_target":
            await q.message.reply_text(
                "Send target in format `ip:port`\nExample: `1.2.3.4:3478`"
            )
            return await q.answer()

        if data == "cfg_pps":
            await q.message.reply_text("Send total packets per second (1–100000).")
            return await q.answer()

        if data == "cfg_senders":
            await q.message.reply_text("Send number of parallel sender tasks (1–64).")
            return await q.answer()

        if data == "cfg_duration":
            await q.message.reply_text("Send duration in seconds (0 = until stopped).")
            return await q.answer()

        if data == "cfg_type":
            # rotate type
            order = ["stun", "rtp", "random"]
            self.config.packet_type = order[(order.index(self.config.packet_type) + 1) % 3]
            return await self.show_panel(q)

        if data == "start":
            await self.start_test()
            return await self.show_panel(q)

        if data == "stop":
            await self.stop_test()
            return await self.show_panel(q)

        if data == "users" and uid == SUPER_ADMIN_ID:
            return await self.show_users(q)

        if data.startswith("user_add"):
            await q.message.reply_text("Send the numeric Telegram user ID to authorize.")
            return await q.answer()

        if data.startswith("user_del:"):
            target_id = int(data.split(":", 1)[1])
            ok = self.access.revoke(target_id)
            await q.answer("Revoked." if ok else "Cannot revoke.", show_alert=True)
            return await self.show_users(q)

        if data == "back":
            return await self.show_panel(q)

    # ---------------- User management ----------------
    async def show_users(self, q: CallbackQuery):
        ids = self.access.list()
        lines = ["**👥 Authorized Users**", "━━━━━━━━━━━━━━━━━━━━"]
        rows = []
        for i in ids:
            tag = " ⭐ (super admin)" if i == SUPER_ADMIN_ID else ""
            lines.append(f"• `{i}`{tag}")
            if i != SUPER_ADMIN_ID:
                rows.append([InlineKeyboardButton(f"❌ Revoke {i}", callback_data=f"user_del:{i}")])
        rows.append([InlineKeyboardButton("➕ Add User", callback_data="user_add")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        await q.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
        await q.answer()

    # ---------------- Control ----------------
    async def start_test(self):
        c = self.config
        if c.running:
            return
        if not c.target_ip or not c.target_port:
            return
        c.running = True
        c.started_at = time.time()

        per_sender_pps = max(1, c.pps // max(1, c.senders))
        for _ in range(c.senders):
            s = PacketSender(c.target_ip, c.target_port, per_sender_pps, c.packet_type)
            s.start()
            self.senders.append(s)

        if c.duration > 0:
            self._auto_stop_task = asyncio.create_task(self._auto_stop(c.duration))

        logger.info("Started stress: %s:%s @ %s PPS x%s",
                    c.target_ip, c.target_port, c.pps, c.senders)

    async def _auto_stop(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.config.running:
                await self.stop_test()
        except asyncio.CancelledError:
            pass

    async def stop_test(self):
        self.config.running = False
        for s in self.senders:
            await s.stop()
        self.senders.clear()
        if self._auto_stop_task:
            self._auto_stop_task.cancel()
            self._auto_stop_task = None
        logger.info("Stopped.")

    # ---------------- Message → config router ----------------
    async def handle_config_message(self, msg: Message) -> bool:
        """Called for non-command messages from authorized users.
        Return True if consumed."""
        if not self.access.is_authorized(msg.from_user.id):
            return False
        text = (msg.text or "").strip()
        if not text:
            return False

        # Target
        if ":" in text and text.replace(".", "").replace(":", "").isdigit():
            try:
                ip, port = text.split(":", 1)
                self.config.target_ip = ip.strip()
                self.config.target_port = int(port.strip())
                await msg.reply_text(f"✅ Target set to `{self.config.target_ip}:{self.config.target_port}`")
                return True
            except Exception:
                return False

        # Numeric
        if text.isdigit():
            n = int(text)
            # Heuristic: which field to update is ambiguous without state.
            # We use a simple pending-action approach stored per user.
            action = PENDING.pop(msg.from_user.id, None)
            if action == "pps":
                self.config.pps = max(1, min(n, 100000))
                await msg.reply_text(f"✅ Rate = {self.config.pps} PPS")
            elif action == "senders":
                self.config.senders = max(1, min(n, 64))
                await msg.reply_text(f"✅ Senders = {self.config.senders}")
            elif action == "duration":
                self.config.duration = max(0, n)
                await msg.reply_text(f"✅ Duration = {self.config.duration}s")
            elif action == "user_add":
                ok = self.access.add(n)
                await msg.reply_text("✅ Added." if ok else "Already authorized.")
            else:
                return False
            return True

        return False

    # ---------------- Run ----------------
    async def run(self):
        await self.bot.start()
        logger.info("Bot online. Super admin: %s", SUPER_ADMIN_ID)
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop_test()
            await self.bot.stop()

# Pending action store (simple in-memory state per user)
PENDING: Dict[int, str] = {}

# -------------------------------------------------------------------
# Hook PENDING into the callbacks we already registered
# We patch the manager's handler to set PENDING before replies.
# -------------------------------------------------------------------
_orig_handle_callback = StressManager.handle_callback

async def _patched_handle_callback(self, q: CallbackQuery):
    data = q.data
    if data == "cfg_target":
        PENDING[q.from_user.id] = None  # target parsed by content
    elif data == "cfg_pps":
        PENDING[q.from_user.id] = "pps"
    elif data == "cfg_senders":
        PENDING[q.from_user.id] = "senders"
    elif data == "cfg_duration":
        PENDING[q.from_user.id] = "duration"
    elif data == "user_add":
        PENDING[q.from_user.id] = "user_add"
    return await _orig_handle_callback(self, q)

StressManager.handle_callback = _patched_handle_callback

# -------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------
async def main():
    manager = StressManager()

    # Also listen for plain text messages to route into config
    @manager.bot.on_message(filters.private & ~filters.command(["start", "admin", "help"]))
    async def route_config(_, msg: Message):
        await manager.handle_config_message(msg)

    await manager.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bye.")