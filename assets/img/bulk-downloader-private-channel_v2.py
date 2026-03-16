# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev
#
# Single-file webhook bot — zero local module imports.
# HuggingFace Spaces Docker:  uvicorn main:app --host 0.0.0.0 --port 7860
#
# DEPLOYMENT NOTE:
#   HF Spaces blocks api.telegram.org (Bot API HTTP).
#   This bot uses Pyrogram over MTProto (raw TCP) — NOT blocked.
#
# ARCHITECTURE:
#   /bdl — Parallel pipeline with ordered delivery:
#     Phase 1: bulk-fetch message metadata (chunked get_messages)
#     Phase 2: N parallel download workers (asyncio.Semaphore pool)
#     Phase 3: ordered sender (ready_queue + send_pointer, send-as-ready)
#     Phase 4: /retry command re-attempts failed/abandoned slots,
#              editing their placeholder messages in-place
#
#   Features:
#     - Forward EVERYTHING: media, text, polls, stickers, forwards
#     - Preserve reply chains: source reply→msg mapping to our group
#     - Partial file fallback: send whatever bytes we got with warning caption
#     - Abandoned placeholder: text message keeping sequence on total failure
#     - /retry: re-run failed slots and edit placeholders with results
#     - /killall: hard-cancels ALL worker tasks for that batch immediately
#     - Live status board: edits loading_msg every STATE_LOG_INTERVAL seconds
#     - Status message always at bottom: deleted+resent after each file send
#     - Upload progress bar: ephemeral message showing upload %, then deleted
#     - No reply-to spam: downloaded files are standalone, only status replies

import os
import sys
import json
import math
import shutil
import psutil
import asyncio
import logging
import datetime
import traceback
from time import time
from typing import Optional, Dict, List, Any, Tuple
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from enum import Enum

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import PeerIdInvalid, BadRequest, FloodWait
from pyrogram.errors.exceptions.flood_420 import FloodPremiumWait
from pyrogram.errors.exceptions.bad_request_400 import FileReferenceExpired
from pyrogram.parser import Parser
from pyrogram.utils import get_channel_id
from pyrogram.types import (
    InputMediaPhoto, InputMediaVideo,      # used by edit_placeholder_with_media
    InputMediaAudio, InputMediaDocument,   # used by edit_placeholder_with_media
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from pyleaves import Leaves

# ════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════
try:
    os.remove("logs.txt")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s - %(levelname)s] - %(funcName)s() - Line %(lineno)d: %(name)s - %(message)s",
    datefmt="%d-%b-%y %I:%M:%S %p",
    handlers=[
        RotatingFileHandler("logs.txt", mode="w+", maxBytes=5_000_000, backupCount=10),
        logging.StreamHandler(),
    ],
)
logging.getLogger("pyrogram").setLevel(logging.ERROR)

def LOGGER(name: str) -> logging.Logger:
    return logging.getLogger(name)

# ════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════
try:
    load_dotenv("config.env.local")
    load_dotenv("config.env")
except Exception:
    pass

_missing = [v for v in ("BOT_TOKEN", "SESSION_STRING", "API_ID", "API_HASH")
            if not os.getenv(v)]
if _missing:
    print(f"ERROR: Missing required env vars: {', '.join(_missing)}")
    sys.exit(1)

class PyroConf:
    API_ID   = int(os.getenv("API_ID", "6"))
    API_HASH = os.getenv("API_HASH", "eb06d4abfb49dc3eeb1aeb98ae0f581e")
    BOT_TOKEN      = os.getenv("BOT_TOKEN")
    SESSION_STRING = os.getenv("SESSION_STRING")
    BOT_START_TIME = time()

    # Workers
    MAX_FETCH_WORKERS        = int(os.getenv("MAX_FETCH_WORKERS", "3"))
    MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
    # Parallel Telegram uploads (mirrors rclone --transfers=4 from upload_all.sh)
    # Downloads fill a pool; uploads drain it M at a time, ordered delivery preserved.
    MAX_UPLOAD_WORKERS       = int(os.getenv("MAX_UPLOAD_WORKERS", "2"))

    # Retry
    MAX_DOWNLOAD_RETRIES     = int(os.getenv("MAX_DOWNLOAD_RETRIES", "3"))
    RETRY_DELAY_SECONDS      = int(os.getenv("RETRY_DELAY_SECONDS", "5"))

    # Timeouts / limits
    CONSECUTIVE_ERROR_LIMIT  = int(os.getenv("CONSECUTIVE_ERROR_LIMIT", "5"))
    FLOOD_WAIT_DELAY         = int(os.getenv("FLOOD_WAIT_DELAY", "3"))
    FETCH_BATCH_SIZE         = int(os.getenv("FETCH_BATCH_SIZE", "20"))

    # Live UI update interval (seconds)
    STATE_LOG_INTERVAL       = float(os.getenv("STATE_LOG_INTERVAL", "5.0"))

    # Upload progress edit interval (seconds)
    UPLOAD_PROGRESS_INTERVAL = float(os.getenv("UPLOAD_PROGRESS_INTERVAL", "3.0"))

    # Upload
    MAX_UPLOAD_RETRIES       = int(os.getenv("MAX_UPLOAD_RETRIES", "3"))

    # Progress throttle for single /dl downloads (seconds)
    DOWNLOAD_PROGRESS_INTERVAL = float(os.getenv("DOWNLOAD_PROGRESS_INTERVAL", "2.5"))

    # Sonic mode: max simultaneous uploads (too high = FLOOD_WAIT)
    MAX_SONIC_UPLOADS        = int(os.getenv("MAX_SONIC_UPLOADS", "2"))

# ════════════════════════════════════════════════════════
# TASK STATUS
# ════════════════════════════════════════════════════════
class TaskStatus(str, Enum):
    PENDING      = "pending"
    FETCHING     = "fetching"
    DOWNLOADING  = "downloading"
    SENDING      = "sending"     # queued for upload (downloaded, waiting for upload slot)
    UPLOADING    = "uploading"   # actively uploading to Telegram
    COMPLETED    = "completed"
    FAILED       = "failed"
    PARTIAL      = "partial"    # sent with warning — partial file
    ABANDONED    = "abandoned"  # placeholder message sent, retryable
    SKIPPED      = "skipped"

_STATUS_ICON = {
    TaskStatus.PENDING:     "⏳",
    TaskStatus.FETCHING:    "🔍",
    TaskStatus.DOWNLOADING: "⬇️",
    TaskStatus.SENDING:     "📦",   # queued, waiting for upload slot
    TaskStatus.UPLOADING:   "📤",   # actively uploading
    TaskStatus.COMPLETED:   "✅",
    TaskStatus.FAILED:      "❌",
    TaskStatus.PARTIAL:     "⚠️",
    TaskStatus.ABANDONED:   "🔲",
    TaskStatus.SKIPPED:     "⏭️",
}

# Callback data for the inline Refresh button on the status message
REFRESH_CB    = "batch_refresh"
# Callback data for /settings toggles
SETTING_CB_PREFIX = "setting:"   # setting:<key>:<value>

# ════════════════════════════════════════════════════════
# USER SETTINGS
# Per-chat preferences stored in memory.
# Persists for the lifetime of the process (resets on redeploy).
# ════════════════════════════════════════════════════════
_SETTING_DEFAULTS: Dict[str, Any] = {
    "status_always_bottom": True,   # delete+resend status after each file
    "sonic_download":       False,  # upload out-of-order, fill gaps with placeholders
    "sonic_strict":         True,   # sonic mode: strict sequence via single chat_writer (default)
                                    # False = concurrent/loose (faster but may mis-order)
}

_user_settings: Dict[int, Dict[str, Any]] = {}  # chat_id → {key: value}
_USER_SETTINGS_MAX = 500   # evict oldest entries beyond this limit

def get_setting(chat_id: int, key: str) -> Any:
    return _user_settings.get(chat_id, {}).get(key, _SETTING_DEFAULTS.get(key))

def set_setting(chat_id: int, key: str, value: Any) -> None:
    if chat_id not in _user_settings:
        # Evict the oldest entry when at capacity
        if len(_user_settings) >= _USER_SETTINGS_MAX:
            oldest = next(iter(_user_settings))
            del _user_settings[oldest]
        _user_settings[chat_id] = dict(_SETTING_DEFAULTS)
    _user_settings[chat_id][key] = value

# ── Settings menu helpers ────────────────────────────────
_SETTING_LABELS = {
    "status_always_bottom": "📌 Status bar always at bottom",
    "sonic_download":       "⚡ Sonic Download (upload instantly, fill gaps with placeholders)",
    "sonic_strict":         "🔢 Sonic: Strict sequence (single writer, 100% order guarantee)",
}

def _settings_keyboard_dict(chat_id: int) -> dict:
    """Build the settings inline keyboard as a raw dict for webhook JSON responses."""
    rows = []
    for key, label in _SETTING_LABELS.items():
        val  = get_setting(chat_id, key)
        icon = "✅ On" if val else "❌ Off"
        cb   = f"{SETTING_CB_PREFIX}{key}:{int(not val)}"
        rows.append([{"text": f"{icon}  {label}", "callback_data": cb}])
    rows.append([{"text": "✖ Close", "callback_data": "settings_close"}])
    return {"inline_keyboard": rows}

def _settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Pyrogram InlineKeyboardMarkup adapter — wraps _settings_keyboard_dict."""
    raw = _settings_keyboard_dict(chat_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(btn["text"], callback_data=btn["callback_data"])
         for btn in row]
        for row in raw["inline_keyboard"]
    ])

def _settings_text(chat_id: int) -> str:
    header = "\u2699\ufe0f **Bot Settings**\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\nTap a setting to toggle it.\n"
    lines = [header]
    for key, label in _SETTING_LABELS.items():
        val  = get_setting(chat_id, key)
        icon = "\u2705 On" if val else "\u274c Off"
        lines.append(f"{label}: **{icon}**")
    return "\n".join(lines)

# ════════════════════════════════════════════════════════
# UX HELPERS
# ════════════════════════════════════════════════════════
def fmt_size(b: float) -> str:
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def fmt_time(seconds: float, compact: bool = False) -> str:
    """Format a duration.
    compact=False (default): '1h 30m 5s'  — used in progress bars.
    compact=True:            '1h30m5s'     — used in /stats uptime.
    Returns '—' for non-positive or NaN input.
    """
    if seconds <= 0 or math.isnan(seconds):
        return "—"
    seconds = int(seconds)
    sep = "" if compact else " "
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{sep}{s}s"
    h, m = divmod(m, 60)
    return f"{h}h{sep}{m}m"

def get_readable_time(seconds: int) -> str:
    """Compact uptime string, e.g. '2d3h5m10s'. Delegates to fmt_time(compact=True)."""
    result = ""
    days, rem = divmod(int(seconds), 86400)
    if days:
        result += f"{days}d"
        remaining_secs = rem
        result += fmt_time(remaining_secs, compact=True).lstrip("—") if remaining_secs > 0 else "0s"
        return result
    return fmt_time(seconds, compact=True)

def progress_bar(current: int, total: int, length: int = 10) -> str:
    if not total or total <= 0 or current <= 0:
        return "░" * length
    pct    = min(1.0, current / total)
    filled = int(length * pct)
    return "▓" * filled + "░" * (length - filled)

def short_name(filename: str, maxlen: int = 35) -> str:
    return filename if len(filename) <= maxlen else filename[:maxlen - 3] + "..."

def build_progress_text(phase: str, current: int, total: int,
                        speed: float, elapsed: float,
                        filename: str, attempt: int = 1,
                        max_retries: int = 1) -> str:
    pct       = min(100, int(current * 100 / total)) if total > 0 else 0
    bar       = progress_bar(current, total)
    total_str = fmt_size(total) if total > 0 else "?"
    eta       = fmt_time((total - current) / speed) if (
        total > 0 and speed > 0 and current < total) else "—"
    icon      = "📥" if phase == "download" else "📤"
    label     = "Downloading" if phase == "download" else "Uploading"
    retry_str = f"  _(retry {attempt}/{max_retries})_" if attempt > 1 else ""
    return (
        f"{icon} **{label}:**{retry_str} `{filename}`\n\n"
        f"`{bar}` **{pct}%**\n\n"
        f"**Done:** `{fmt_size(current)}` / `{total_str}`\n"
        f"**Speed:** `{fmt_size(speed)}/s`\n"
        f"**ETA:** `{eta}`\n"
        f"**Elapsed:** `{fmt_time(elapsed)}`"
    )

# ════════════════════════════════════════════════════════
# BATCH STATE — per slot
# ════════════════════════════════════════════════════════
@dataclass
class TaskState:
    index:            int
    url:              str
    status:           TaskStatus  = TaskStatus.PENDING
    filename:         str         = ""
    file_size:        int         = 0
    bytes_done:       int         = 0
    error:            str         = ""
    retry_count:      int         = 0
    # message_id of placeholder/partial sent in group — used by /retry
    placeholder_msg_id: Optional[int] = None
    # message_id of the successfully sent final message in group
    sent_msg_id:      Optional[int]   = None

# ════════════════════════════════════════════════════════
# BATCH STATE MANAGER — live Telegram UI
# ════════════════════════════════════════════════════════
class BatchStateManager:
    def __init__(self, total: int, chat_id: int, reply_to_id: int,
                 status_message=None, batch_start: float = None,
                 refresh_event: asyncio.Event = None):
        self.total          = total
        self.chat_id        = chat_id
        self.reply_to_id    = reply_to_id
        self.status_message = status_message
        self.batch_start    = batch_start or time()
        self.refresh_event  = refresh_event   # set by /refresh to force instant redraw
        self.state_map:     Dict[int, TaskState] = {}
        self.update_queue:  asyncio.Queue        = asyncio.Queue()
        self._stop_event:   asyncio.Event        = asyncio.Event()
        self._task:         Optional[asyncio.Task] = None
        self._last_ui:      float                = time()  # wait full interval before first edit
        # Serialises all status-message operations (edit, delete, resend) so
        # _update_live_ui and move_status_to_bottom never interleave.
        self._status_lock:  asyncio.Lock         = asyncio.Lock()

    def register_slots(self, indices: List[int], urls: List[str]):
        for i, url in zip(indices, urls):
            self.state_map[i] = TaskState(index=i, url=url)

    async def put(self, index: int, **kwargs):
        await self.update_queue.put({"index": index, **kwargs})

    def start(self):
        self._task = asyncio.create_task(self._run(), name="BatchStateManager")
        return self._task

    async def stop(self):
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=30.0)
            except asyncio.TimeoutError:
                LOGGER(__name__).error("BatchStateManager stop timed out")
                self._task.cancel()

    def _refresh_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data=REFRESH_CB)
        ]])

    async def move_status_to_bottom(self):
        """
        After each file send, either delete+resend the status message so
        it stays at the bottom (status_always_bottom=True), or just
        edit it in-place (status_always_bottom=False).
        Controlled per-user via /settings.
        Serialised with _status_lock to prevent concurrent edits from
        _update_live_ui stepping on a delete+resend in progress.
        """
        if not self.status_message:
            return
        async with self._status_lock:
            text = self._build_ui_text()
            if get_setting(self.chat_id, "status_always_bottom"):
                # Delete + resend → always the newest/bottommost message
                try:
                    await self.status_message.delete()
                except Exception:
                    pass
                try:
                    self.status_message = await bot.send_message(
                        self.chat_id, text,
                        reply_markup=self._refresh_markup())
                    self._last_ui = time()
                except Exception as e:
                    LOGGER(__name__).debug(f"move_status_to_bottom (resend) failed: {e}")
            else:
                # Edit in-place → stays where it was, no new message
                try:
                    await bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.status_message.id,
                        text=text,
                        reply_markup=self._refresh_markup())
                    self._last_ui = time()
                except Exception as e:
                    LOGGER(__name__).debug(f"move_status_to_bottom (edit) failed: {e}")

    async def _run(self):
        LOGGER(__name__).info("BatchStateManager started.")
        while not self._stop_event.is_set():
            # ── 1. Check time FIRST — update UI if interval elapsed ─────
            # /refresh: force immediate redraw
            if self.refresh_event and self.refresh_event.is_set():
                self.refresh_event.clear()
                self._last_ui = 0.0
            if time() - self._last_ui >= PyroConf.STATE_LOG_INTERVAL:
                await self._update_live_ui()

            # ── 2. Wait for next state update (up to STATE_LOG_INTERVAL) ─
            # Wakes immediately when a worker pushes an update.
            # Times out after STATE_LOG_INTERVAL so the UI check above
            # fires reliably even when the queue is busy.
            try:
                update = await asyncio.wait_for(
                    self.update_queue.get(),
                    timeout=PyroConf.STATE_LOG_INTERVAL)
                self._apply(update)
                self.update_queue.task_done()
                # Drain any burst that arrived while we were awaiting
                while True:
                    try:
                        u2 = self.update_queue.get_nowait()
                        self._apply(u2)
                        self.update_queue.task_done()
                    except asyncio.QueueEmpty:
                        break
            except asyncio.TimeoutError:
                pass   # interval elapsed — loop back, UI check fires next

        await self._flush()
        await self._update_live_ui()
        LOGGER(__name__).info("BatchStateManager finished.")

    # Statuses that represent a final outcome — once set, they must not be
    # overwritten by a stale queued update (e.g. DOWNLOADING arriving after
    # the worker already set ABANDONED directly on the state_map object).
    _TERMINAL_STATUSES = frozenset({
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.PARTIAL,
        TaskStatus.ABANDONED,
        TaskStatus.SKIPPED,
    })

    def _apply(self, update: dict):
        idx = update.pop("index", None)
        if idx is not None and idx in self.state_map:
            s = self.state_map[idx]
            new_status = update.get("status")
            # Never let a non-terminal queued update overwrite a terminal status
            # that was already written directly to the state_map object.
            if (new_status is not None
                    and s.status in self._TERMINAL_STATUSES
                    and new_status not in self._TERMINAL_STATUSES):
                LOGGER(__name__).debug(
                    f"_apply: ignoring stale status {new_status!r} for slot {idx} "
                    f"(already {s.status!r})")
                update.pop("status", None)
            for k, v in update.items():
                if hasattr(s, k):
                    setattr(s, k, v)

    async def _flush(self):
        while not self.update_queue.empty():
            try:
                update = self.update_queue.get_nowait()
                self._apply(update)
                self.update_queue.task_done()
            except asyncio.QueueEmpty:
                break

    def _counts(self) -> Dict[TaskStatus, int]:
        counts = {s: 0 for s in TaskStatus}
        for ts in self.state_map.values():
            counts[ts.status] += 1
        return counts

    def _build_ui_text(self) -> str:
        counts  = self._counts()
        elapsed = time() - self.batch_start
        done    = sum(counts[s] for s in (
            TaskStatus.COMPLETED, TaskStatus.FAILED,
            TaskStatus.PARTIAL, TaskStatus.ABANDONED, TaskStatus.SKIPPED))
        bar     = progress_bar(done, self.total, length=10)
        pct     = int(done * 100 / self.total) if self.total else 0

        header = (
            f"⚡ **Batch Download** — `{self.total}` posts\n"
            f"`{bar}` {pct}%  •  "
            f"✅`{counts[TaskStatus.COMPLETED]}` "
            f"⚠️`{counts[TaskStatus.PARTIAL]}` "
            f"🔲`{counts[TaskStatus.ABANDONED]}` "
            f"❌`{counts[TaskStatus.FAILED]}` "
            f"⏭️`{counts[TaskStatus.SKIPPED]}` "
            f"•  `{fmt_time(elapsed)}`\n"
            f"⬇️`{counts[TaskStatus.DOWNLOADING]}` dl  "
            f"📤`{counts[TaskStatus.UPLOADING]}` up  "
            f"📦`{counts[TaskStatus.SENDING]}` queued"
        )

        active_lines = []
        for ts in sorted(self.state_map.values(), key=lambda x: x.index):
            if ts.status not in (TaskStatus.DOWNLOADING, TaskStatus.UPLOADING,
                                 TaskStatus.SENDING, TaskStatus.FETCHING):
                continue
            icon  = _STATUS_ICON[ts.status]
            label = short_name(ts.filename or f"Post #{ts.index + 1}", 28)
            if ts.status == TaskStatus.DOWNLOADING:
                if ts.file_size > 0 and ts.bytes_done > 0:
                    pbar = progress_bar(ts.bytes_done, ts.file_size, length=6)
                    active_lines.append(
                        f"{icon} `{pbar}` `{label}`\n"
                        f"    `{fmt_size(ts.bytes_done)}` / `{fmt_size(ts.file_size)}`")
                elif ts.file_size > 0:
                    active_lines.append(
                        f"{icon} `{label}` — `{fmt_size(ts.file_size)}`")
                else:
                    active_lines.append(f"{icon} `{label}`")
            elif ts.status == TaskStatus.UPLOADING:
                size_str = f"`{fmt_size(ts.file_size)}`" if ts.file_size else ""
                active_lines.append(f"📤 `{label}` {size_str} — _uploading…_")
            elif ts.status == TaskStatus.SENDING:
                active_lines.append(f"📦 `{label}` — _queued for upload_")
            else:
                active_lines.append(f"{icon} `{label}`")

        recent_lines = []
        recent = [
            ts for ts in sorted(self.state_map.values(),
                                 key=lambda x: x.index, reverse=True)
            if ts.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                             TaskStatus.PARTIAL, TaskStatus.ABANDONED)
        ][:4]
        for ts in reversed(recent):
            icon  = _STATUS_ICON[ts.status]
            label = short_name(ts.filename or f"Post #{ts.index + 1}", 28)
            if ts.status == TaskStatus.COMPLETED:
                sz = f"`{fmt_size(ts.file_size)}`" if ts.file_size else ""
                recent_lines.append(f"{icon} `{label}` {sz}")
            elif ts.status == TaskStatus.PARTIAL:
                recent_lines.append(f"{icon} `{label}` — _partial, sent with warning_")
            elif ts.status == TaskStatus.ABANDONED:
                recent_lines.append(f"{icon} `{label}` — _placeholder sent, use /retry_")
            else:
                err = short_name(ts.error or "error", 35)
                recent_lines.append(f"{icon} `{label}` — _{err}_")

        parts = [header]
        if active_lines:
            shown   = active_lines[:10]
            hidden  = len(active_lines) - len(shown)
            block   = "\n".join(shown)
            if hidden > 0:
                block += f"\n_…and {hidden} more_"
            parts.append("\n**Active:**\n" + block)
        if recent_lines:
            parts.append("\n**Recent:**\n" + "\n".join(recent_lines))
        pending = counts[TaskStatus.PENDING]
        if pending > 0:
            parts.append(f"\n_⏳ {pending} post(s) queued…_")
        parts.append("\n_/skip — skip current  •  /killall — stop all  •  /refresh — refresh now_")
        return "\n".join(parts)

    async def _update_live_ui(self):
        self._last_ui = time()
        counts = self._counts()
        LOGGER(__name__).info(
            f"[Batch] {self.total} total | "
            f"✅{counts[TaskStatus.COMPLETED]} "
            f"⬇️{counts[TaskStatus.DOWNLOADING]} "
            f"📤{counts[TaskStatus.SENDING]} "
            f"⚠️{counts[TaskStatus.PARTIAL]} "
            f"🔲{counts[TaskStatus.ABANDONED]} "
            f"❌{counts[TaskStatus.FAILED]} "
            f"⏭️{counts[TaskStatus.SKIPPED]}")
        if not self.status_message:
            return
        # Use try_acquire so the timer tick doesn't block waiting behind a
        # move_status_to_bottom that is mid delete+resend. If the lock is held,
        # skip this edit — the next timer tick will catch up.
        if self._status_lock.locked():
            LOGGER(__name__).debug("_update_live_ui: lock held, skipping edit")
            return
        async with self._status_lock:
            if not self.status_message:
                return
            try:
                await bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.status_message.id,
                    text=self._build_ui_text(),
                    reply_markup=self._refresh_markup())
            except Exception as e:
                LOGGER(__name__).debug(f"Live UI edit skipped: {e}")

    def summary(self) -> dict:
        counts = {s: 0 for s in TaskStatus}
        for ts in self.state_map.values():
            counts[ts.status] += 1
        return counts

    def get_retryable(self) -> List[TaskState]:
        """Return slots that can be retried (failed or abandoned)."""
        return [
            ts for ts in self.state_map.values()
            if ts.status in (TaskStatus.FAILED, TaskStatus.ABANDONED,
                             TaskStatus.PARTIAL)
        ]

# ════════════════════════════════════════════════════════
# PER-BATCH REGISTRY — stores state managers by (chat_id, batch_key)
# Used by /retry to find the last batch's state
# ════════════════════════════════════════════════════════
_batch_registry: Dict[int, "BatchStateManager"] = {}  # chat_id → last BatchStateManager
_batch_worker_tasks: Dict[int, List[asyncio.Task]]   = {}  # chat_id → worker tasks

# ════════════════════════════════════════════════════════
# FILE HELPERS
# ════════════════════════════════════════════════════════
def get_download_path(folder_id: int, filename: str,
                      root_dir: str = "downloads") -> str:
    folder = os.path.join(root_dir, str(folder_id))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, filename)

def cleanup_download(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
        if path and os.path.exists(path + ".temp"):
            os.remove(path + ".temp")
        if path:
            folder = os.path.dirname(path)
            if os.path.isdir(folder) and not os.listdir(folder):
                os.rmdir(folder)
    except Exception as e:
        LOGGER(__name__).error(f"Cleanup failed for {path}: {e}")

def cleanup_downloads_root(root_dir: str = "downloads") -> tuple:
    if not os.path.isdir(root_dir):
        return 0, 0
    file_count = total_size = 0
    for dirpath, _, filenames in os.walk(root_dir):
        for name in filenames:
            file_count += 1
            try:
                total_size += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    shutil.rmtree(root_dir, ignore_errors=True)
    return file_count, total_size

async def fileSizeLimit(file_size, message, action_type="download",
                        is_premium=False):
    MAX_FILE_SIZE = 2 * 2_097_152_000 if is_premium else 2_097_152_000
    if file_size > MAX_FILE_SIZE:
        await message.reply(
            f"The file size exceeds the {fmt_size(MAX_FILE_SIZE)} "
            f"limit and cannot be {action_type}ed.")
        return False
    return True

# ════════════════════════════════════════════════════════
# MEDIA CHECKS
# ════════════════════════════════════════════════════════
def has_downloadable_media(msg) -> bool:
    return bool(
        msg.document or msg.video  or msg.audio     or msg.voice or
        msg.video_note or msg.animation or msg.sticker or msg.photo
    )

def has_any_content(msg) -> bool:
    """True for anything worth forwarding: media, text, polls, etc."""
    return bool(
        msg.text or msg.caption or msg.media or
        msg.poll or msg.location or msg.venue or
        msg.contact or msg.dice or msg.game
    )

# ════════════════════════════════════════════════════════
# LINK HELPERS
# ════════════════════════════════════════════════════════
async def get_parsed_msg(text, entities):
    return Parser.unparse(text, entities or [], is_html=False)

def getChatMsgID(link: str):
    linkps = link.split("/")
    chat_id = message_id = None
    try:
        if len(linkps) == 7 and linkps[3] == "c":
            chat_id    = get_channel_id(int(linkps[4]))
            message_id = int(linkps[6])
        elif len(linkps) == 6:
            if linkps[3] == "c":
                chat_id    = get_channel_id(int(linkps[4]))
                message_id = int(linkps[5])
            else:
                chat_id    = linkps[3]
                message_id = int(linkps[5])
        elif len(linkps) == 5:
            chat_id = linkps[3]
            if chat_id == "m":
                raise ValueError("Invalid ClientType")
            message_id = int(linkps[4])
    except (ValueError, TypeError):
        raise ValueError("Invalid post URL. Must end with a numeric ID.")
    if not chat_id or not message_id:
        raise ValueError("Please send a valid Telegram post URL.")
    return chat_id, message_id

def get_file_name(message_id: int, chat_message) -> str:
    if chat_message.document:
        return chat_message.document.file_name or f"{message_id}"
    elif chat_message.video:
        return chat_message.video.file_name or f"{message_id}.mp4"
    elif chat_message.audio:
        return chat_message.audio.file_name or f"{message_id}.mp3"
    elif chat_message.voice:
        return f"{message_id}.ogg"
    elif chat_message.video_note:
        return f"{message_id}.mp4"
    elif chat_message.animation:
        return chat_message.animation.file_name or f"{message_id}.gif"
    elif chat_message.sticker:
        if chat_message.sticker.is_animated: return f"{message_id}.tgs"
        elif chat_message.sticker.is_video:  return f"{message_id}.webm"
        else:                                return f"{message_id}.webp"
    elif chat_message.photo:
        return f"{message_id}.jpg"
    return f"{message_id}.bin"

def get_expected_file_size(chat_message) -> Optional[int]:
    if chat_message.document:   return chat_message.document.file_size
    elif chat_message.video:    return chat_message.video.file_size
    elif chat_message.audio:    return chat_message.audio.file_size
    elif chat_message.voice:    return chat_message.voice.file_size
    elif chat_message.video_note: return chat_message.video_note.file_size
    elif chat_message.animation: return chat_message.animation.file_size
    elif chat_message.sticker:  return chat_message.sticker.file_size
    return None

# ════════════════════════════════════════════════════════
# ROBUST DOWNLOAD — retry, size validation, partial capture
# Returns (path, error, is_partial)
#   path       — file path if any bytes saved, else None
#   error      — error description if failed
#   is_partial — True if file exists but is truncated
# ════════════════════════════════════════════════════════
async def download_single_message(
    chat_message,
    download_path: str,
    progress_message=None,
    start_time: float = None,
    retries: int = None,
    _src_chat_id=None,   # needed to re-fetch fresh file_reference on expiry
) -> Tuple[Optional[str], Optional[str], bool]:

    max_retries   = retries if retries is not None else PyroConf.MAX_DOWNLOAD_RETRIES
    expected_size = get_expected_file_size(chat_message)
    last_partial_path: Optional[str] = None   # best partial we've seen so far

    for attempt in range(1, max_retries + 1):
        media_path = None
        attempt_path = (download_path if attempt == 1
                        else f"{download_path}.retry{attempt}")
        try:
            kwargs: dict = {"file_name": attempt_path}
            if progress_message and start_time:
                if callable(progress_message):
                    kwargs["progress"] = progress_message
                else:
                    kwargs["progress"]      = Leaves.progress_for_pyrogram
                    kwargs["progress_args"] = ("📥 Downloading", progress_message, start_time)
            media_path = await chat_message.download(**kwargs)

        except FileReferenceExpired:
            # FILE_REFERENCE_EXPIRED (400): the file_reference inside the cached
            # message object has expired (typically after ~1h). The ONLY fix is to
            # re-fetch the message from Telegram to get a fresh reference, then
            # retry the download with the new message object.
            LOGGER(__name__).warning(
                f"FILE_REFERENCE_EXPIRED attempt {attempt}/{max_retries} — "
                f"re-fetching message for fresh file_reference…")
            for p in (attempt_path, attempt_path + ".temp", media_path):
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            if attempt < max_retries:
                try:
                    src_chat = _src_chat_id or getattr(
                        getattr(chat_message, "chat", None), "id", None)
                    if src_chat and chat_message.id:
                        fresh = await user.get_messages(
                            chat_id=src_chat, message_ids=chat_message.id)
                        if fresh and fresh.id:
                            chat_message = fresh   # swap in fresh reference
                            expected_size = get_expected_file_size(chat_message) or expected_size
                            LOGGER(__name__).info(
                                f"Got fresh file_reference for msg {chat_message.id}")
                except Exception as refetch_err:
                    LOGGER(__name__).error(f"Re-fetch failed: {refetch_err}")
                await asyncio.sleep(PyroConf.RETRY_DELAY_SECONDS)
                continue
            break

        except FloodPremiumWait as e:
            # FLOOD_PREMIUM_WAIT (420): non-premium account rate-limited on
            # large file downloads. Sleep the exact wait + buffer, always retry.
            wait_s = int(getattr(e, "value", 0) or 15)
            LOGGER(__name__).warning(
                f"FloodPremiumWait {wait_s}s (attempt {attempt}/{max_retries})")
            for p in (attempt_path, attempt_path + ".temp", media_path):
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            if attempt < max_retries:
                await asyncio.sleep(wait_s + 2)
                continue
            break

        except FloodWait as e:
            # FLOOD_WAIT (420): too many download requests from this DC session.
            # sleep_threshold in Client handles small waits automatically;
            # we only see this if wait > sleep_threshold (30s). Always sleep + retry.
            wait_s = int(getattr(e, "value", 0) or 30)
            LOGGER(__name__).warning(
                f"FloodWait {wait_s}s on download (attempt {attempt}/{max_retries})")
            for p in (attempt_path, attempt_path + ".temp", media_path):
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            if attempt < max_retries:
                await asyncio.sleep(wait_s + 2)   # +2s buffer over exact wait
                continue
            break

        except OSError as e:
            # TCP/socket error — Pyrogram's DC session dropped.
            # Sleep with backoff to let it reconnect.
            LOGGER(__name__).warning(
                f"TCP/OSError attempt {attempt}/{max_retries}: {e}")
            for p in (attempt_path, attempt_path + ".temp", media_path):
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            if attempt < max_retries:
                await asyncio.sleep(PyroConf.RETRY_DELAY_SECONDS * attempt + 3)
                continue
            break

        except (TimeoutError, asyncio.TimeoutError):
            LOGGER(__name__).warning(
                f"Timeout attempt {attempt}/{max_retries}")
            for p in (attempt_path, attempt_path + ".temp", media_path):
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            if attempt < max_retries:
                await asyncio.sleep(PyroConf.RETRY_DELAY_SECONDS * attempt)
                continue
            break

        except Exception as e:
            err_str = str(e)
            # AUTH_BYTES_INVALID: DC migration session has stale auth bytes.
            # Needs a longer sleep so Pyrogram can re-establish DC authorization.
            if "AUTH_BYTES_INVALID" in err_str:
                LOGGER(__name__).warning(
                    f"AUTH_BYTES_INVALID attempt {attempt}/{max_retries} — "
                    f"waiting for DC re-auth…")
                for p in (attempt_path, attempt_path + ".temp", media_path):
                    if p and os.path.exists(p):
                        try: os.remove(p)
                        except Exception: pass
                if attempt < max_retries:
                    await asyncio.sleep(15 * attempt)
                    continue
                break
            # FILE_REFERENCE_EXPIRED can also appear as a generic BadRequest
            # with the string in the message before Pyrogram parses it.
            if "FILE_REFERENCE_EXPIRED" in err_str:
                LOGGER(__name__).warning(
                    f"FILE_REFERENCE_EXPIRED (generic) attempt {attempt}/{max_retries}")
                for p in (attempt_path, attempt_path + ".temp", media_path):
                    if p and os.path.exists(p):
                        try: os.remove(p)
                        except Exception: pass
                if attempt < max_retries:
                    try:
                        src_chat = _src_chat_id or getattr(
                            getattr(chat_message, "chat", None), "id", None)
                        if src_chat and chat_message.id:
                            fresh = await user.get_messages(
                                chat_id=src_chat, message_ids=chat_message.id)
                            if fresh and fresh.id:
                                chat_message = fresh
                                expected_size = get_expected_file_size(chat_message) or expected_size
                    except Exception as refetch_err:
                        LOGGER(__name__).error(f"Re-fetch failed: {refetch_err}")
                    await asyncio.sleep(PyroConf.RETRY_DELAY_SECONDS)
                    continue
                break
            LOGGER(__name__).error(
                f"Download error attempt {attempt}/{max_retries}: {e}")
            for p in (attempt_path, attempt_path + ".temp", media_path):
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            if attempt < max_retries:
                await asyncio.sleep(PyroConf.RETRY_DELAY_SECONDS * attempt)
                continue
            break

        # ── Validate ──────────────────────────────────────
        if not media_path or not os.path.exists(media_path):
            if attempt < max_retries:
                await asyncio.sleep(PyroConf.RETRY_DELAY_SECONDS)
                continue
            break

        actual_size = os.path.getsize(media_path)

        if actual_size == 0:
            LOGGER(__name__).warning(f"0-byte file attempt {attempt}/{max_retries}: {media_path}")
            for p in (media_path, attempt_path + ".temp"):
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            if attempt < max_retries:
                await asyncio.sleep(PyroConf.RETRY_DELAY_SECONDS * attempt)
                continue
            break

        if expected_size and actual_size < expected_size * 0.95:
            LOGGER(__name__).warning(
                f"Partial download attempt {attempt}/{max_retries}: "
                f"got {fmt_size(actual_size)} / expected {fmt_size(expected_size)}")
            # Keep this partial as our best candidate
            if last_partial_path and last_partial_path != media_path:
                try: os.remove(last_partial_path)
                except Exception: pass
            last_partial_path = media_path
            # Clean up .temp but NOT the file itself — we may send it as partial
            if os.path.exists(attempt_path + ".temp"):
                try: os.remove(attempt_path + ".temp")
                except Exception: pass
            if attempt < max_retries:
                await asyncio.sleep(PyroConf.RETRY_DELAY_SECONDS * attempt * 2)
                continue
            # All retries exhausted — return partial as last resort
            LOGGER(__name__).warning(
                f"Returning partial file after all retries: {last_partial_path}")
            return last_partial_path, (
                f"Partial: {fmt_size(actual_size)} / {fmt_size(expected_size)}"), True

        # ── Success ───────────────────────────────────────
        if attempt_path != download_path:
            try:
                if os.path.exists(download_path):
                    os.remove(download_path)
                os.rename(media_path, download_path)
                media_path = download_path
            except Exception as e:
                LOGGER(__name__).warning(f"Rename failed, using as-is: {e}")
        # Clean up any leftover partial from a previous attempt
        if last_partial_path and last_partial_path != media_path:
            try: os.remove(last_partial_path)
            except Exception: pass
        LOGGER(__name__).info(
            f"✅ Download OK: {fmt_size(actual_size)} "
            f"→ {os.path.basename(media_path)} (attempt {attempt})")
        # src_chat and msg ID are embedded in the attempt_path directory
        return media_path, None, False

    # ── Total failure — return best partial if we have one ──
    if last_partial_path and os.path.exists(last_partial_path):
        actual = os.path.getsize(last_partial_path)
        LOGGER(__name__).warning(
            f"All retries exhausted — using partial: {fmt_size(actual)}")
        return last_partial_path, f"Partial: {fmt_size(actual)} / {fmt_size(expected_size or 0)}", True

    return None, "Download failed after all retries", False

# ════════════════════════════════════════════════════════
# SEND MEDIA WITH UPLOAD PROGRESS BAR
# Shows an ephemeral "📤 Uploading…" message with a live
# progress bar while the file uploads. When upload completes
# the progress message is deleted, leaving only the file.
# In batch_mode=True the ephemeral progress message is suppressed —
# the batch status board already shows per-slot upload progress,
# and an extra message between files breaks "always at bottom".
# ════════════════════════════════════════════════════════
async def send_media_with_progress(
    bot_client,
    chat_id: int,
    media_path: str,
    media_type: str,
    caption: str,
    filename: str = "",
    batch_mode: bool = False,
) -> Optional[Any]:
    """
    Send a file to Telegram with a live upload progress bar.
    Returns the sent message object, or None on failure.
    The upload progress message is sent standalone (no reply_to)
    so it doesn't pollute the reply chain, and is deleted after.
    Pass batch_mode=True to suppress the ephemeral progress message.
    """
    fname_short   = short_name(filename or os.path.basename(media_path), 35)
    file_size     = os.path.getsize(media_path) if os.path.exists(media_path) else 0
    upload_start  = time()
    last_edit     = [0.0]

    # Send ephemeral progress message (suppressed in batch mode — the batch
    # status board tracks per-slot upload progress instead)
    prog_msg = None
    if not batch_mode:
        try:
            prog_msg = await bot_client.send_message(
                chat_id,
                f"📤 **Uploading…** `{fname_short}`\n\n"
                f"`░░░░░░░░░░` 0%\n"
                f"**Size:** `{fmt_size(file_size)}`")
        except Exception:
            prog_msg = None

    async def _upload_progress(current: int, total: int):
        now = time()
        if now - last_edit[0] < PyroConf.UPLOAD_PROGRESS_INTERVAL:
            return
        last_edit[0] = now
        elapsed = now - upload_start
        speed   = current / elapsed if elapsed > 0 else 0
        actual_total = total if total and total > 0 else file_size
        try:
            if prog_msg:
                await prog_msg.edit(
                    build_progress_text(
                        "upload", current, actual_total,
                        speed, elapsed, fname_short))
        except Exception:
            pass

    send_map = {
        "photo":    bot_client.send_photo,
        "video":    bot_client.send_video,
        "audio":    bot_client.send_audio,
        "document": bot_client.send_document,
    }
    sender = send_map.get(media_type, bot_client.send_document)

    sent_msg = None
    for attempt in range(1, PyroConf.MAX_UPLOAD_RETRIES + 1):
        try:
            sent_msg = await sender(
                chat_id, media_path,
                caption=caption or "",
                progress=_upload_progress,
            )
            break
        except FloodPremiumWait as e:
            wait_s = int(getattr(e, "value", 0) or 15)
            LOGGER(__name__).warning(
                f"FloodPremiumWait {wait_s}s on upload "
                f"(attempt {attempt}/{PyroConf.MAX_UPLOAD_RETRIES})")
            if attempt < PyroConf.MAX_UPLOAD_RETRIES:
                await asyncio.sleep(wait_s + 2)
                continue
            LOGGER(__name__).error("Upload FloodPremiumWait retries exhausted")
            sent_msg = None
            break
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 30)
            LOGGER(__name__).warning(
                f"FloodWait {wait_s}s on upload "
                f"(attempt {attempt}/{PyroConf.MAX_UPLOAD_RETRIES})")
            if attempt < PyroConf.MAX_UPLOAD_RETRIES:
                await asyncio.sleep(wait_s + 2)
                continue
            LOGGER(__name__).error("Upload FloodWait retries exhausted")
            sent_msg = None
            break
        except Exception as e:
            LOGGER(__name__).error(
                f"Upload error attempt {attempt}/{PyroConf.MAX_UPLOAD_RETRIES}: {e}")
            if attempt < PyroConf.MAX_UPLOAD_RETRIES:
                await asyncio.sleep(5 * attempt)
                continue
            sent_msg = None
            break

    # Always clean up the ephemeral progress message
    if prog_msg:
        try:
            await prog_msg.delete()
        except Exception:
            pass

    return sent_msg

# ════════════════════════════════════════════════════════
# FORWARD ANY MESSAGE TYPE
# Handles: media, text, polls, stickers, forwards, etc.
# Preserves reply chains via src_to_dst_map.
# ════════════════════════════════════════════════════════
async def edit_placeholder_with_media(
    chat_id: int,
    placeholder_msg_id: int,
    media_path: str,
    media_type: str,
    caption: str,
    filename: str,
) -> bool:
    """
    Convert a placeholder text message into the actual media in-place.

    Uses user.edit_message_media() (MTProto messages.editMessage with media param)
    which — unlike the Bot API — can attach a file to an existing text message,
    effectively replacing the placeholder with the real content at the exact same
    chat position.

    Falls back gracefully if edit_message_media fails for any reason.
    Returns True if the in-place replacement succeeded, False otherwise.
    """
    # Build the appropriate InputMedia wrapper
    type_map = {
        "photo":    InputMediaPhoto,
        "video":    InputMediaVideo,
        "audio":    InputMediaAudio,
        "document": InputMediaDocument,
    }
    media_cls = type_map.get(media_type, InputMediaDocument)

    try:
        input_media = media_cls(media=media_path, caption=caption or "")
        await user.edit_message_media(
            chat_id=chat_id,
            message_id=placeholder_msg_id,
            media=input_media,
        )
        LOGGER(__name__).info(
            f"✅ Placeholder {placeholder_msg_id} replaced with media in-place")
        return True
    except Exception as e:
        LOGGER(__name__).warning(
            f"edit_message_media failed (will fall back to reply): {e}")
        return False


async def _send_with_flood_retry(send_fn, *args, retries: int = 3, **kwargs) -> Optional[Any]:
    """
    Call any bot send_* function with FloodWait retry.
    On FloodWait, sleeps the required duration and retries up to `retries` times.
    Returns the sent message or None on failure.
    """
    for attempt in range(1, retries + 1):
        try:
            return await send_fn(*args, **kwargs)
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            LOGGER(__name__).warning(
                f"FloodWait {wait_s}s on {send_fn.__name__} "
                f"(attempt {attempt}/{retries})")
            if attempt < retries and wait_s > 0:
                await asyncio.sleep(wait_s + 1)
                continue
            LOGGER(__name__).error(
                f"{send_fn.__name__} FloodWait retries exhausted")
            return None
        except Exception as e:
            LOGGER(__name__).error(
                f"{send_fn.__name__} failed attempt {attempt}/{retries}: {e}")
            if attempt < retries:
                await asyncio.sleep(3 * attempt)
                continue
            return None
    return None


async def forward_message(
    src_msg,
    chat_id: int,
    caption_override: str = "",
    media_path: str = None,
    media_type: str = None,
    is_partial: bool = False,
    src_to_dst_map: Dict[int, int] = None,
    filename: str = "",
    batch_mode: bool = False,
) -> Optional[Any]:
    """
    Forward a single message to chat_id.
    - If media_path provided: send that file (download path)
    - Else: forward text/polls/other content
    - Preserves reply chain: if src_msg.reply_to_message_id is in
      src_to_dst_map, use mapped dst message_id as reply_to
    - No reply_to_message_id to the /bdl command — files are standalone
    - batch_mode=True suppresses the ephemeral upload progress message
    """
    # Resolve reply chain
    reply_to = None
    if src_to_dst_map and src_msg.reply_to_message_id:
        reply_to = src_to_dst_map.get(src_msg.reply_to_message_id)

    # Build caption
    caption = caption_override
    if not caption:
        try:
            caption = await get_parsed_msg(
                src_msg.caption or src_msg.text or "",
                src_msg.caption_entities or src_msg.entities)
        except Exception:
            caption = src_msg.caption or src_msg.text or ""

    if is_partial and media_path:
        # Prepend partial warning to caption
        actual = os.path.getsize(media_path) if os.path.exists(media_path) else 0
        expected = get_expected_file_size(src_msg) or 0
        warn = (
            f"⚠️ **Partial File** — download was cut short.\n"
            f"Got `{fmt_size(actual)}` / expected `{fmt_size(expected)}`\n"
            f"Use `/retry` to attempt a full re-download.\n"
            f"{'━' * 19}\n"
        )
        caption = warn + (caption or "")

    sent_msg = None

    if media_path and os.path.exists(media_path):
        sent_msg = await send_media_with_progress(
            bot, chat_id, media_path,
            media_type or "document",
            caption, filename or os.path.basename(media_path),
            batch_mode=batch_mode)

    elif src_msg.text or src_msg.caption:
        sent_msg = await _send_with_flood_retry(
            bot.send_message,
            chat_id, caption or "\u200b",
            reply_to_message_id=reply_to)

    elif src_msg.poll:
        poll = src_msg.poll
        opts = [o.text for o in poll.options]
        sent_msg = await _send_with_flood_retry(
            bot.send_poll,
            chat_id, poll.question, opts,
            is_anonymous=poll.is_anonymous,
            allows_multiple_answers=poll.allows_multiple_answers,
            reply_to_message_id=reply_to)

    elif src_msg.location:
        loc = src_msg.location
        sent_msg = await _send_with_flood_retry(
            bot.send_location,
            chat_id, loc.latitude, loc.longitude,
            reply_to_message_id=reply_to)

    elif src_msg.contact:
        c = src_msg.contact
        sent_msg = await _send_with_flood_retry(
            bot.send_contact,
            chat_id, c.phone_number, c.first_name,
            last_name=c.last_name,
            reply_to_message_id=reply_to)

    elif src_msg.dice:
        sent_msg = await _send_with_flood_retry(
            bot.send_dice,
            chat_id, emoji=src_msg.dice.emoji,
            reply_to_message_id=reply_to)

    else:
        # Fallback: Pyrogram forward
        try:
            fwd = await bot.forward_messages(
                chat_id, src_msg.chat.id, src_msg.id)
            sent_msg = fwd[0] if isinstance(fwd, list) else fwd
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            LOGGER(__name__).warning(f"FloodWait {wait_s}s on forward_messages")
            await asyncio.sleep(wait_s + 1)
            try:
                fwd = await bot.forward_messages(
                    chat_id, src_msg.chat.id, src_msg.id)
                sent_msg = fwd[0] if isinstance(fwd, list) else fwd
            except Exception as e2:
                LOGGER(__name__).error(f"forward_messages retry failed: {e2}")
        except Exception as e:
            LOGGER(__name__).error(f"Generic forward failed: {e}")

    return sent_msg

# ════════════════════════════════════════════════════════
# PYROGRAM CLIENTS
# ════════════════════════════════════════════════════════
bot = Client(
    "media_bot",
    api_id=PyroConf.API_ID,
    api_hash=PyroConf.API_HASH,
    bot_token=PyroConf.BOT_TOKEN,
    workers=100,
    parse_mode=ParseMode.MARKDOWN,
    max_concurrent_transmissions=10,
    sleep_threshold=30,
)

user = Client(
    "user_session",
    api_id=PyroConf.API_ID,
    api_hash=PyroConf.API_HASH,
    session_string=PyroConf.SESSION_STRING,
    workers=100,
    max_concurrent_transmissions=10,
    sleep_threshold=30,
)

# ════════════════════════════════════════════════════════
# GLOBAL STATE
# ════════════════════════════════════════════════════════
RUNNING_TASKS: set = set()
download_semaphore: Optional[asyncio.Semaphore] = None
_skip_events: Dict[int, asyncio.Event] = {}
_kill_events: Dict[int, asyncio.Event] = {}   # hard kill per user

def get_skip_event(chat_id: int) -> asyncio.Event:
    if chat_id not in _skip_events:
        _skip_events[chat_id] = asyncio.Event()
    return _skip_events[chat_id]

def get_kill_event(chat_id: int) -> asyncio.Event:
    if chat_id not in _kill_events:
        _kill_events[chat_id] = asyncio.Event()
    return _kill_events[chat_id]

_refresh_events: Dict[int, asyncio.Event] = {}

def get_refresh_event(chat_id: int) -> asyncio.Event:
    if chat_id not in _refresh_events:
        _refresh_events[chat_id] = asyncio.Event()
    return _refresh_events[chat_id]

def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    task.add_done_callback(RUNNING_TASKS.discard)
    return task

# ════════════════════════════════════════════════════════
# WEBHOOK HELPERS
# ════════════════════════════════════════════════════════
def _msg(chat_id: int, text: str, parse_mode: str = "Markdown",
         reply_markup: dict = None,
         disable_web_page_preview: bool = False) -> dict:
    payload = {"method": "sendMessage", "chat_id": chat_id,
               "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if disable_web_page_preview:
        payload["disable_web_page_preview"] = True
    return payload

def _update_channel_markup() -> dict:
    return {"inline_keyboard": [[
        {"text": "Update Channel", "url": "https://t.me/itsSmartDev"}
    ]]}

# ════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ════════════════════════════════════════════════════════
def handle_start(chat_id: int) -> dict:
    return _msg(chat_id,
        "👋 **Welcome to Media Downloader Bot!**\n\n"
        "I can copy entire channels — media, text, polls, stickers, everything.\n"
        "Just send me a link (paste it directly or use `/dl <link>`).\n\n"
        "ℹ️ Use `/help` to view all commands.\n"
        "🔒 Make sure the user client is part of the source chat.\n\n"
        "Ready? Send me a Telegram post link!",
        reply_markup=_update_channel_markup(),
        disable_web_page_preview=True)

def handle_help(chat_id: int) -> dict:
    return _msg(chat_id,
        "💡 **Media Downloader Bot Help**\n\n"
        "➤ **Single Post**\n"
        "   `/dl <post_URL>` or paste the link directly.\n\n"
        "➤ **Batch Copy**\n"
        "   `/bdl start_link end_link`\n"
        "   Copies ALL posts in range: media, text, polls, stickers.\n"
        "   Reply chains are preserved.\n"
        "   💡 `/bdl https://t.me/ch/100 https://t.me/ch/120`\n\n"
        "➤ `/retry`   – re-attempt failed/partial posts from last batch\n"
        "➤ `/report`  – get a file listing all failed/partial/skipped posts\n"
        "➤ `/settings` – configure bot preferences (e.g. status bar position)\n"
        "➤ `/skip`    – skip current stuck file in a batch\n"
        "➤ `/killall` – hard-stop everything immediately\n"
        "➤ `/refresh` – force-refresh the live status board instantly\n"
        "➤ `/logs`    – get the bot log file\n"
        "➤ `/cleanup` – delete leftover temp files\n"
        "➤ `/stats`   – system & bot statistics\n\n"
        "**Partial downloads:** if a file can't be fully downloaded, the bot\n"
        "sends whatever it got with a ⚠️ warning. Use `/retry` to fix it.",
        reply_markup=_update_channel_markup(),
        disable_web_page_preview=True)

def handle_stats(chat_id: int) -> dict:
    uptime     = get_readable_time(int(time() - PyroConf.BOT_START_TIME))
    total, used, free = shutil.disk_usage(".")
    net        = psutil.net_io_counters()
    cpu        = psutil.cpu_percent(interval=None)  # non-blocking; uses last sample
    mem        = psutil.virtual_memory()
    proc       = psutil.Process(os.getpid())
    active     = len([t for t in RUNNING_TASKS if not t.done()])
    dl_files   = sum(len(files) for _, _, files in os.walk("downloads")) \
                 if os.path.isdir("downloads") else 0
    retryable  = 0
    if chat_id in _batch_registry:
        retryable = len(_batch_registry[chat_id].get_retryable())

    return _msg(chat_id,
        "📊 **Bot Statistics**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 **Uptime:**       `{uptime}`\n"
        f"⚙️ **Active Jobs:**  `{active}`\n"
        f"📁 **Temp Files:**   `{dl_files}`\n"
        f"🔁 **Retryable:**    `{retryable}` slot(s) from last batch\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"💾 **Disk:**         `{fmt_size(used)}` / `{fmt_size(total)}` "
        f"(`{fmt_size(free)}` free)\n"
        f"🧠 **Memory:**       `{round(proc.memory_info()[0]/1024**2)} MiB` "
        f"/ `{round(mem.total/1024**2)} MiB`\n"
        f"⚡ **CPU:**          `{cpu}%`\n"
        f"🌐 **Network:**      ↑`{fmt_size(net.bytes_sent)}` "
        f"↓`{fmt_size(net.bytes_recv)}`\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👷 **Workers:**      fetch=`{PyroConf.MAX_FETCH_WORKERS}` "
        f"dl=`{PyroConf.MAX_CONCURRENT_DOWNLOADS}` "
        f"ul=`{PyroConf.MAX_UPLOAD_WORKERS}` "
        f"retries=`{PyroConf.MAX_DOWNLOAD_RETRIES}`")

def handle_killall(chat_id: int) -> dict:
    # Hard kill: set kill event + cancel all tasks for this user
    get_kill_event(chat_id).set()
    get_skip_event(chat_id).set()
    # Cancel all worker tasks for this batch
    killed = 0
    for t in list(_batch_worker_tasks.get(chat_id, [])):
        if not t.done():
            t.cancel()
            killed += 1
    # Also cancel all global running tasks (belt and suspenders)
    cancelled = sum(
        1 for t in list(RUNNING_TASKS)
        if not t.done() and t.cancel())
    return _msg(chat_id,
        f"🛑 **Hard Stop Executed**\n\n"
        f"Cancelled `{killed}` download worker(s) and `{cancelled}` task(s).\n"
        f"_Everything for your batch has been stopped immediately._")

def handle_skip(chat_id: int) -> dict:
    get_skip_event(chat_id).set()
    return _msg(chat_id,
        "⏭️ **Skip signal sent.**\n"
        "_Current download will be abandoned and the batch continues._")

def handle_cleanup(chat_id: int) -> dict:
    try:
        files_removed, bytes_freed = cleanup_downloads_root()
        if files_removed == 0:
            return _msg(chat_id, "🧹 **Cleanup complete** — no leftover files found.")
        return _msg(chat_id,
            f"🧹 **Cleanup complete!**\n\n"
            f"🗑️ Removed `{files_removed}` file(s)\n"
            f"💾 Freed `{fmt_size(bytes_freed)}`")
    except Exception as e:
        LOGGER(__name__).error(f"Cleanup failed: {e}")
        return _msg(chat_id, "❌ **Cleanup failed.** Check `/logs` for details.")

def handle_settings(chat_id: int) -> dict:
    """Send the settings menu with inline toggle buttons."""
    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": _settings_text(chat_id),
        "parse_mode": "Markdown",
        "reply_markup": _settings_keyboard_dict(chat_id),
    }

async def handle_report(chat_id: int, reply_to_id: int):
    """
    Generate and send a report file listing every non-completed slot
    from the last batch: ABANDONED, PARTIAL, FAILED, SKIPPED.
    Includes the original post URL so the user can re-process manually.
    """
    state_mgr = _batch_registry.get(chat_id)
    if not state_mgr:
        await bot.send_message(
            chat_id,
            "❌ **No batch found.**\n\nRun a `/bdl` batch first.",
            reply_to_message_id=reply_to_id)
        return

    report_statuses = {
        TaskStatus.ABANDONED, TaskStatus.PARTIAL,
        TaskStatus.FAILED,    TaskStatus.SKIPPED,
    }
    slots = [
        ts for ts in sorted(state_mgr.state_map.values(), key=lambda x: x.index)
        if ts.status in report_statuses
    ]

    if not slots:
        await bot.send_message(
            chat_id,
            "✅ **Nothing to report** — all slots completed successfully!",
            reply_to_message_id=reply_to_id)
        return

    # Build report text
    icon_map = {
        TaskStatus.ABANDONED: "🔲 ABANDONED",
        TaskStatus.PARTIAL:   "⚠️ PARTIAL",
        TaskStatus.FAILED:    "❌ FAILED",
        TaskStatus.SKIPPED:   "⏭️ SKIPPED",
    }
    lines = [
        f"Batch Report — {len(slots)} issue(s) out of {state_mgr.total} total",
        f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
        "",
    ]
    for ts in slots:
        status_label = icon_map.get(ts.status, str(ts.status))
        fname        = ts.filename or f"Post #{ts.index + 1}"
        error_note   = f"  Error: {ts.error}" if ts.error else ""
        lines += [
            f"[{ts.index + 1:03d}] {status_label}",
            f"  URL:  {ts.url}",
            f"  File: {fname}",
        ]
        if error_note:
            lines.append(error_note)
        lines.append("")

    report_text = "\n".join(lines)
    report_path = f"/tmp/batch_report_{chat_id}.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    counts = {s: sum(1 for ts in slots if ts.status == s) for s in report_statuses}
    caption = (
        f"\U0001f4cb **Batch Report** \u2014 `{len(slots)}` issue(s)\n\n"
        f"\U0001f7eb Abandoned: `{counts[TaskStatus.ABANDONED]}`\n"
        f"\u26a0\ufe0f Partial:   `{counts[TaskStatus.PARTIAL]}`\n"
        f"\u274c Failed:    `{counts[TaskStatus.FAILED]}`\n"
        f"\u23ed\ufe0f Skipped:   `{counts[TaskStatus.SKIPPED]}`\n\n"
        f"_Each entry includes the original post URL for manual recovery._"
    )
    try:
        await bot.send_document(
            chat_id, report_path,
            caption=caption,
            reply_to_message_id=reply_to_id)
    except Exception as e:
        LOGGER(__name__).error(f"Report send failed: {e}")
        await bot.send_message(
            chat_id, f"❌ Failed to send report: `{e}`",
            reply_to_message_id=reply_to_id)
    finally:
        try:
            os.remove(report_path)
        except Exception:
            pass


def handle_refresh(chat_id: int) -> dict:
    """Force an instant redraw of the live status board via the refresh_event."""
    state_mgr = _batch_registry.get(chat_id)
    if not state_mgr:
        return _msg(chat_id,
            "ℹ️ **No active batch** — nothing to refresh.\n"
            "_Run `/bdl` to start a batch._")
    get_refresh_event(chat_id).set()
    return _msg(chat_id, "🔄 **Status board refreshed!**")

# ════════════════════════════════════════════════════════
# MESSAGE PROXY (for /dl single download replies)
# ════════════════════════════════════════════════════════
class _MsgProxy:
    def __init__(self, cid: int, rid: int):
        self.id   = rid
        self.chat = type("_Chat", (), {"id": cid})()

    async def reply(self, text, **kw):
        if "reply_markup" in kw:
            raise NotImplementedError(
                "_MsgProxy.reply does not support reply_markup — "
                "use bot.send_message() directly.")
        await bot.send_message(self.chat.id, text,
                               reply_to_message_id=self.id)

# ════════════════════════════════════════════════════════
# SINGLE DOWNLOAD HANDLER (/dl)
# ════════════════════════════════════════════════════════
async def handle_download(chat_id: int, reply_to_id: int, post_url: str):
    async with download_semaphore:
        if "?" in post_url:
            post_url = post_url.split("?", 1)[0]
        message = _MsgProxy(chat_id, reply_to_id)
        try:
            _cid, message_id = getChatMsgID(post_url)
            status_msg = await bot.send_message(
                chat_id, "🔍 **Fetching post info…**",
                reply_to_message_id=reply_to_id)

            chat_message = await user.get_messages(
                chat_id=_cid, message_ids=message_id)
            LOGGER(__name__).info(f"Downloading from: {post_url}")

            if chat_message.media and not has_downloadable_media(chat_message):
                await status_msg.edit(
                    "❌ **No Downloadable Media**\n\n"
                    "This post contains only a link preview or unsupported media type.")
                return

            if chat_message.document or chat_message.video or chat_message.audio:
                file_size = (
                    chat_message.document.file_size if chat_message.document else
                    chat_message.video.file_size    if chat_message.video    else
                    chat_message.audio.file_size)
                if not await fileSizeLimit(file_size, message, "download",
                                           user.me.is_premium):
                    await status_msg.delete()
                    return

            parsed_caption = await get_parsed_msg(
                chat_message.caption or "", chat_message.caption_entities)
            parsed_text = await get_parsed_msg(
                chat_message.text or "", chat_message.entities)

            if chat_message.media_group_id:
                await status_msg.edit("📥 **Downloading…**")
                if not await _send_single_media(
                        chat_message, chat_id, reply_to_id=None):
                    await status_msg.edit(
                        "❌ **Failed**\n\nCould not extract media from this post.")
                else:
                    await status_msg.delete()
                return

            elif has_downloadable_media(chat_message):
                filename      = get_file_name(message_id, chat_message)
                fname_short   = short_name(filename)
                expected_size = get_expected_file_size(chat_message) or 0
                download_path = get_download_path(reply_to_id, filename)

                await status_msg.edit(
                    f"🔍 **Preparing download…**\n\n"
                    f"📄 `{fname_short}`\n"
                    f"📦 Size: `{fmt_size(expected_size) if expected_size else 'unknown'}`")

                dl_start  = time()
                last_edit = [0.0]

                async def on_progress(current: int, total: int):
                    now = time()
                    if now - last_edit[0] < PyroConf.DOWNLOAD_PROGRESS_INTERVAL:
                        return
                    last_edit[0] = now
                    elapsed = now - dl_start
                    speed   = current / elapsed if elapsed > 0 else 0
                    actual_total = total if total and total > 0 else expected_size
                    try:
                        await status_msg.edit(
                            build_progress_text(
                                "download", current, actual_total,
                                speed, elapsed, fname_short))
                    except Exception:
                        pass

                media_path, err, is_partial = await download_single_message(
                    chat_message, download_path,
                    progress_message=on_progress,
                    start_time=dl_start)

                if not media_path:
                    await status_msg.edit(
                        f"❌ **Download Failed**\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📄 `{fname_short}`\n"
                        f"💬 `{err}`\n\n"
                        f"_Please try again._")
                    return

                actual_size = os.path.getsize(media_path)
                dl_elapsed  = time() - dl_start
                dl_speed    = actual_size / dl_elapsed if dl_elapsed > 0 else 0

                await status_msg.edit(
                    f"📤 **Uploading to Telegram…**\n\n"
                    f"📄 `{fname_short}`\n"
                    f"📦 `{fmt_size(actual_size)}`"
                    + (f"\n\n⚠️ _Partial file ({err})_" if is_partial else ""))

                media_type = (
                    "photo"    if chat_message.photo else
                    "video"    if chat_message.video else
                    "audio"    if chat_message.audio else
                    "document")

                caption = parsed_caption
                if is_partial:
                    caption = (
                        f"⚠️ **Partial File** — {err}\n"
                        f"{'━' * 19}\n" + caption)

                sent_msg = await send_media_with_progress(
                    bot, chat_id, media_path, media_type, caption, filename)
                cleanup_download(media_path)

                total_elapsed = time() - dl_start
                result_text = (
                    f"{'⚠️ Partial Download' if is_partial else '✅ Download Complete!'}\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📄 **File:** `{fname_short}`\n"
                    f"📦 **Size:** `{fmt_size(actual_size)}`\n"
                    f"⚡ **Speed:** `{fmt_size(dl_speed)}/s`\n"
                    f"⏱ **Time:** `{fmt_time(total_elapsed)}`"
                    + (f"\n\n_Use /retry if you need the complete file._"
                       if is_partial else ""))
                await status_msg.edit(result_text)

            elif chat_message.text or chat_message.caption:
                await status_msg.delete()
                await bot.send_message(chat_id, parsed_text or parsed_caption)
            else:
                await status_msg.edit(
                    "❌ **Nothing to Download**\n\n"
                    "This post contains no media or text.")

        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            LOGGER(__name__).warning(f"FloodWait: {wait_s}s")
            try:
                await status_msg.edit(
                    f"⏳ **Rate Limited**\n\nWaiting `{wait_s}s`…")
            except Exception:
                pass
            if wait_s > 0:
                await asyncio.sleep(wait_s + 1)
        except (PeerIdInvalid, BadRequest, KeyError):
            try:
                await status_msg.edit(
                    "❌ **Access Denied**\n\n"
                    "Make sure the user client is a member of the source chat.")
            except Exception:
                await message.reply("❌ **Access Denied**")
        except Exception as e:
            LOGGER(__name__).error(traceback.format_exc())
            try:
                await status_msg.edit(
                    f"❌ **Something Went Wrong**\n\n`{str(e)[:200]}`")
            except Exception:
                await message.reply(f"❌ **Error:** `{str(e)[:200]}`")


async def _send_single_media(chat_message, chat_id: int,
                             reply_to: int = None) -> bool:
    """Send one media item standalone (used by /dl for media groups)."""
    if not has_downloadable_media(chat_message):
        return False
    filename      = get_file_name(chat_message.id, chat_message)
    download_path = get_download_path(chat_id, filename)
    media_path, err, is_partial = await download_single_message(
        chat_message, download_path)
    if not media_path:
        return False
    cap = ""
    try:
        cap = await get_parsed_msg(
            chat_message.caption or "", chat_message.caption_entities)
    except Exception:
        cap = chat_message.caption or ""
    if is_partial:
        actual = os.path.getsize(media_path)
        expected = get_expected_file_size(chat_message) or 0
        cap = f"⚠️ **Partial File** {fmt_size(actual)}/{fmt_size(expected)}\n" + cap
    media_type = (
        "photo" if chat_message.photo else
        "video" if chat_message.video else
        "audio" if chat_message.audio else "document")
    await send_media_with_progress(bot, chat_id, media_path,
                                   media_type, cap, filename)
    cleanup_download(media_path)
    return True

# ════════════════════════════════════════════════════════
# SLOT RESULT
# ════════════════════════════════════════════════════════
@dataclass
class _SlotResult:
    index:      int
    msg:        Any
    media_path: Optional[str] = None
    caption:    str           = ""
    media_type: str           = "document"
    filename:   str           = ""
    error:      Optional[str] = None
    skipped:    bool          = False
    is_partial: bool          = False   # partial file — send with warning

# ════════════════════════════════════════════════════════
# DOWNLOAD WORKER
# ════════════════════════════════════════════════════════
async def _download_worker(
    index:       int,
    chat_message,              # single message OR list of messages (media group)
    reply_to_id: int,
    slot_future: asyncio.Future,
    state_mgr:   BatchStateManager,
    worker_sem:  asyncio.Semaphore,
    skip_event:  asyncio.Event,
    kill_event:  asyncio.Event,
):
    async with worker_sem:
        if skip_event.is_set() or kill_event.is_set():
            await state_mgr.put(index, status=TaskStatus.SKIPPED)
            slot_future.set_result(_SlotResult(
                index=index, msg=chat_message, skipped=True))
            return

        # ── Media group: download each item, package as list ──────────────
        if isinstance(chat_message, list):
            group_msgs = chat_message
            _grp_url = state_mgr.state_map[index].url if index in state_mgr.state_map else ""
            LOGGER(__name__).info(
                f"[Slot {index}] 🗂️  Media group ({len(group_msgs)} items)  {_grp_url}")
            group_results = []
            for gm in group_msgs:
                if not has_downloadable_media(gm):
                    continue
                gfilename  = get_file_name(gm.id, gm)
                gpath      = get_download_path(reply_to_id, f"grp_{gm.id}_{gfilename}")
                gexpected  = get_expected_file_size(gm) or 0
                await state_mgr.put(index, status=TaskStatus.DOWNLOADING,
                                    filename=gfilename, file_size=gexpected)
                gpath_dl, gerr, gpartial = await download_single_message(
                    gm, gpath,
                    _src_chat_id=getattr(getattr(gm, 'chat', None), 'id', None))
                if kill_event.is_set():
                    for r in group_results:
                        if r.get("path") and os.path.exists(r["path"]):
                            cleanup_download(r["path"])
                    if gpath_dl and os.path.exists(gpath_dl):
                        cleanup_download(gpath_dl)
                    await state_mgr.put(index, status=TaskStatus.SKIPPED)
                    slot_future.set_result(_SlotResult(
                        index=index, msg=group_msgs[0], skipped=True))
                    return
                gcap = ""
                try:
                    gcap = await get_parsed_msg(
                        gm.caption or "", gm.caption_entities)
                except Exception:
                    gcap = gm.caption or ""
                gmtype = ("photo" if gm.photo else "video" if gm.video
                          else "audio" if gm.audio else "document")
                group_results.append({
                    "msg_id": gm.id,
                    "path":    gpath_dl,     # None if download failed
                    "err":     gerr,
                    "partial": gpartial,
                    "cap":     gcap,
                    "mtype":   gmtype,
                    "filename": gfilename,
                    "expected": gexpected,
                })

            if not group_results:
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status = TaskStatus.ABANDONED
                await state_mgr.put(index, status=TaskStatus.ABANDONED,
                                    error="No downloadable media in group")
                slot_future.set_result(_SlotResult(
                    index=index, msg=group_msgs[0],
                    error="No downloadable media in group"))
                return

            has_any_success = any(r["path"] for r in group_results)
            await state_mgr.put(index, status=TaskStatus.SENDING)
            slot_future.set_result(_SlotResult(
                index=index, msg=group_msgs[0],
                caption="", media_type="group",
                filename=f"media_group_{len(group_results)}_items",
                error=None,
                is_partial=any(r["partial"] for r in group_results)
                           or not has_any_success,
                media_path=json.dumps(group_results, default=str),
            ))
            return

        # ── Single message ─────────────────────────────────────────────────
        await state_mgr.put(index, status=TaskStatus.DOWNLOADING)

        # Log the post URL so it's visible in the shell for each item
        _slot_url = state_mgr.state_map[index].url if index in state_mgr.state_map else ""
        LOGGER(__name__).info(f"[Slot {index}] ⬇️  {_slot_url}")

        if not has_downloadable_media(chat_message):
            # Text/poll/etc — no download needed
            LOGGER(__name__).info(f"[Slot {index}] 📝 Text/non-media → forwarding directly")
            await state_mgr.put(index, status=TaskStatus.SENDING,
                                filename=f"Post #{index+1}")
            slot_future.set_result(_SlotResult(
                index=index, msg=chat_message,
                caption="", media_type="text"))
            return

        filename      = get_file_name(chat_message.id, chat_message)
        download_path = get_download_path(reply_to_id, filename)
        expected_size = get_expected_file_size(chat_message) or 0

        LOGGER(__name__).info(
            f"[Slot {index}] 📦 {filename}  {fmt_size(expected_size)}  {_slot_url}")
        await state_mgr.put(index, filename=filename, file_size=expected_size)

        _last_prog = [0.0]
        async def _slot_progress(current: int, total: int):
            if kill_event.is_set():
                return
            now = time()
            if now - _last_prog[0] < PyroConf.STATE_LOG_INTERVAL:
                return
            _last_prog[0] = now
            actual_total = total if total and total > 0 else expected_size
            await state_mgr.put(index, bytes_done=current, file_size=actual_total)

        media_path, err, is_partial = await download_single_message(
            chat_message, download_path,
            progress_message=_slot_progress,
            _src_chat_id=getattr(getattr(chat_message, 'chat', None), 'id', None))

        if kill_event.is_set():
            if media_path and os.path.exists(media_path):
                cleanup_download(media_path)
            await state_mgr.put(index, status=TaskStatus.SKIPPED)
            slot_future.set_result(_SlotResult(
                index=index, msg=chat_message, skipped=True))
            return

        if not media_path:
            # Write directly to state_map so /retry sees it immediately,
            # and also enqueue via put() so the UI loop is notified.
            if index in state_mgr.state_map:
                state_mgr.state_map[index].status = TaskStatus.ABANDONED
            await state_mgr.put(index, status=TaskStatus.ABANDONED,
                                error=err or "Download failed")
            slot_future.set_result(_SlotResult(
                index=index, msg=chat_message,
                filename=filename,
                error=err or "Download failed"))
            return

        cap = ""
        try:
            cap = await get_parsed_msg(
                chat_message.caption or "",
                chat_message.caption_entities)
        except Exception:
            cap = chat_message.caption or ""

        media_type = (
            "photo"    if chat_message.photo else
            "video"    if chat_message.video else
            "audio"    if chat_message.audio else
            "document")

        status = TaskStatus.PARTIAL if is_partial else TaskStatus.SENDING
        await state_mgr.put(
            index, status=status,
            filename=filename,
            file_size=os.path.getsize(media_path))

        slot_future.set_result(_SlotResult(
            index=index, msg=chat_message,
            media_path=media_path,
            caption=cap,
            media_type=media_type,
            filename=filename,
            is_partial=is_partial,
        ))

# ════════════════════════════════════════════════════════
# SEND ONE SLOT
# Handles: media, partial, abandoned, text, polls, etc.
# Updates src_to_dst_map for reply chain preservation.
# Returns (sent, failed, skipped, next_index)
# ════════════════════════════════════════════════════════
async def _send_one_slot(
    index:          int,
    results:        Dict[int, _SlotResult],
    chat_id:        int,
    state_mgr:      BatchStateManager,
    src_to_dst_map: Dict[int, int],
    kill_event:     asyncio.Event,
) -> tuple:
    result = results[index]
    sent = failed = skipped = 0

    if kill_event.is_set():
        if result.media_path and os.path.exists(result.media_path):
            cleanup_download(result.media_path)
        skipped = 1
        return sent, failed, skipped, index + 1

    if result.skipped:
        skipped = 1
        if result.media_path:
            cleanup_download(result.media_path)
        LOGGER(__name__).info(f"[Sender] Slot {index} skipped.")
        return sent, failed, skipped, index + 1

    src_msg = result.msg

    # ── Abandoned: no bytes at all ─────────────────────────────────────────
    if result.error and not result.media_path and result.media_type != "group":
        failed = 1
        fname  = short_name(result.filename or f"Post #{index + 1}", 35)
        LOGGER(__name__).error(f"[Sender] Slot {index} abandoned: {result.error}")
        try:
            placeholder = await bot.send_message(
                chat_id,
                f"🔲 **File Unavailable** — Post #{index + 1}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📄 `{fname}`\n"
                f"⚠️ _{result.error[:150]}_\n\n"
                f"_The file exists in the source channel but could not be downloaded.\n"
                f"Use `/retry` after the batch completes to try again._")
            # Write directly so /retry sees it immediately, and also enqueue for UI.
            if index in state_mgr.state_map:
                state_mgr.state_map[index].status           = TaskStatus.ABANDONED
            await state_mgr.put(index, status=TaskStatus.ABANDONED,
                                placeholder_msg_id=placeholder.id)
        except Exception as e:
            LOGGER(__name__).error(f"Placeholder send failed: {e}")
        return sent, failed, skipped, index + 1

    # ── Media group slot ───────────────────────────────────────────────────
    if result.media_type == "group" and result.media_path:
        try:
            group_items = json.loads(result.media_path)
        except Exception:
            group_items = []

        group_sent = group_failed = 0
        failed_filenames = []

        for item in group_items:
            ipath    = item.get("path")
            icap     = item.get("cap", "")
            imtype   = item.get("mtype", "document")
            ifname   = item.get("filename", "")
            ipartial = item.get("partial", False)
            ierr     = item.get("err", "")
            iexpected = item.get("expected", 0)

            # Item downloaded successfully (full or partial)
            if ipath and os.path.exists(ipath):
                actual = os.path.getsize(ipath)
                if ipartial:
                    icap = (f"⚠️ **Partial** `{fmt_size(actual)}`/`{fmt_size(iexpected)}`\n"
                            f"_Use `/retry` for full file._\n" + icap)
                try:
                    sm = await send_media_with_progress(
                        bot, chat_id, ipath, imtype, icap, ifname,
                        batch_mode=True)
                    if sm and src_msg:
                        src_to_dst_map[src_msg.id] = sm.id
                    group_sent += 1
                    if ipartial:
                        group_failed += 1  # partial still needs retry
                except FloodWait as e:
                    wait_s = int(getattr(e, "value", 0) or 0)
                    await asyncio.sleep(wait_s + 1)
                    try:
                        sm = await send_media_with_progress(
                            bot, chat_id, ipath, imtype, icap, ifname,
                            batch_mode=True)
                        group_sent += 1
                    except Exception as e2:
                        group_failed += 1
                        failed_filenames.append(ifname)
                        LOGGER(__name__).error(f"Group item retry send failed: {e2}")
                except Exception as e:
                    group_failed += 1
                    failed_filenames.append(ifname)
                    LOGGER(__name__).error(f"Group item send failed: {e}")
                finally:
                    cleanup_download(ipath)
            else:
                # Item download failed — send placeholder to preserve sequence
                group_failed += 1
                failed_filenames.append(ifname)
                fname_short = short_name(ifname or "unknown", 35)
                LOGGER(__name__).warning(
                    f"Group item missing/failed: {ifname} — {ierr}")
                try:
                    ph = await bot.send_message(
                        chat_id,
                        f"🔲 **File Unavailable** (group item)\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📄 `{fname_short}`\n"
                        f"⚠️ _{str(ierr)[:120] or 'Download failed'}_\n\n"
                        f"_Use `/retry` to attempt re-download._")
                    # Store the placeholder for /retry
                    if index in state_mgr.state_map:
                        state_mgr.state_map[index].placeholder_msg_id = ph.id
                    await state_mgr.put(index, placeholder_msg_id=ph.id)
                except Exception as pe:
                    LOGGER(__name__).error(f"Group placeholder send failed: {pe}")

        # Set final slot status
        if group_sent > 0 and group_failed == 0:
            sent = 1
            await state_mgr.put(index, status=TaskStatus.COMPLETED)
        elif group_sent > 0 and group_failed > 0:
            sent = 1
            failed = 1   # partial success — mark both
            _gerr = f"Some items failed: {', '.join(failed_filenames)}"
            if index in state_mgr.state_map:
                state_mgr.state_map[index].status = TaskStatus.PARTIAL
            await state_mgr.put(index, status=TaskStatus.PARTIAL, error=_gerr)
        else:
            failed = 1
            if index in state_mgr.state_map:
                state_mgr.state_map[index].status = TaskStatus.ABANDONED
            await state_mgr.put(index, status=TaskStatus.ABANDONED)
        return sent, failed, skipped, index + 1

    # ── Media (full or partial) ────────────────────────────────────────────
    if result.media_path:
        if not os.path.exists(result.media_path):
            failed = 1
            LOGGER(__name__).error(f"[Sender] Slot {index}: file gone before send")
            try:
                ph = await bot.send_message(
                    chat_id,
                    f"🔲 **File Lost** — Post #{index + 1}\n\n"
                    f"`{os.path.basename(result.media_path)}`\n"
                    f"_File was removed before upload. Use `/retry`._")
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status           = TaskStatus.ABANDONED
                await state_mgr.put(index, status=TaskStatus.ABANDONED,
                                    placeholder_msg_id=ph.id)
            except Exception:
                pass
            return sent, failed, skipped, index + 1

        try:
            sent_msg = await forward_message(
                src_msg=src_msg,
                chat_id=chat_id,
                media_path=result.media_path,
                media_type=result.media_type,
                is_partial=result.is_partial,
                src_to_dst_map=src_to_dst_map,
                filename=result.filename,
                batch_mode=True,
            )
            fsize = (os.path.getsize(result.media_path)
                     if os.path.exists(result.media_path) else 0)
            if result.is_partial:
                failed += 1
                _ph_id = sent_msg.id if sent_msg else None
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status = TaskStatus.PARTIAL
                    if _ph_id:
                        state_mgr.state_map[index].placeholder_msg_id = _ph_id
                _put_kw: dict = {"status": TaskStatus.PARTIAL}
                if _ph_id:
                    _put_kw["placeholder_msg_id"] = _ph_id
                await state_mgr.put(index, **_put_kw)
            else:
                sent = 1
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status   = TaskStatus.COMPLETED
                await state_mgr.put(index, status=TaskStatus.COMPLETED, file_size=fsize)
            if sent_msg and src_msg:
                src_to_dst_map[src_msg.id] = sent_msg.id
                await state_mgr.put(index, sent_msg_id=sent_msg.id)
            LOGGER(__name__).info(
                f"[Sender] {'⚠️' if result.is_partial else '✅'} "
                f"Slot {index} sent ({fmt_size(fsize)})")
        except Exception as e:
            failed = 1
            if index in state_mgr.state_map:
                state_mgr.state_map[index].status = TaskStatus.FAILED
            await state_mgr.put(index, status=TaskStatus.FAILED, error=str(e))
            LOGGER(__name__).error(f"[Sender] Send failed slot {index}: {e}")
            try:
                ph = await bot.send_message(
                    chat_id,
                    f"🔲 **Upload Failed** — Post #{index + 1}\n\n"
                    f"`{str(e)[:200]}`\n_Use `/retry` to try again._")
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].placeholder_msg_id = ph.id
                await state_mgr.put(index, placeholder_msg_id=ph.id)
            except Exception:
                pass
        finally:
            cleanup_download(result.media_path)
        return sent, failed, skipped, index + 1

    # ── Text / poll / other non-media ─────────────────────────────────────
    if src_msg:
        try:
            sent_msg = await forward_message(
                src_msg=src_msg,
                chat_id=chat_id,
                src_to_dst_map=src_to_dst_map,
                batch_mode=True,
            )
            await state_mgr.put(index, status=TaskStatus.COMPLETED)
            sent = 1
            if sent_msg and src_msg:
                src_to_dst_map[src_msg.id] = sent_msg.id
                await state_mgr.put(index, sent_msg_id=sent_msg.id)
        except Exception as e:
            failed = 1
            await state_mgr.put(index, status=TaskStatus.FAILED, error=str(e))
            LOGGER(__name__).error(f"[Sender] Non-media forward failed slot {index}: {e}")
    else:
        skipped = 1

    return sent, failed, skipped, index + 1


# ════════════════════════════════════════════════════════
# SONIC SENDER — out-of-order uploads with placeholder gaps
#
# Normal mode:  strictly sequential — wait for slot[i] before sending slot[i+1]
#
# Sonic mode:   upload any slot the instant it's ready.
#               For slots that arrive out of order, send "reserved" placeholder
#               messages first to hold positions in the chat sequence, then
#               replace them in-place when the real content arrives.
#
# Example with slots 0,1,2,3,4 and download order 2,4,0,1,3:
#
#   slot 2 ready first:
#     → send placeholder for slot 0   [0-ph]
#     → send placeholder for slot 1   [1-ph]
#     → upload slot 2 directly        [2-file]
#
#   slot 4 ready next (slot 3 not yet done):
#     → send placeholder for slot 3   [3-ph]
#     → upload slot 4 directly        [4-file]
#
#   slot 0 ready:
#     → replace [0-ph] in-place with real content via edit_message_media
#
#   slot 1 ready:
#     → replace [1-ph] in-place
#
#   slot 3 ready:
#     → replace [3-ph] in-place
#
# Key invariants:
#   1. For every slot i, either a real file OR a placeholder exists in chat
#      before any slot j>i uploads — sequence is always preserved visually.
#   2. A placeholder is NEVER sent for a slot that already has a real file.
#   3. Placeholders are tracked in sonic_ph_map {slot_index → ph_msg_id}.
#   4. edit_message_media runs on the user client (can convert text→media).
#   5. Simultaneous uploads are serialised via upload_sem so Telegram isn't
#      hammered — configurable via MAX_SONIC_UPLOADS env var.
# ════════════════════════════════════════════════════════
async def _sonic_sender(
    futures:        List[asyncio.Future],
    chat_id:        int,
    state_mgr:      "BatchStateManager",
    src_to_dst_map: Dict[int, int],
    skip_event:     asyncio.Event,
    kill_event:     asyncio.Event,
) -> dict:
    n          = len(futures)
    sent = failed = skipped = 0

    # Track which slot indices already have their real file in chat
    # (so we never send a placeholder for them)
    uploaded_slots: set = set()

    # Map: slot_index → placeholder message_id
    sonic_ph_map: Dict[int, int] = {}

    # Serialise simultaneous uploads — too many concurrent uploads to the same
    # chat causes Telegram to return FLOOD_WAIT or silently drop messages.
    max_concurrent = int(os.getenv("MAX_SONIC_UPLOADS", "2"))
    upload_sem     = asyncio.Semaphore(max_concurrent)

    # Lock to serialise placeholder creation — prevents two coroutines from
    # both deciding to send a placeholder for the same gap simultaneously.
    ph_lock = asyncio.Lock()

    ready_queue: asyncio.Queue      = asyncio.Queue()
    results: Dict[int, _SlotResult] = {}

    def _on_done(index: int, fut: asyncio.Future):
        if fut.cancelled():
            results[index] = _SlotResult(index=index, msg=None, skipped=True)
        elif fut.exception():
            results[index] = _SlotResult(
                index=index, msg=None, error=str(fut.exception()))
        else:
            results[index] = fut.result()
        ready_queue.put_nowait(index)

    for i, fut in enumerate(futures):
        fut.add_done_callback(lambda f, idx=i: _on_done(idx, f))
        if fut.done():
            _on_done(i, fut)

    async def _ensure_gaps_filled(up_to_index: int):
        """
        Before uploading slot `up_to_index`, ensure every slot 0…up_to_index-1
        that has NOT yet been uploaded has a placeholder in chat.
        Uses ph_lock so concurrent callers don't double-send placeholders.
        """
        async with ph_lock:
            for gap in range(up_to_index):
                if gap in uploaded_slots:
                    continue          # real file already in chat — skip
                if gap in sonic_ph_map:
                    continue          # placeholder already sent — skip
                if kill_event.is_set():
                    break
                # Need a placeholder for this gap
                gap_ts  = state_mgr.state_map.get(gap)
                fname   = gap_ts.filename if gap_ts else f"Post #{gap + 1}"
                fsize   = gap_ts.file_size if gap_ts else 0
                url     = gap_ts.url if gap_ts else ""
                size_str = f"\n📦 `{fmt_size(fsize)}`" if fsize else ""
                LOGGER(__name__).info(
                    f"[Sonic] Creating gap placeholder for slot {gap}: {fname}")
                try:
                    ph = await bot.send_message(
                        chat_id,
                        f"⚡ **Downloading…** — Post #{gap + 1}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📄 `{short_name(fname, 35)}`{size_str}\n"
                        f"🔗 `{url}`\n\n"
                        f"_Will be replaced automatically when ready._")
                    sonic_ph_map[gap] = ph.id
                    if gap_ts:
                        gap_ts.placeholder_msg_id = ph.id
                    LOGGER(__name__).info(
                        f"[Sonic] Placeholder for slot {gap} = msg {ph.id}")
                except FloodWait as e:
                    wait_s = int(getattr(e, "value", 0) or 5)
                    LOGGER(__name__).warning(
                        f"[Sonic] FloodWait {wait_s}s sending gap placeholder")
                    await asyncio.sleep(wait_s + 1)
                    try:
                        ph = await bot.send_message(
                            chat_id,
                            f"⚡ **Downloading…** — Post #{gap + 1}\n"
                            f"📄 `{short_name(fname, 35)}`\n"
                            f"_Will replace automatically when ready._")
                        sonic_ph_map[gap] = ph.id
                        if gap_ts:
                            gap_ts.placeholder_msg_id = ph.id
                    except Exception as e2:
                        LOGGER(__name__).error(
                            f"[Sonic] Gap placeholder retry failed slot {gap}: {e2}")
                except Exception as e:
                    LOGGER(__name__).error(
                        f"[Sonic] Gap placeholder failed slot {gap}: {e}")

    async def _upload_slot(index: int):
        nonlocal sent, failed, skipped
        result = results.get(index)
        if not result:
            return

        if kill_event.is_set():
            await state_mgr.put(index, status=TaskStatus.SKIPPED)
            skipped += 1
            return

        if result.skipped:
            await state_mgr.put(index, status=TaskStatus.SKIPPED)
            skipped += 1
            if result.media_path and os.path.exists(result.media_path):
                cleanup_download(result.media_path)
            return

        src_msg = result.msg

        # ── Abandoned (no file, not a group) ──────────────────────────────
        if result.error and not result.media_path and result.media_type != "group":
            failed += 1
            fname = short_name(result.filename or f"Post #{index + 1}", 35)
            LOGGER(__name__).error(f"[Sonic] Slot {index} abandoned: {result.error}")
            # If a gap placeholder already exists, update it to show failure
            ph_id = sonic_ph_map.get(index)
            if ph_id:
                try:
                    await bot.edit_message_text(
                        chat_id, ph_id,
                        f"🔲 **File Unavailable** — Post #{index + 1}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📄 `{fname}`\n"
                        f"⚠️ _{result.error[:150]}_\n\n"
                        f"_Use `/retry` to attempt re-download._")
                except Exception as e:
                    LOGGER(__name__).debug(f"[Sonic] Placeholder update failed: {e}")
            else:
                # No placeholder yet — send an abandoned placeholder
                try:
                    ph = await bot.send_message(
                        chat_id,
                        f"🔲 **File Unavailable** — Post #{index + 1}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📄 `{fname}`\n"
                        f"⚠️ _{result.error[:150]}_\n\n"
                        f"_Use `/retry` to attempt re-download._")
                    sonic_ph_map[index] = ph.id
                except Exception:
                    pass
            if index in state_mgr.state_map:
                state_mgr.state_map[index].status = TaskStatus.ABANDONED
                ph_id2 = sonic_ph_map.get(index)
                if ph_id2:
                    state_mgr.state_map[index].placeholder_msg_id = ph_id2
            _ph2 = sonic_ph_map.get(index)
            _put_kw: dict = {"status": TaskStatus.ABANDONED}
            if _ph2:
                _put_kw["placeholder_msg_id"] = _ph2
            await state_mgr.put(index, **_put_kw)
            return

        # ── Mark as uploaded so _ensure_gaps_filled won't send a placeholder ──
        # Must happen BEFORE we acquire upload_sem to prevent a race where
        # another coroutine sees this slot as a gap and sends a placeholder
        # right as we're about to upload the real file.
        uploaded_slots.add(index)

        # ── Media group ───────────────────────────────────────────────────
        if result.media_type == "group" and result.media_path:
            try:
                group_items = json.loads(result.media_path)
            except Exception:
                group_items = []
            group_sent_count = group_failed_count = 0
            ph_id = sonic_ph_map.get(index)

            async with upload_sem:
                for item in group_items:
                    ipath    = item.get("path")
                    icap     = item.get("cap", "")
                    imtype   = item.get("mtype", "document")
                    ifname   = item.get("filename", "")
                    ipartial = item.get("partial", False)
                    iexpected = item.get("expected", 0)
                    ierr     = item.get("err", "")

                    if ipath and os.path.exists(ipath):
                        actual = os.path.getsize(ipath)
                        if ipartial:
                            icap = (
                                f"⚠️ **Partial** `{fmt_size(actual)}`/`{fmt_size(iexpected)}`\n"
                                f"_Use `/retry` for full file._\n" + icap)
                        try:
                            sm = await send_media_with_progress(
                                bot, chat_id, ipath, imtype, icap, ifname,
                                batch_mode=True)
                            if sm and src_msg:
                                src_to_dst_map[src_msg.id] = sm.id
                            group_sent_count += 1
                            if ipartial:
                                group_failed_count += 1
                        except FloodWait as e:
                            w = int(getattr(e, "value", 0) or 0)
                            await asyncio.sleep(w + 1)
                            try:
                                sm = await send_media_with_progress(
                                    bot, chat_id, ipath, imtype, icap, ifname,
                                    batch_mode=True)
                                group_sent_count += 1
                            except Exception as e2:
                                group_failed_count += 1
                                LOGGER(__name__).error(
                                    f"[Sonic] Group item retry failed: {e2}")
                        except Exception as e:
                            group_failed_count += 1
                            LOGGER(__name__).error(
                                f"[Sonic] Group item failed: {e}")
                        finally:
                            cleanup_download(ipath)
                    else:
                        group_failed_count += 1
                        LOGGER(__name__).warning(
                            f"[Sonic] Group item missing: {ifname} — {ierr}")

            if ph_id:
                # Update the gap placeholder to show group result
                try:
                    icon = "✅" if group_failed_count == 0 else "⚠️"
                    await bot.edit_message_text(
                        chat_id, ph_id,
                        f"{icon} **Media Group** — Post #{index + 1}\n"
                        f"`{group_sent_count}` files sent"
                        + (f", `{group_failed_count}` failed" if group_failed_count else ""))
                except Exception:
                    pass

            if group_sent_count > 0:
                sent += 1
                _st = TaskStatus.PARTIAL if group_failed_count > 0 else TaskStatus.COMPLETED
                await state_mgr.put(index, status=_st)
            else:
                failed += 1
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status = TaskStatus.ABANDONED
                await state_mgr.put(index, status=TaskStatus.ABANDONED)
            return

        # ── Single media slot ─────────────────────────────────────────────
        if result.media_path:
            if not os.path.exists(result.media_path):
                failed += 1
                LOGGER(__name__).error(
                    f"[Sonic] Slot {index}: file gone before upload")
                ph_id = sonic_ph_map.get(index)
                if ph_id:
                    try:
                        await bot.edit_message_text(
                            chat_id, ph_id,
                            f"🔲 **File Lost** — Post #{index + 1}\n"
                            f"_File removed before upload. Use `/retry`._")
                    except Exception:
                        pass
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status = TaskStatus.ABANDONED
                await state_mgr.put(index, status=TaskStatus.ABANDONED)
                return

            ph_id = sonic_ph_map.get(index)
            await state_mgr.put(index, status=TaskStatus.UPLOADING)

            replaced_in_place = False
            if ph_id:
                # Try to replace the gap placeholder in-place
                cap = result.caption or ""
                if result.is_partial:
                    actual   = os.path.getsize(result.media_path)
                    expected = get_expected_file_size(result.msg) or 0 if result.msg else 0
                    cap = (f"⚠️ **Partial File** — Got `{fmt_size(actual)}`"
                           f" / expected `{fmt_size(expected)}`\n"
                           f"Use `/retry` for full file.\n"
                           f"{'━' * 19}\n") + cap

                async with upload_sem:
                    replaced_in_place = await edit_placeholder_with_media(
                        chat_id=chat_id,
                        placeholder_msg_id=ph_id,
                        media_path=result.media_path,
                        media_type=result.media_type,
                        caption=cap,
                        filename=result.filename,
                    )

            if replaced_in_place:
                # In-place success — clean up
                cleanup_download(result.media_path)
                LOGGER(__name__).info(
                    f"[Sonic] ✅ Slot {index} replaced placeholder in-place")
                sent += 1 if not result.is_partial else 0
                failed += 1 if result.is_partial else 0
                _st = TaskStatus.PARTIAL if result.is_partial else TaskStatus.COMPLETED
                await state_mgr.put(index, status=_st)
                if src_msg and result.msg:
                    src_to_dst_map[src_msg.id] = ph_id
                    await state_mgr.put(index, sent_msg_id=ph_id)
            else:
                # edit_message_media failed — fall back to sending new message
                try:
                    async with upload_sem:
                        sent_msg = await forward_message(
                            src_msg=src_msg,
                            chat_id=chat_id,
                            media_path=result.media_path,
                            media_type=result.media_type,
                            is_partial=result.is_partial,
                            src_to_dst_map=src_to_dst_map,
                            filename=result.filename,
                            batch_mode=True,
                        )
                    cleanup_download(result.media_path)
                    fsize = state_mgr.state_map[index].file_size if index in state_mgr.state_map else 0
                    LOGGER(__name__).info(
                        f"[Sonic] ✅ Slot {index} sent (fallback) {fmt_size(fsize)}")
                    sent += 1 if not result.is_partial else 0
                    failed += 1 if result.is_partial else 0
                    _st = TaskStatus.PARTIAL if result.is_partial else TaskStatus.COMPLETED
                    await state_mgr.put(index, status=_st)
                    if sent_msg:
                        await state_mgr.put(index, sent_msg_id=sent_msg.id)
                    if sent_msg and src_msg:
                        src_to_dst_map[src_msg.id] = sent_msg.id
                    # Update the gap placeholder to link to the new message
                    if ph_id and sent_msg:
                        try:
                            link = (f"https://t.me/c/"
                                    f"{str(chat_id).lstrip('-100')}/{sent_msg.id}")
                            icon  = "⚠️" if result.is_partial else "✅"
                            label = "Partial" if result.is_partial else "Uploaded"
                            await bot.edit_message_text(
                                chat_id, ph_id,
                                f"{icon} **{label}** — Post #{index + 1}\n"
                                f"[Jump to message ↑]({link})",
                                disable_web_page_preview=True)
                        except Exception as e:
                            LOGGER(__name__).debug(
                                f"[Sonic] Gap placeholder link update failed: {e}")
                except Exception as e:
                    failed += 1
                    cleanup_download(result.media_path)
                    LOGGER(__name__).error(f"[Sonic] Upload failed slot {index}: {e}")
                    if index in state_mgr.state_map:
                        state_mgr.state_map[index].status = TaskStatus.FAILED
                    await state_mgr.put(index, status=TaskStatus.FAILED, error=str(e))
            return

        # ── Text / poll / other non-media ─────────────────────────────────
        if src_msg:
            ph_id = sonic_ph_map.get(index)
            try:
                async with upload_sem:
                    sent_msg = await forward_message(
                        src_msg=src_msg, chat_id=chat_id,
                        src_to_dst_map=src_to_dst_map,
                        batch_mode=True)
                await state_mgr.put(index, status=TaskStatus.COMPLETED)
                sent += 1
                if sent_msg and src_msg:
                    src_to_dst_map[src_msg.id] = sent_msg.id
                    await state_mgr.put(index, sent_msg_id=sent_msg.id)
                # Update gap placeholder if one was sent
                if ph_id and sent_msg:
                    try:
                        link = (f"https://t.me/c/"
                                f"{str(chat_id).lstrip('-100')}/{sent_msg.id}")
                        await bot.edit_message_text(
                            chat_id, ph_id,
                            f"✅ **Forwarded** — Post #{index + 1}\n"
                            f"[Jump to message ↑]({link})",
                            disable_web_page_preview=True)
                    except Exception:
                        pass
            except Exception as e:
                failed += 1
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status = TaskStatus.FAILED
                await state_mgr.put(index, status=TaskStatus.FAILED, error=str(e))
                LOGGER(__name__).error(f"[Sonic] Text forward failed slot {index}: {e}")
        else:
            skipped += 1

    # ── Main loop: dispatch upload tasks as slots become ready ────────────
    pending_uploads: Dict[int, asyncio.Task] = {}  # slot → upload task
    completed_count = 0

    while completed_count < n:
        if kill_event.is_set():
            # Cancel all pending upload tasks
            for t in pending_uploads.values():
                if not t.done():
                    t.cancel()
            remaining = n - completed_count
            skipped += remaining
            break

        if skip_event.is_set():
            # Skip the lowest-index not-yet-started slot
            skip_event.clear()
            for idx in range(n):
                if idx not in results and idx not in pending_uploads:
                    await state_mgr.put(idx, status=TaskStatus.SKIPPED)
                    skipped += 1
                    completed_count += 1
                    LOGGER(__name__).info(f"[Sonic] Slot {idx} skipped by user")
                    break

        # Poll for newly completed downloads
        try:
            ready_idx = await asyncio.wait_for(ready_queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            # Clean up completed upload tasks
            done_tasks = [idx for idx, t in list(pending_uploads.items())
                          if t.done()]
            for idx in done_tasks:
                t = pending_uploads.pop(idx)
                try:
                    await t  # propagate any exception
                except Exception as e:
                    LOGGER(__name__).error(
                        f"[Sonic] Upload task for slot {idx} raised: {e}")
                completed_count += 1
                await state_mgr.move_status_to_bottom()
            continue

        # A download just completed — dispatch its upload immediately
        if ready_idx in pending_uploads or ready_idx not in results:
            continue  # already dispatched or spurious signal

        # Fill gap placeholders for all earlier slots that aren't uploaded yet
        await _ensure_gaps_filled(ready_idx)

        if kill_event.is_set():
            break

        # Dispatch the upload as an asyncio task so it runs concurrently
        # with future downloads and gap filling
        t = asyncio.create_task(
            _upload_slot(ready_idx),
            name=f"sonic_upload_{ready_idx}")
        pending_uploads[ready_idx] = t
        LOGGER(__name__).info(
            f"[Sonic] Dispatched upload task for slot {ready_idx}")

        # Drain any already-completed upload tasks
        for idx in [i for i, t in list(pending_uploads.items()) if t.done()]:
            t = pending_uploads.pop(idx)
            try:
                await t
            except Exception as e:
                LOGGER(__name__).error(
                    f"[Sonic] Upload task slot {idx} raised: {e}")
            completed_count += 1
            await state_mgr.move_status_to_bottom()

    # Wait for all in-flight upload tasks to finish
    if pending_uploads:
        await asyncio.gather(*pending_uploads.values(), return_exceptions=True)
        completed_count += len(pending_uploads)

    return {"sent": sent, "failed": failed, "skipped": skipped}

# ════════════════════════════════════════════════════════
# STRICT SONIC SENDER — guaranteed sequence + instant delivery
#
# Downloads are fully parallel (same as sonic mode).
# A single `chat_writer` coroutine is the ONLY thing that ever
# calls send_message / forward_message / edit_message_text.
# This makes send ordering 100% deterministic.
#
# Flow for slots 0,1,2,3,4 with download order 2,4,0,1,3:
#
#   write_pointer=0: slot 0 not ready → send [0-ph], advance
#   write_pointer=1: slot 1 not ready → send [1-ph], advance
#   write_pointer=2: slot 2 IS ready  → send [2-file] directly, advance
#   write_pointer=3: slot 3 not ready → send [3-ph], advance
#   write_pointer=4: slot 4 IS ready  → send [4-file] directly, advance
#   (loop ends, chat_writer waits for pending_replace slots)
#   slot 0 finishes → edit [0-ph] → real file
#   slot 1 finishes → edit [1-ph] → real file
#   slot 3 finishes → edit [3-ph] → real file
#
# Speed gain: slot 2 appears in chat immediately instead of waiting
# for slots 0 and 1 to fully download. Downloads run in parallel
# filling slots while the writer already moved past them.
# ════════════════════════════════════════════════════════
async def _strict_sonic_sender(
    futures:        List[asyncio.Future],
    chat_id:        int,
    state_mgr:      "BatchStateManager",
    src_to_dst_map: Dict[int, int],
    skip_event:     asyncio.Event,
    kill_event:     asyncio.Event,
) -> dict:
    n = len(futures)
    sent = failed = skipped = 0

    # slot_index → _SlotResult, filled by done-callbacks
    results:       Dict[int, "_SlotResult"] = {}
    # slot_index → placeholder message_id (for slots sent as ph while waiting)
    sonic_ph_map:  Dict[int, int]           = {}
    # slot_index → asyncio.Event, pulsed when results[index] is set
    ready_events:  Dict[int, asyncio.Event] = {i: asyncio.Event() for i in range(n)}

    def _on_done(index: int, fut: asyncio.Future):
        if fut.cancelled():
            results[index] = _SlotResult(index=index, msg=None, skipped=True)
        elif fut.exception():
            results[index] = _SlotResult(
                index=index, msg=None, error=str(fut.exception()))
        else:
            results[index] = fut.result()
        ready_events[index].set()

    for i, fut in enumerate(futures):
        fut.add_done_callback(lambda f, idx=i: _on_done(idx, f))
        if fut.done():
            _on_done(i, fut)

    # ── Helper: send a "downloading…" placeholder for a slot ─────────────
    async def _send_gap_placeholder(gap: int):
        gap_ts   = state_mgr.state_map.get(gap)
        fname    = gap_ts.filename if gap_ts else f"Post #{gap + 1}"
        fsize    = gap_ts.file_size if gap_ts else 0
        url      = gap_ts.url if gap_ts else ""
        size_str = f"\n📦 `{fmt_size(fsize)}`" if fsize else ""
        text = (
            f"⚡ **Downloading…** — Post #{gap + 1}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📄 `{short_name(fname, 35)}`{size_str}\n"
            f"🔗 `{url}`\n\n"
            f"_Will be replaced automatically when ready._"
        )
        for attempt in range(2):
            try:
                ph = await bot.send_message(chat_id, text)
                sonic_ph_map[gap] = ph.id
                if gap_ts:
                    gap_ts.placeholder_msg_id = ph.id
                LOGGER(__name__).info(
                    f"[StrictSonic] Placeholder slot {gap} → msg {ph.id}")
                return
            except FloodWait as e:
                wait_s = int(getattr(e, "value", 0) or 5)
                LOGGER(__name__).warning(
                    f"[StrictSonic] FloodWait {wait_s}s sending placeholder slot {gap}")
                await asyncio.sleep(wait_s + 1)
            except Exception as e:
                LOGGER(__name__).error(
                    f"[StrictSonic] Placeholder failed slot {gap}: {e}")
                break

    # ── Helper: upload the real content for a slot (or update its ph) ────
    async def _send_real(index: int):
        nonlocal sent, failed, skipped
        result = results.get(index)
        if not result:
            return

        if result.skipped:
            await state_mgr.put(index, status=TaskStatus.SKIPPED)
            skipped += 1
            if result.media_path and os.path.exists(result.media_path):
                cleanup_download(result.media_path)
            return

        src_msg = result.msg
        ph_id   = sonic_ph_map.get(index)

        # ── Error / abandoned ────────────────────────────────────────────
        if result.error and not result.media_path and result.media_type != "group":
            failed += 1
            fname = short_name(result.filename or f"Post #{index + 1}", 35)
            LOGGER(__name__).error(
                f"[StrictSonic] Slot {index} abandoned: {result.error}")
            err_text = (
                f"🔲 **File Unavailable** — Post #{index + 1}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📄 `{fname}`\n"
                f"⚠️ _{result.error[:150]}_\n\n"
                f"_Use `/retry` to attempt re-download._"
            )
            if ph_id:
                try:
                    await bot.edit_message_text(chat_id, ph_id, err_text)
                except Exception as e:
                    LOGGER(__name__).debug(
                        f"[StrictSonic] Placeholder update failed: {e}")
            else:
                try:
                    ph = await bot.send_message(chat_id, err_text)
                    sonic_ph_map[index] = ph.id
                except Exception:
                    pass
            _ph2 = sonic_ph_map.get(index)
            if index in state_mgr.state_map:
                state_mgr.state_map[index].status = TaskStatus.ABANDONED
                if _ph2:
                    state_mgr.state_map[index].placeholder_msg_id = _ph2
            _put_kw: dict = {"status": TaskStatus.ABANDONED}
            if _ph2:
                _put_kw["placeholder_msg_id"] = _ph2
            await state_mgr.put(index, **_put_kw)
            return

        # ── Media group ──────────────────────────────────────────────────
        if result.media_type == "group" and result.media_path:
            try:
                group_items = json.loads(result.media_path)
            except Exception:
                group_items = []
            group_sent_count = group_failed_count = 0

            for item in group_items:
                ipath    = item.get("path")
                icap     = item.get("cap", "")
                imtype   = item.get("mtype", "document")
                ifname   = item.get("filename", "")
                ipartial = item.get("partial", False)
                iexpected = item.get("expected", 0)
                ierr     = item.get("err", "")

                if ipath and os.path.exists(ipath):
                    actual = os.path.getsize(ipath)
                    if ipartial:
                        icap = (
                            f"⚠️ **Partial** `{fmt_size(actual)}`/`{fmt_size(iexpected)}`\n"
                            f"_Use `/retry` for full file._\n" + icap)
                    try:
                        sm = await send_media_with_progress(
                            bot, chat_id, ipath, imtype, icap, ifname,
                            batch_mode=True)
                        if sm and src_msg:
                            src_to_dst_map[src_msg.id] = sm.id
                        group_sent_count += 1
                        if ipartial:
                            group_failed_count += 1
                    except FloodWait as e:
                        w = int(getattr(e, "value", 0) or 0)
                        await asyncio.sleep(w + 1)
                        try:
                            sm = await send_media_with_progress(
                                bot, chat_id, ipath, imtype, icap, ifname,
                                batch_mode=True)
                            group_sent_count += 1
                        except Exception as e2:
                            group_failed_count += 1
                            LOGGER(__name__).error(
                                f"[StrictSonic] Group item retry failed: {e2}")
                    except Exception as e:
                        group_failed_count += 1
                        LOGGER(__name__).error(
                            f"[StrictSonic] Group item failed: {e}")
                    finally:
                        cleanup_download(ipath)
                else:
                    group_failed_count += 1
                    LOGGER(__name__).warning(
                        f"[StrictSonic] Group item missing: {ifname} — {ierr}")

            if ph_id:
                try:
                    icon = "✅" if group_failed_count == 0 else "⚠️"
                    await bot.edit_message_text(
                        chat_id, ph_id,
                        f"{icon} **Media Group** — Post #{index + 1}\n"
                        f"`{group_sent_count}` files sent"
                        + (f", `{group_failed_count}` failed"
                           if group_failed_count else ""))
                except Exception:
                    pass

            if group_sent_count > 0:
                sent += 1
                _st = (TaskStatus.PARTIAL if group_failed_count > 0
                       else TaskStatus.COMPLETED)
                await state_mgr.put(index, status=_st)
            else:
                failed += 1
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status = TaskStatus.ABANDONED
                await state_mgr.put(index, status=TaskStatus.ABANDONED)
            return

        # ── Single media slot ────────────────────────────────────────────
        if result.media_path:
            if not os.path.exists(result.media_path):
                failed += 1
                LOGGER(__name__).error(
                    f"[StrictSonic] Slot {index}: file gone before upload")
                if ph_id:
                    try:
                        await bot.edit_message_text(
                            chat_id, ph_id,
                            f"🔲 **File Lost** — Post #{index + 1}\n"
                            f"_File removed before upload. Use `/retry`._")
                    except Exception:
                        pass
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status = TaskStatus.ABANDONED
                await state_mgr.put(index, status=TaskStatus.ABANDONED)
                return

            await state_mgr.put(index, status=TaskStatus.UPLOADING)

            replaced_in_place = False
            if ph_id:
                cap = result.caption or ""
                if result.is_partial:
                    actual   = os.path.getsize(result.media_path)
                    expected = (get_expected_file_size(result.msg) or 0
                                if result.msg else 0)
                    cap = (
                        f"⚠️ **Partial File** — Got `{fmt_size(actual)}`"
                        f" / expected `{fmt_size(expected)}`\n"
                        f"Use `/retry` for full file.\n"
                        f"{'━' * 19}\n"
                    ) + cap
                replaced_in_place = await edit_placeholder_with_media(
                    chat_id=chat_id,
                    placeholder_msg_id=ph_id,
                    media_path=result.media_path,
                    media_type=result.media_type,
                    caption=cap,
                    filename=result.filename,
                )

            if replaced_in_place:
                cleanup_download(result.media_path)
                LOGGER(__name__).info(
                    f"[StrictSonic] ✅ Slot {index} replaced placeholder in-place")
                sent += 1 if not result.is_partial else 0
                failed += 1 if result.is_partial else 0
                _st = (TaskStatus.PARTIAL if result.is_partial
                       else TaskStatus.COMPLETED)
                await state_mgr.put(index, status=_st)
                if src_msg and result.msg:
                    src_to_dst_map[src_msg.id] = ph_id
                    await state_mgr.put(index, sent_msg_id=ph_id)
            elif ph_id:
                # edit_message_media failed AND a placeholder is already in chat.
                # Sending a new message here would break sequence (it would appear
                # below later slots). Instead: mark ABANDONED and edit the placeholder
                # to a clear error notice so the user can /retry at the correct position.
                failed += 1
                cleanup_download(result.media_path)
                LOGGER(__name__).warning(
                    f"[StrictSonic] edit_message_media failed for slot {index} "
                    f"(placeholder {ph_id}) — marking ABANDONED for /retry")
                try:
                    await bot.edit_message_text(
                        chat_id, ph_id,
                        f"🔲 **Upload Failed** — Post #{index + 1}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"📄 `{short_name(result.filename or f'Post #{index+1}', 35)}`\n\n"
                        f"_edit_message_media failed (permissions or unsupported type).\n"
                        f"Use `/retry` to re-upload at this position._")
                except Exception as edit_err:
                    LOGGER(__name__).debug(
                        f"[StrictSonic] Placeholder error-edit failed: {edit_err}")
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status = TaskStatus.ABANDONED
                    state_mgr.state_map[index].placeholder_msg_id = ph_id
                await state_mgr.put(index, status=TaskStatus.ABANDONED,
                                    placeholder_msg_id=ph_id)
            else:
                # No placeholder exists — this slot was sent directly in Phase A.
                # A new message here is at the correct position, so fall back normally.
                try:
                    sent_msg = await forward_message(
                        src_msg=src_msg,
                        chat_id=chat_id,
                        media_path=result.media_path,
                        media_type=result.media_type,
                        is_partial=result.is_partial,
                        src_to_dst_map=src_to_dst_map,
                        filename=result.filename,
                        batch_mode=True,
                    )
                    cleanup_download(result.media_path)
                    fsize = (state_mgr.state_map[index].file_size
                             if index in state_mgr.state_map else 0)
                    LOGGER(__name__).info(
                        f"[StrictSonic] ✅ Slot {index} sent (no-ph fallback) {fmt_size(fsize)}")
                    sent += 1 if not result.is_partial else 0
                    failed += 1 if result.is_partial else 0
                    _st = (TaskStatus.PARTIAL if result.is_partial
                           else TaskStatus.COMPLETED)
                    await state_mgr.put(index, status=_st)
                    if sent_msg:
                        await state_mgr.put(index, sent_msg_id=sent_msg.id)
                    if sent_msg and src_msg:
                        src_to_dst_map[src_msg.id] = sent_msg.id
                except Exception as e:
                    failed += 1
                    cleanup_download(result.media_path)
                    LOGGER(__name__).error(
                        f"[StrictSonic] No-ph fallback failed slot {index}: {e}")
                    if index in state_mgr.state_map:
                        state_mgr.state_map[index].status = TaskStatus.FAILED
                    await state_mgr.put(index, status=TaskStatus.FAILED, error=str(e))
            return

        # ── Text / poll / other non-media ────────────────────────────────
        if src_msg:
            try:
                sent_msg = await forward_message(
                    src_msg=src_msg, chat_id=chat_id,
                    src_to_dst_map=src_to_dst_map,
                    batch_mode=True)
                await state_mgr.put(index, status=TaskStatus.COMPLETED)
                sent += 1
                if sent_msg and src_msg:
                    src_to_dst_map[src_msg.id] = sent_msg.id
                    await state_mgr.put(index, sent_msg_id=sent_msg.id)
                if ph_id and sent_msg:
                    try:
                        link = (f"https://t.me/c/"
                                f"{str(chat_id).lstrip('-100')}/{sent_msg.id}")
                        await bot.edit_message_text(
                            chat_id, ph_id,
                            f"✅ **Forwarded** — Post #{index + 1}\n"
                            f"[Jump to message ↑]({link})",
                            disable_web_page_preview=True)
                    except Exception:
                        pass
            except Exception as e:
                failed += 1
                if index in state_mgr.state_map:
                    state_mgr.state_map[index].status = TaskStatus.FAILED
                await state_mgr.put(index, status=TaskStatus.FAILED, error=str(e))
                LOGGER(__name__).error(
                    f"[StrictSonic] Text forward failed slot {index}: {e}")
        else:
            skipped += 1

    # ── chat_writer: THE ONLY coroutine that ever touches chat ───────────
    # Phase A: walk write_pointer from 0..n-1 in strict order.
    #   - If slot is ready → send real content immediately.
    #   - If slot is not ready → yield one event-loop tick (gives just-
    #     finishing downloads a chance to land), re-check once, THEN send
    #     a placeholder and record in pending_replace.
    # Phase B: replace placeholders in strict ascending index order.
    #   Waiting for each slot in order guarantees that the fallback path
    #   (when edit_message_media fails) also produces messages top-to-bottom.
    #   If edit_message_media fails AND a placeholder is in chat, we mark
    #   ABANDONED and edit the placeholder to an error notice — we never
    #   send a new message that would appear below later slots.
    async def chat_writer():
        nonlocal skipped
        write_pointer   = 0
        pending_replace: List[int] = []   # slots that got a placeholder

        # ── Phase A ──────────────────────────────────────────────────────
        while write_pointer < n:
            if kill_event.is_set():
                while write_pointer < n:
                    await state_mgr.put(write_pointer, status=TaskStatus.SKIPPED)
                    skipped += 1
                    write_pointer += 1
                return

            if skip_event.is_set():
                skip_event.clear()
                # Skip the current write_pointer slot
                fut = futures[write_pointer]
                if not fut.done():
                    fut.cancel()
                await state_mgr.put(write_pointer, status=TaskStatus.SKIPPED)
                skipped += 1
                # Remove from pending_replace if it was already queued
                if write_pointer in pending_replace:
                    pending_replace.remove(write_pointer)
                LOGGER(__name__).info(
                    f"[StrictSonic] Slot {write_pointer} skipped by user.")
                write_pointer += 1
                continue

            if write_pointer in results:
                # Slot is already downloaded — send real content now
                LOGGER(__name__).info(
                    f"[StrictSonic] writer: slot {write_pointer} ready → send real")
                await _send_real(write_pointer)
                await state_mgr.move_status_to_bottom()
                write_pointer += 1
            else:
                # FIX: yield one event-loop tick before deciding to send a
                # placeholder. Downloads that completed just before this check
                # may not have run their done-callback yet; a single yield
                # lets them do so and avoids an unnecessary placeholder.
                await asyncio.sleep(0)
                if write_pointer in results:
                    LOGGER(__name__).info(
                        f"[StrictSonic] writer: slot {write_pointer} ready after yield → send real")
                    await _send_real(write_pointer)
                    await state_mgr.move_status_to_bottom()
                    write_pointer += 1
                    continue
                # Truly not ready — send placeholder and move on
                LOGGER(__name__).info(
                    f"[StrictSonic] writer: slot {write_pointer} not ready → placeholder")
                await _send_gap_placeholder(write_pointer)
                pending_replace.append(write_pointer)
                write_pointer += 1

        # ── Phase B: replace placeholders in strict slot order ───────────
        # We wait for each slot in ascending index order. Because every slot
        # already has a placeholder holding its position in chat, the visual
        # sequence is already established. Processing in order here ensures
        # that the fallback "send new message" path (when edit_message_media
        # fails) also produces messages in the correct top-to-bottom order.
        for idx in pending_replace:
            if kill_event.is_set():
                break
            if skip_event.is_set():
                skip_event.clear()
                await state_mgr.put(idx, status=TaskStatus.SKIPPED)
                skipped += 1
                LOGGER(__name__).info(
                    f"[StrictSonic] Phase B slot {idx} skipped by user.")
                continue
            # Wait for this specific slot to finish downloading
            if not ready_events[idx].is_set():
                LOGGER(__name__).info(
                    f"[StrictSonic] Phase B: waiting for slot {idx}…")
                await ready_events[idx].wait()
            if kill_event.is_set():
                break
            LOGGER(__name__).info(
                f"[StrictSonic] Phase B: replacing placeholder for slot {idx}")
            await _send_real(idx)
            await state_mgr.move_status_to_bottom()

    await chat_writer()
    return {"sent": sent, "failed": failed, "skipped": skipped}


# ════════════════════════════════════════════════════════
# ORDERED SENDER — strict sequence, maximum throughput
#
# Why NOT parallel uploads:
#   send_document() both uploads AND sends in one atomic Telegram call.
#   There is no way to "hold" a sent message. If slot 2 uploads before
#   slot 0, it appears in the chat before slot 0 — order broken.
#
# Correct architecture (mirrors upload_all.sh sequential process_group):
#   Downloads run N-parallel (MAX_FETCH_WORKERS) → files land on disk
#   Sender is strictly sequential — sends slot[i], then slot[i+1], …
#   Speed comes from pre-downloading: by the time slot[i] finishes
#   uploading, slots[i+1…i+N] are already on disk → sender chains
#   through them with zero additional wait.
#
# This matches upv3.py's design exactly:
#   "Sequential Upload Loop: guarantees upload order"
#   "downloads are parallel, uploads are sequential"
# ════════════════════════════════════════════════════════
async def _ordered_sender(
    futures:        List[asyncio.Future],   # download futures (resolved in parallel)
    chat_id:        int,
    state_mgr:      "BatchStateManager",
    src_to_dst_map: Dict[int, int],
    skip_event:     asyncio.Event,
    kill_event:     asyncio.Event,
) -> dict:
    """
    Sends slots in strict order 0, 1, 2, …
    Downloads happen in parallel (N workers), so by the time slot[i]
    finishes uploading, slot[i+1] is likely already on disk.
    The sender immediately chains to it with zero wait.
    """
    n = len(futures)
    sent = failed = skipped = 0

    # ready_queue: download done-callbacks push indices here instantly
    ready_queue: asyncio.Queue      = asyncio.Queue()
    results: Dict[int, _SlotResult] = {}

    def _on_done(index: int, fut: asyncio.Future):
        if fut.cancelled():
            results[index] = _SlotResult(index=index, msg=None, skipped=True)
        elif fut.exception():
            results[index] = _SlotResult(
                index=index, msg=None, error=str(fut.exception()))
        else:
            results[index] = fut.result()
        ready_queue.put_nowait(index)

    for i, fut in enumerate(futures):
        fut.add_done_callback(lambda f, idx=i: _on_done(idx, f))
        if fut.done():
            _on_done(i, fut)

    send_pointer = 0   # next slot index that must be sent

    while send_pointer < n:

        # ── Hard kill ──────────────────────────────────────────────────────
        if kill_event.is_set():
            while send_pointer < n:
                await state_mgr.put(send_pointer, status=TaskStatus.SKIPPED)
                skipped += 1
                send_pointer += 1
            break

        # ── Skip current slot ──────────────────────────────────────────────
        if skip_event.is_set():
            skip_event.clear()
            fut = futures[send_pointer]
            if not fut.done():
                fut.cancel()
            await state_mgr.put(send_pointer, status=TaskStatus.SKIPPED)
            LOGGER(__name__).info(f"[Sender] Slot {send_pointer} skipped by user.")
            skipped += 1
            send_pointer += 1
            while send_pointer < n and send_pointer in results:
                s, f, sk, send_pointer = await _send_one_slot(
                    send_pointer, results, chat_id, state_mgr,
                    src_to_dst_map, kill_event)
                sent += s; failed += f; skipped += sk
                await state_mgr.move_status_to_bottom()
            continue

        # ── Send if this slot is already downloaded ────────────────────────
        if send_pointer in results:
            await state_mgr.put(send_pointer, status=TaskStatus.UPLOADING)
            s, f, sk, send_pointer = await _send_one_slot(
                send_pointer, results, chat_id, state_mgr,
                src_to_dst_map, kill_event)
            sent += s; failed += f; skipped += sk
            await state_mgr.move_status_to_bottom()
            while send_pointer < n and send_pointer in results:
                if kill_event.is_set():
                    break
                await state_mgr.put(send_pointer, status=TaskStatus.UPLOADING)
                s, f, sk, send_pointer = await _send_one_slot(
                    send_pointer, results, chat_id, state_mgr,
                    src_to_dst_map, kill_event)
                sent += s; failed += f; skipped += sk
                await state_mgr.move_status_to_bottom()
            continue

        # ── Wait for any download to complete ─────────────────────────────
        try:
            await asyncio.wait_for(ready_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        while send_pointer < n and send_pointer in results:
            if kill_event.is_set():
                break
            await state_mgr.put(send_pointer, status=TaskStatus.UPLOADING)
            s, f, sk, send_pointer = await _send_one_slot(
                send_pointer, results, chat_id, state_mgr,
                src_to_dst_map, kill_event)
            sent += s; failed += f; skipped += sk
            await state_mgr.move_status_to_bottom()

    return {"sent": sent, "failed": failed, "skipped": skipped}

# ════════════════════════════════════════════════════════
# BATCH DOWNLOAD
# ════════════════════════════════════════════════════════
async def handle_batch_download(chat_id: int, reply_to_id: int,
                                start_link: str, end_link: str):
    message = _MsgProxy(chat_id, reply_to_id)
    try:
        start_chat, start_id = getChatMsgID(start_link)
        end_chat,   end_id   = getChatMsgID(end_link)
    except Exception as e:
        await message.reply(f"**❌ Error parsing links:\n`{e}`**")
        return
    if start_chat != end_chat:
        await message.reply("**❌ Both links must be from the same channel.**")
        return
    if start_id > end_id:
        await message.reply("**❌ Invalid range: start ID cannot exceed end ID.**")
        return

    total_range = end_id - start_id + 1
    prefix      = start_link.rsplit("/", 1)[0]

    loading_msg = await bot.send_message(
        chat_id,
        f"🔍 **Fetching {total_range} posts…**\n\n"
        f"Range: `{start_id}` → `{end_id}`\n"
        f"⚡ Workers: `{PyroConf.MAX_FETCH_WORKERS}` parallel")

    # ── Phase 1: Bulk fetch ───────────────────────────────
    all_ids = list(range(start_id, end_id + 1))
    fetched_messages: Dict[int, Any] = {}
    consecutive_errors = 0
    meta_failed = 0
    _total_chunks  = max(1, (len(all_ids) + PyroConf.FETCH_BATCH_SIZE - 1)
                         // PyroConf.FETCH_BATCH_SIZE)
    _fetch_spinner = ["⠻","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    _fetch_tick    = [0]
    _last_fetch_edit = [0.0]

    for chunk_start in range(0, len(all_ids), PyroConf.FETCH_BATCH_SIZE):
        chunk     = all_ids[chunk_start: chunk_start + PyroConf.FETCH_BATCH_SIZE]
        chunk_idx = chunk_start // PyroConf.FETCH_BATCH_SIZE + 1
        fetch_pct = int(chunk_idx * 100 / _total_chunks)
        fetch_bar = progress_bar(chunk_idx, _total_chunks, length=8)
        spin      = _fetch_spinner[_fetch_tick[0] % len(_fetch_spinner)]
        _fetch_tick[0] += 1

        now = time()
        if now - _last_fetch_edit[0] >= 2.0:
            _last_fetch_edit[0] = now
            try:
                await loading_msg.edit(
                    f"{spin} **Fetching posts…**\n\n"
                    f"`{fetch_bar}` {fetch_pct}%  "
                    f"(`{chunk_idx}`/`{_total_chunks}` chunks)\n"
                    f"📥 Found `{len(fetched_messages)}` so far\n"
                    f"Range: `{start_id}` → `{end_id}`")
            except Exception:
                pass

        try:
            msgs = await user.get_messages(chat_id=start_chat, message_ids=chunk)
            if not isinstance(msgs, list):
                msgs = [msgs]
            for m in msgs:
                if m and m.id:
                    fetched_messages[m.id] = m
            consecutive_errors = 0
        except (PeerIdInvalid, BadRequest) as e:
            consecutive_errors += len(chunk)
            meta_failed        += len(chunk)
            LOGGER(__name__).error(f"Critical fetch error: {e}")
            if consecutive_errors >= PyroConf.CONSECUTIVE_ERROR_LIMIT:
                await loading_msg.delete()
                await message.reply(
                    "❌ **Batch Aborted**\n"
                    "━━━━━━━━━━━━━━━━━━━\n"
                    f"{consecutive_errors} consecutive access errors.\n"
                    "Make sure the user client is a member of the channel.")
                return
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            try:
                await loading_msg.edit(
                    f"⏳ **Rate limited during fetch…**\n\n"
                    f"Waiting `{wait_s}s` then resuming.\n"
                    f"(`{chunk_idx}`/`{_total_chunks}` chunks done)")
            except Exception:
                pass
            await asyncio.sleep(wait_s + 1)
        except Exception as e:
            meta_failed        += len(chunk)
            consecutive_errors += 1
            LOGGER(__name__).error(f"Fetch chunk error: {e}")
        if chunk_start + PyroConf.FETCH_BATCH_SIZE < len(all_ids):
            await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)

    # ── Build ordered task list ───────────────────────────
    # Each slot is either a single message or a list of messages
    # (for media groups — all items in the group are one slot).
    ordered_tasks: List[Any] = []          # each item: msg OR [msg, msg, …]
    seen_media_groups: Dict[str, int] = {} # media_group_id → index in ordered_tasks
    skipped_meta = 0

    for msg_id in all_ids:
        msg = fetched_messages.get(msg_id)
        if not msg:
            skipped_meta += 1
            continue
        if msg.media_group_id:
            gid = str(msg.media_group_id)
            if gid in seen_media_groups:
                # Append to existing group slot — don't create a new slot
                slot_idx = seen_media_groups[gid]
                existing = ordered_tasks[slot_idx]
                if isinstance(existing, list):
                    existing.append(msg)
                else:
                    ordered_tasks[slot_idx] = [existing, msg]
                continue
            else:
                seen_media_groups[gid] = len(ordered_tasks)
        # Forward EVERYTHING with content (not just media)
        if not has_any_content(msg):
            skipped_meta += 1
            continue
        ordered_tasks.append(msg)

    total_tasks = len(ordered_tasks)
    if total_tasks == 0:
        await loading_msg.delete()
        await message.reply(
            f"❌ **Nothing to Forward**\n\n"
            f"No content found in that range.\n"
            f"Fetched `{len(fetched_messages)}`/`{total_range}` posts, "
            f"skipped `{skipped_meta + meta_failed}`.")
        return

    batch_start = time()
    # Don't hardcode the initial text — let _build_ui_text render it
    # so every subsequent edit is guaranteed to differ (elapsed time changes)
    await loading_msg.edit(
        f"⚡ **Batch Download** — `{total_tasks}` posts\n"
        f"_Preparing workers…_")

    # ── Phase 2: State manager + workers ─────────────────
    kill_event = get_kill_event(chat_id)
    kill_event.clear()
    skip_event = get_skip_event(chat_id)
    skip_event.clear()
    refresh_event = get_refresh_event(chat_id)
    refresh_event.clear()

    state_mgr = BatchStateManager(
        total=total_tasks, chat_id=chat_id, reply_to_id=reply_to_id,
        status_message=loading_msg, batch_start=batch_start,
        refresh_event=refresh_event)
    state_mgr.register_slots(
        list(range(total_tasks)),
        [f"{prefix}/{(m[0].id if isinstance(m, list) else m.id)}"
         for m in ordered_tasks])
    state_mgr.start()

    # Stop any previous batch's state manager before registering the new one,
    # to prevent two managers writing to the same chat_id concurrently.
    old_mgr = _batch_registry.get(chat_id)
    if old_mgr:
        try:
            await old_mgr.stop()
        except Exception as e:
            LOGGER(__name__).warning(f"Failed to stop old BatchStateManager: {e}")

    # Register for /retry
    _batch_registry[chat_id] = state_mgr

    worker_sem  = asyncio.Semaphore(PyroConf.MAX_FETCH_WORKERS)
    loop        = asyncio.get_event_loop()
    slot_futures: List[asyncio.Future] = [
        loop.create_future() for _ in ordered_tasks]

    worker_tasks = []
    for i, msg in enumerate(ordered_tasks):
        wt = track_task(_download_worker(
            index=i, chat_message=msg,
            reply_to_id=reply_to_id,
            slot_future=slot_futures[i],
            state_mgr=state_mgr,
            worker_sem=worker_sem,
            skip_event=skip_event,
            kill_event=kill_event,
        ))
        worker_tasks.append(wt)

    # Register worker tasks for /killall
    _batch_worker_tasks[chat_id] = worker_tasks

    # Reply chain: source msg_id → our group msg_id
    src_to_dst_map: Dict[int, int] = {}

    # ── Phase 3: Sender (normal or sonic mode) ───────────
    _use_sonic  = get_setting(chat_id, "sonic_download")
    _use_strict = get_setting(chat_id, "sonic_strict")
    if _use_sonic and _use_strict:
        _mode_label = "⚡🔢 SONIC-STRICT"
        sender_fn = _strict_sonic_sender
    elif _use_sonic:
        _mode_label = "⚡ SONIC-LOOSE"
        sender_fn = _sonic_sender
    else:
        _mode_label = "🔢 ORDERED"
        sender_fn = _ordered_sender
    LOGGER(__name__).info(f"[Batch] Sender mode: {_mode_label}")
    results = await sender_fn(
        futures=slot_futures,
        chat_id=chat_id,
        state_mgr=state_mgr,
        src_to_dst_map=src_to_dst_map,
        skip_event=skip_event,
        kill_event=kill_event,
    )

    # Cancel any still-running workers
    for t in worker_tasks:
        if not t.done():
            t.cancel()
    if worker_tasks:
        await asyncio.wait(worker_tasks, timeout=10)

    await state_mgr.stop()

    # Delete status message (will send final summary instead)
    try:
        await state_mgr.status_message.delete()
    except Exception:
        pass

    sent    = results["sent"]
    failed  = results["failed"]
    skipped = results["skipped"] + skipped_meta + meta_failed
    elapsed = fmt_time(time() - batch_start)
    retryable = len(state_mgr.get_retryable())

    counts   = state_mgr.summary()
    n_partial = counts[TaskStatus.PARTIAL]
    summary = (
        f"{'🛑 Batch Stopped' if kill_event.is_set() else '✅ Batch Complete!'}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Sent:**      `{sent}` post(s)\n"
        f"⚠️ **Partial:**   `{n_partial}` (sent with warning)\n"
        f"🔲 **Abandoned:** `{retryable}` (placeholder sent)\n"
        f"⏭️ **Skipped:**   `{skipped}`\n"
        f"⏱ **Time:**      `{elapsed}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ **Workers:**   `{PyroConf.MAX_FETCH_WORKERS}` parallel"
        + (f"\n\n_💡 {retryable} slot(s) can be retried — send /retry_"
           if retryable > 0 else ""))
    await bot.send_message(chat_id, summary)

# ════════════════════════════════════════════════════════
# /retry COMMAND
# Re-attempts failed/abandoned/partial slots from the last
# batch, editing their placeholder messages with results.
# ════════════════════════════════════════════════════════
async def handle_retry(chat_id: int, reply_to_id: int):
    state_mgr = _batch_registry.get(chat_id)
    if not state_mgr:
        await bot.send_message(
            chat_id,
            "❌ **No batch found to retry.**\n\n"
            "Run a `/bdl` batch first.",
            reply_to_message_id=reply_to_id)
        return

    retryable = state_mgr.get_retryable()
    if not retryable:
        await bot.send_message(
            chat_id,
            "✅ **Nothing to retry** — all slots succeeded!",
            reply_to_message_id=reply_to_id)
        return

    status_msg = await bot.send_message(
        chat_id,
        f"🔁 **Retrying `{len(retryable)}` slot(s)…**\n\n"
        f"Slots: {', '.join(f'#{ts.index + 1}' for ts in retryable)}",
        reply_to_message_id=reply_to_id)

    fixed = still_failed = 0

    for slot_num, ts in enumerate(retryable, 1):
        def _retry_status(extra=""):
            return (
                f"🔁 **Retry** `{slot_num}`/`{len(retryable)}`  "
                f"✅`{fixed}` ❌`{still_failed}`\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                f"🔗 `{ts.url}`\n"
                + (extra if extra else "_Fetching…_"))

        try:
            await status_msg.edit(_retry_status())
        except Exception:
            pass

        src_msg = None
        try:
            _cid, _mid = getChatMsgID(ts.url)
            src_msg = await user.get_messages(chat_id=_cid, message_ids=_mid)
            LOGGER(__name__).info(f"[Retry slot {ts.index}] Fetched: {ts.url}")
        except Exception as e:
            LOGGER(__name__).error(f"[Retry slot {ts.index}] Fetch failed: {e}")
            still_failed += 1
            continue

        if not src_msg:
            still_failed += 1
            continue

        if not has_downloadable_media(src_msg):
            try:
                await status_msg.edit(_retry_status("📝 _Forwarding text/non-media…_"))
            except Exception:
                pass
            try:
                sent_msg = await forward_message(src_msg=src_msg, chat_id=chat_id)
                if ts.placeholder_msg_id and sent_msg:
                    try:
                        link = f"https://t.me/c/{str(chat_id).lstrip('-100')}/{sent_msg.id}"
                        await bot.edit_message_text(
                            chat_id, ts.placeholder_msg_id,
                            f"✅ **Fixed!** — Post #{ts.index + 1}\n"
                            f"_Text forwarded._\n"
                            f"[Jump to message]({link})",
                            disable_web_page_preview=True)
                    except Exception:
                        pass
                ts.status = TaskStatus.COMPLETED
                fixed += 1
            except Exception as e:
                LOGGER(__name__).error(f"[Retry slot {ts.index}] Non-media forward failed: {e}")
                still_failed += 1
            continue

        filename      = get_file_name(src_msg.id, src_msg)
        download_path = get_download_path(state_mgr.reply_to_id, filename)
        fname_short   = short_name(filename)
        expected_size = get_expected_file_size(src_msg) or 0
        dl_start      = time()
        last_retry_edit = [0.0]

        async def _retry_dl_progress(current, total,
                                     _sn=slot_num, _fn=fname_short,
                                     _exp=expected_size):
            now = time()
            if now - last_retry_edit[0] < PyroConf.DOWNLOAD_PROGRESS_INTERVAL:
                return
            last_retry_edit[0] = now
            elapsed = now - dl_start
            speed   = current / elapsed if elapsed > 0 else 0
            actual_total = total if total and total > 0 else _exp
            try:
                await status_msg.edit(
                    f"🔁 **Retry** `{_sn}`/`{len(retryable)}`  "
                    f"✅`{fixed}` ❌`{still_failed}`\n"
                    "━" * 19 + "\n"
                    + build_progress_text("download", current, actual_total,
                                          speed, elapsed, _fn))
            except Exception:
                pass

        LOGGER(__name__).info(f"[Retry slot {ts.index}] ⬇️  {ts.url}  {fname_short}")
        media_path, err, is_partial = await download_single_message(
            src_msg, download_path,
            progress_message=_retry_dl_progress,
            start_time=dl_start,
            _src_chat_id=getattr(getattr(src_msg, "chat", None), "id", None))

        if not media_path:
            still_failed += 1
            LOGGER(__name__).error(f"[Retry slot {ts.index}] Download failed: {err}")
            try:
                await status_msg.edit(
                    _retry_status(f"❌ `{fname_short}` — {err}\n_Moving to next…_"))
            except Exception:
                pass
            continue

        cap = ""
        try:
            cap = await get_parsed_msg(src_msg.caption or "", src_msg.caption_entities)
        except Exception:
            cap = src_msg.caption or ""

        if is_partial:
            actual   = os.path.getsize(media_path)
            expected = get_expected_file_size(src_msg) or 0
            cap = f"⚠️ **Still Partial** {fmt_size(actual)}/{fmt_size(expected)}\n" + cap

        media_type = (
            "photo" if src_msg.photo else
            "video" if src_msg.video else
            "audio" if src_msg.audio else "document")

        try:
            await status_msg.edit(_retry_status(f"📤 `{fname_short}` — _uploading…_"))
        except Exception:
            pass

        try:
            replaced_in_place = False
            sent_msg = None

            if ts.placeholder_msg_id:
                # ── Primary path: replace placeholder in-place ──────────────
                # user.edit_message_media converts the text placeholder into the
                # actual media at exactly the same chat position.
                replaced_in_place = await edit_placeholder_with_media(
                    chat_id=chat_id,
                    placeholder_msg_id=ts.placeholder_msg_id,
                    media_path=media_path,
                    media_type=media_type,
                    caption=cap,
                    filename=filename,
                )

            if replaced_in_place:
                # In-place succeeded — no new message needed.
                # Update the placeholder message to reflect partial status if needed.
                if is_partial:
                    try:
                        # The media is now there but partial — append warning to caption
                        # via a second edit_message_media with updated caption
                        media_cls_map = {
                            "photo": InputMediaPhoto, "video": InputMediaVideo,
                            "audio": InputMediaAudio, "document": InputMediaDocument,
                        }
                        mc = media_cls_map.get(media_type, InputMediaDocument)
                        await user.edit_message_media(
                            chat_id=chat_id,
                            message_id=ts.placeholder_msg_id,
                            media=mc(media=media_path, caption=cap),
                        )
                    except Exception:
                        pass
                cleanup_download(media_path)
                LOGGER(__name__).info(
                    f"[Retry slot {ts.index}] ✅ Replaced placeholder in-place: {fname_short}")
            else:
                # ── Fallback path: send new message + edit placeholder text ──
                # edit_message_media failed (no placeholder, permission error, etc.)
                # Send the file as a new message and update the placeholder to link to it.
                sent_msg = await send_media_with_progress(
                    bot, chat_id, media_path, media_type, cap, filename,
                    batch_mode=True)
                cleanup_download(media_path)
                LOGGER(__name__).info(
                    f"[Retry slot {ts.index}] ✅ Sent (fallback): {fname_short}")

                if ts.placeholder_msg_id and sent_msg:
                    try:
                        link = f"https://t.me/c/{str(chat_id).lstrip('-100')}/{sent_msg.id}"
                        icon  = "⚠️" if is_partial else "✅"
                        label = "Still Partial" if is_partial else "Fixed!"
                        await bot.edit_message_text(
                            chat_id, ts.placeholder_msg_id,
                            f"{icon} **{label}** — Post #{ts.index + 1}\n"
                            f"[Jump to message ↑]({link})",
                            disable_web_page_preview=True)
                    except Exception as edit_err:
                        LOGGER(__name__).debug(f"Placeholder text edit failed: {edit_err}")

            ts.status = TaskStatus.COMPLETED if not is_partial else TaskStatus.PARTIAL
            if hasattr(ts, "sent_msg_id") and sent_msg:
                ts.sent_msg_id = sent_msg.id
            fixed += 1
        except Exception as e:
            still_failed += 1
            cleanup_download(media_path)
            LOGGER(__name__).error(f"[Retry slot {ts.index}] Send failed: {e}")

# FASTAPI LIFESPAN
# ════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    global download_semaphore
    download_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_DOWNLOADS)
    LOGGER(__name__).info("Starting Pyrogram clients…")
    await user.start()
    await bot.start()
    LOGGER(__name__).info(
        f"Bot ready — workers: fetch={PyroConf.MAX_FETCH_WORKERS} "
        f"dl={PyroConf.MAX_CONCURRENT_DOWNLOADS}")
    yield
    LOGGER(__name__).info("Shutting down — cancelling tasks…")
    if RUNNING_TASKS:
        for t in list(RUNNING_TASKS):
            t.cancel()
        await asyncio.wait(list(RUNNING_TASKS), timeout=15)
    await bot.stop()
    await user.stop()
    LOGGER(__name__).info("Shutdown complete.")

app = FastAPI(lifespan=lifespan)

# ════════════════════════════════════════════════════════
# BATCH ACTIVE GUARD
# ════════════════════════════════════════════════════════
def _is_batch_active(chat_id: int) -> bool:
    """Return True if a batch is currently running for this chat_id."""
    mgr = _batch_registry.get(chat_id)
    if mgr is None:
        return False
    # _stop_event is set only after handle_batch_download fully completes
    return not mgr._stop_event.is_set()

_BUSY_MSG = (
    "⚙️ **Batch in progress.**\n\n"
    "A `/bdl` batch is currently running.\n"
    "Use `/skip` to skip the current file, "
    "`/killall` to stop everything, "
    "or wait for it to finish."
)

# ════════════════════════════════════════════════════════
# WEBHOOK
# ════════════════════════════════════════════════════════
@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    LOGGER(__name__).debug(f"Update: {update}")

    # ── Inline button callback (Refresh button on status message) ──────────
    if "callback_query" in update:
        cq      = update["callback_query"]
        cq_id   = cq["id"]
        cq_data = cq.get("data", "")
        cq_chat = cq["message"]["chat"]["id"]
        cq_msg_id = cq["message"]["message_id"]

        # ── Settings toggle ────────────────────────────────────────────────
        if cq_data.startswith(SETTING_CB_PREFIX):
            # Format: setting:<key>:<0|1>
            parts = cq_data[len(SETTING_CB_PREFIX):].rsplit(":", 1)
            if len(parts) == 2:
                key, raw_val = parts
                new_val = bool(int(raw_val))
                set_setting(cq_chat, key, new_val)
                label  = _SETTING_LABELS.get(key, key)
                status = "✅ On" if new_val else "❌ Off"

                # Update the settings menu immediately so the toggle reflects
                try:
                    await bot.edit_message_text(
                        chat_id=cq_chat,
                        message_id=cq_msg_id,
                        text=_settings_text(cq_chat),
                        reply_markup=_settings_keyboard(cq_chat))
                except Exception as e:
                    LOGGER(__name__).debug(f"Settings menu update failed: {e}")

                # For status_always_bottom: demonstrate the effect instantly.
                # If turned ON  → move status to bottom right now so user sees it snap down.
                # If turned OFF → edit status in-place so user sees it stop jumping.
                if key == "status_always_bottom":
                    state_mgr = _batch_registry.get(cq_chat)
                    if state_mgr and state_mgr.status_message:
                        try:
                            await state_mgr.move_status_to_bottom()
                        except Exception as e:
                            LOGGER(__name__).debug(f"Instant status demo failed: {e}")

                try:
                    await bot.answer_callback_query(
                        cq_id, f"{label}: {status}")
                except Exception:
                    pass

            return JSONResponse({"status": "ok"})

        # ── Settings close ─────────────────────────────────────────────────
        if cq_data == "settings_close":
            try:
                await bot.delete_messages(cq_chat, [cq_msg_id])
            except Exception:
                pass
            try:
                await bot.answer_callback_query(cq_id, "Settings closed.")
            except Exception:
                pass
            return JSONResponse({"status": "ok"})

        if cq_data == REFRESH_CB:
            state_mgr = _batch_registry.get(cq_chat)
            if state_mgr and state_mgr.status_message:
                try:
                    await bot.edit_message_text(
                        chat_id=cq_chat,
                        message_id=state_mgr.status_message.id,
                        text=state_mgr._build_ui_text(),
                        reply_markup=state_mgr._refresh_markup())
                    await bot.answer_callback_query(cq_id, "✅ Refreshed!")
                except Exception as e:
                    LOGGER(__name__).debug(f"Refresh callback failed: {e}")
                    try:
                        await bot.answer_callback_query(cq_id, "⚠️ Could not refresh")
                    except Exception:
                        pass
            else:
                try:
                    await bot.answer_callback_query(cq_id, "No active batch found.")
                except Exception:
                    pass
        return JSONResponse({"status": "ok"})

    if "message" not in update:
        return JSONResponse({"status": "ok"})

    msg     = update["message"]
    chat_id = msg["chat"]["id"]
    text    = msg.get("text", "").strip()
    msg_id  = msg["message_id"]

    if text.startswith("/start"):
        return JSONResponse(handle_start(chat_id))
    if text.startswith("/help"):
        return JSONResponse(handle_help(chat_id))
    if text.startswith("/stats"):
        return JSONResponse(handle_stats(chat_id))
    if text.startswith("/killall"):
        return JSONResponse(handle_killall(chat_id))
    if text.startswith("/skip"):
        return JSONResponse(handle_skip(chat_id))
    if text.startswith("/cleanup"):
        return JSONResponse(handle_cleanup(chat_id))
    if text.startswith("/settings"):
        return JSONResponse(handle_settings(chat_id))
    if text.startswith("/refresh"):
        return JSONResponse(handle_refresh(chat_id))

    if text.startswith("/report"):
        track_task(handle_report(chat_id, msg_id))
        return JSONResponse({"status": "ok"})

    if text.startswith("/retry"):
        track_task(handle_retry(chat_id, msg_id))
        return JSONResponse({"status": "ok"})

    if text.startswith("/logs"):
        async def _send_logs():
            if os.path.exists("logs.txt"):
                await bot.send_document(
                    chat_id, "logs.txt", caption="**Logs**",
                    reply_to_message_id=msg_id)
            else:
                await bot.send_message(
                    chat_id, "**No log file found.**",
                    reply_to_message_id=msg_id)
        track_task(_send_logs())
        return JSONResponse({"status": "ok"})

    if text.startswith("/dl"):
        parts = text.split(None, 1)
        if len(parts) < 2:
            return JSONResponse(
                _msg(chat_id, "**Provide a post URL after the /dl command.**"))
        if _is_batch_active(chat_id):
            return JSONResponse(_msg(chat_id, _BUSY_MSG))
        track_task(handle_download(chat_id, msg_id, parts[1].strip()))
        return JSONResponse({"status": "ok"})

    if text.startswith("/bdl"):
        parts = text.split()
        if (len(parts) != 3
                or not all(p.startswith("https://t.me/") for p in parts[1:])):
            return JSONResponse(_msg(chat_id,
                "🚀 **Batch Copy**\n`/bdl start_link end_link`\n\n"
                "Copies everything: media, text, polls, stickers.\n"
                "Reply chains are preserved.\n\n"
                "💡 Example:\n"
                "`/bdl https://t.me/mychannel/100 https://t.me/mychannel/120`"))
        if _is_batch_active(chat_id):
            return JSONResponse(_msg(chat_id, _BUSY_MSG))
        track_task(handle_batch_download(chat_id, msg_id, parts[1], parts[2]))
        return JSONResponse({"status": "ok"})

    if text and not text.startswith("/"):
        if _is_batch_active(chat_id):
            # Silently ignore random chat messages during a batch — don't
            # reply to every message the user sends, just discard non-commands.
            return JSONResponse({"status": "ok"})
        track_task(handle_download(chat_id, msg_id, text))
        return JSONResponse({"status": "ok"})

    return JSONResponse({"status": "ok"})