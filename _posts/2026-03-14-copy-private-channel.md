---
layout: post
title: "TG Bulk Downloader — A High-Performance Telegram MTProto Bot"
date: 2026-03-14
author: Akshay
categories: [telegram, python, bot, automation]
tags: [pyrogram, fastapi, mtproto, telegram-bot, async, huggingface]
description: "A single-file, zero-dependency-conflict Telegram bot that bulk-copies media, text, polls, and stickers across channels using Pyrogram over MTProto — deployable on HuggingFace Spaces."
---

# 🤖 TG Bulk Downloader — `main_v4.py`

> **A production-grade, single-file Telegram bot** built on [Pyrogram](https://pyrogram.org/) + [FastAPI](https://fastapi.tiangolo.com/) that bulk-downloads and re-uploads entire Telegram channel ranges — preserving reply chains, handling FloodWait, and keeping you live-updated with a real-time status board.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://python.org)
[![Pyrogram](https://img.shields.io/badge/Pyrogram-MTProto-green)](https://pyrogram.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-Webhook-009688)](https://fastapi.tiangolo.com)
[![HuggingFace](https://img.shields.io/badge/HuggingFace-Spaces-yellow)](https://huggingface.co/spaces)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

---

## 📋 Table of Contents

1. [Why This Exists](#-why-this-exists)
2. [Architecture Overview](#-architecture-overview)
3. [Features](#-features)
4. [Prerequisites](#-prerequisites)
5. [Installation](#-installation)
6. [Environment Variables](#-environment-variables)
7. [Deployment](#-deployment)
8. [Bot Commands Reference](#-bot-commands-reference)
9. [Pipeline Deep-Dive](#-pipeline-deep-dive)
10. [Task Status Lifecycle](#-task-status-lifecycle)
11. [Live Status Board](#-live-status-board)
12. [Configuration Tuning](#-configuration-tuning)
13. [Fault Tolerance & Retry System](#-fault-tolerance--retry-system)
14. [User Settings](#-user-settings)
15. [Code Structure](#-code-structure)
16. [Logging](#-logging)
17. [Known Limitations](#-known-limitations)
18. [Download Source Files](#-download-source-files)

---

## 💡 Why This Exists

HuggingFace Spaces **blocks `api.telegram.org`** (the standard Bot HTTP API), which means typical webhook bots simply cannot run there. This bot solves that by using **Pyrogram over MTProto (raw TCP)** — a lower-level protocol that HF Spaces does not block.

It also solves a common problem: copying large ranges of posts from one Telegram channel/group to another, including media albums, polls, stickers, voice notes, and documents — without losing message order or reply chains.

---

## 🏗 Architecture Overview

```
User sends /bdl <start_url> <end_url>
           │
           ▼
┌─────────────────────────────────────────────────────┐
│                  FastAPI Webhook                     │
│  POST /webhook  ─►  route handler  ─►  task launch  │
└─────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Parallel Pipeline                              │
│                                                                  │
│  Phase 1 ─ FETCH   │  Phase 2 ─ DOWNLOAD  │  Phase 3 ─ UPLOAD  │
│  ─────────────────  │  ──────────────────  │  ─────────────────  │
│  Chunked metadata  │  asyncio.Semaphore   │  Ordered delivery  │
│  get_messages()    │  pool (N workers)    │  ready_queue +     │
│  batch=20 msgs     │  MAX_CONCURRENT_     │  send_pointer      │
│                    │  DOWNLOADS=3         │  MAX_UPLOAD_       │
│                    │                      │  WORKERS=2         │
│                                                                  │
│  Phase 4 ─ RETRY                                                 │
│  ────────────────                                                │
│  /retry re-runs failed/abandoned slots                           │
│  Edits placeholder messages in-place                             │
└──────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────┐
│              BatchStateManager                       │
│  Live Telegram UI — edits status every 5 seconds    │
│  delete+resend after each file (always-bottom mode) │
└─────────────────────────────────────────────────────┘
```

The bot uses **two Pyrogram clients simultaneously**:

| Client | Auth type | Role |
|--------|-----------|------|
| `user` | Session string (user account) | Reads source channel messages |
| `bot` | Bot token | Sends files to destination chat |

---

## ✨ Features

- **Forward EVERYTHING** — media (photos, videos, audio, documents), text, polls, stickers, voice notes, and forwarded messages
- **Preserve reply chains** — maps source `reply_to_msg_id` to our group's equivalent message
- **Ordered delivery** — files always arrive in the correct sequence, even with parallel downloads
- **Partial file fallback** — if download stalls, sends whatever bytes arrived with a `⚠️ Partial` warning caption
- **Abandoned placeholder** — on total failure, sends a text stub to hold sequence position (retryable)
- **`/retry` command** — re-attempts all `failed` / `abandoned` / `partial` slots and edits placeholders in-place
- **`/killall`** — hard-cancels all worker tasks for the active batch immediately
- **`/skip`** — skips the currently stalling download slot
- **Live status board** — edits a single Telegram message every `STATE_LOG_INTERVAL` seconds with real-time counters
- **Upload progress bar** — ephemeral `📤 Uploading...` message showing `%` completion, auto-deleted on finish
- **Always-bottom status** — status message auto-deleted and resent after each file so it stays newest
- **Inline Refresh button** — tap 🔄 on the status message for an instant redraw
- **`/settings` panel** — toggle bot behaviour per-chat via inline keyboard
- **FloodWait handling** — respects `FloodWait` and `FloodPremiumWait` errors with automatic backoff
- **Rotating log file** — `logs.txt` (5 MB, 10 backups), downloadable via `/logs`

---

## 📦 Prerequisites

- Python **3.10+**
- A Telegram **API ID** and **API Hash** from [my.telegram.org](https://my.telegram.org)
- A Telegram **Bot Token** from [@BotFather](https://t.me/BotFather)
- A Pyrogram **Session String** for a user account (generated via `pyrogram.Client`)
- Optionally: a HuggingFace Spaces Docker environment

---

## 🚀 Installation

### 1. Clone / download

```bash
git clone https://github.com/yourname/tg-bulk-downloader.git
cd tg-bulk-downloader
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

**`requirements.txt`:**

```
pyrofork
pyleaves
tgcrypto
python-dotenv
psutil
fastapi
uvicorn[standard]
```

| Package | Purpose |
|---------|---------|
| `pyrofork` | Pyrogram fork — MTProto Telegram client |
| `pyleaves` | Pyrogram leaves plugin system |
| `tgcrypto` | Native crypto acceleration for Pyrogram |
| `python-dotenv` | `.env` file loader for config |
| `psutil` | System stats for `/stats` command |
| `fastapi` | ASGI web framework for the webhook server |
| `uvicorn[standard]` | ASGI server (with uvloop + httptools) |

### 3. Configure environment

Create a `config.env` file (see [Environment Variables](#-environment-variables) below).

### 4. Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Or on HuggingFace Spaces (port 7860):

```bash
uvicorn main:app --host 0.0.0.0 --port 7860
```

---

## 🔑 Environment Variables

Create `config.env` or `config.env.local` in the project root:

```dotenv
# ── Required ──────────────────────────────────────────────────────────
BOT_TOKEN=123456789:AABBCCyour-bot-token-here
SESSION_STRING=BQAyour-pyrogram-session-string-here
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890

# ── Worker concurrency ─────────────────────────────────────────────────
MAX_FETCH_WORKERS=3           # parallel metadata fetch threads
MAX_CONCURRENT_DOWNLOADS=3   # parallel download slots
MAX_UPLOAD_WORKERS=2          # parallel Telegram upload slots

# ── Retry behaviour ────────────────────────────────────────────────────
MAX_DOWNLOAD_RETRIES=3        # per-slot download retry limit
RETRY_DELAY_SECONDS=5         # wait between retries

# ── Timeouts / safety ─────────────────────────────────────────────────
CONSECUTIVE_ERROR_LIMIT=5     # abort batch after N consecutive errors
FLOOD_WAIT_DELAY=3            # extra seconds added on FloodWait
FETCH_BATCH_SIZE=20           # messages fetched per get_messages() call

# ── UI timings ─────────────────────────────────────────────────────────
STATE_LOG_INTERVAL=5.0        # status board edit interval (seconds)
UPLOAD_PROGRESS_INTERVAL=3.0  # upload progress message edit interval
```

> **Tip:** Generate a session string with:
> ```python
> from pyrogram import Client
> with Client("my_account", api_id=API_ID, api_hash=API_HASH) as app:
>     print(app.export_session_string())
> ```

---

## ☁️ Deployment

### HuggingFace Spaces (Docker)

Add a `Dockerfile`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main_v4.py main.py
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
```

Set all environment variables as HF Secrets. The MTProto raw TCP connection used by Pyrogram is **not blocked** by HuggingFace, unlike the standard Bot API HTTP endpoint.

### Local / VPS

```bash
# systemd service or just:
nohup uvicorn main:app --host 0.0.0.0 --port 8000 &
```

### Webhook Setup

After the server is running, register the Telegram webhook:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://your-domain.com/webhook"
```

---

## 📟 Bot Commands Reference

| Command | Arguments | Description |
|---------|-----------|-------------|
| `/start` | — | Welcome message |
| `/help` | — | Full command reference |
| `/dl` | `<post_url>` | Download & re-upload a single post |
| `/bdl` | `<start_url> <end_url>` | Bulk copy a range of posts |
| `/retry` | — | Re-attempt all failed/abandoned/partial slots |
| `/skip` | — | Skip the currently stalling download |
| `/killall` | — | Hard-cancel all workers for the active batch |
| `/refresh` | — | Force-redraw the status board immediately |
| `/stats` | — | Show CPU, RAM, disk, uptime stats |
| `/logs` | — | Upload `logs.txt` as a document |
| `/cleanup` | — | Delete all temp files in `downloads/` |
| `/settings` | — | Open the per-chat settings panel |

### `/bdl` Example

```
/bdl https://t.me/mychannel/100 https://t.me/mychannel/120
```

This copies posts **#100 through #120** from `mychannel` to your chat.

---

## 🔬 Pipeline Deep-Dive

### Phase 1 — Metadata Fetch

Messages are fetched in chunks of `FETCH_BATCH_SIZE=20` using `get_messages()`. The fetch pool uses up to `MAX_FETCH_WORKERS=3` concurrent tasks, each handling a slice of the total ID range. This avoids a single serial fetch loop on large batches.

### Phase 2 — Parallel Download

Each fetched message is queued into a download pool protected by an `asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)`. Workers grab a permit, download the file to `downloads/<chat_id>/<filename>`, and upon completion place the result into a `ready_queue`.

Download progress is tracked via a Pyrogram progress callback, updating `bytes_done` on the slot's `TaskState`. The `BatchStateManager` reads these live values to render per-file progress bars in the status board.

### Phase 3 — Ordered Upload

A dedicated **sender task** monitors `ready_queue` and a `send_pointer` (the index of the next expected slot). Files are uploaded in strict sequential order: if slot #5 finishes before slot #3, slot #5 waits in the queue until the pointer reaches it.

Up to `MAX_UPLOAD_WORKERS=2` uploads run in parallel once ordering is satisfied. Telegram-native albums (grouped media) are batched into `send_media_group()` calls.

### Phase 4 — Retry

`/retry` iterates over all `FAILED`, `ABANDONED`, and `PARTIAL` slots. For each:
1. Re-downloads the source message
2. Attempts to re-upload the file
3. On success: edits the placeholder message in-place with the actual content
4. On failure: increments `still_failed` counter

---

## 🔄 Task Status Lifecycle

```
PENDING ──► FETCHING ──► DOWNLOADING ──► SENDING ──► UPLOADING ──► COMPLETED
                │                │                                    │
                │                └──────────────────────────────► PARTIAL
                │                                                    │
                └───────────────────────────────────────────────► ABANDONED
                                                                     │
                                                               (use /retry)
                                                                     │
                                                               ──► COMPLETED
                                                               ──► FAILED
```

| Status | Icon | Meaning |
|--------|------|---------|
| `PENDING` | ⏳ | Queued, not yet started |
| `FETCHING` | 🔍 | Fetching message metadata |
| `DOWNLOADING` | ⬇️ | Downloading file bytes |
| `SENDING` | 📦 | Downloaded, queued for upload slot |
| `UPLOADING` | 📤 | Actively uploading to Telegram |
| `COMPLETED` | ✅ | Successfully sent |
| `FAILED` | ❌ | All retries exhausted |
| `PARTIAL` | ⚠️ | Partial file sent with warning caption |
| `ABANDONED` | 🔲 | Placeholder sent; retry available |
| `SKIPPED` | ⏭️ | Skipped via `/skip` |

---

## 📊 Live Status Board

The bot maintains a single Telegram message that it edits every `STATE_LOG_INTERVAL` seconds. The board looks like:

```
⚡ Batch Download — 20 posts
▓▓▓▓▓▓░░░░ 60%  •  ✅12 ⚠️1 🔲0 ❌0 ⏭️0  •  2m 14s
⬇️2 dl  📤1 up  📦0 queued

Active:
⬇️ ▓▓▓▓░░ video_clip.mp4
    45.3 MB / 112.0 MB
📤 document.pdf 8.2 MB — uploading…

Recent:
✅ photo_album.jpg 2.1 MB
✅ voice_note.ogg 0.4 MB
✅ sticker.webp

⏳ 7 post(s) queued…

/skip — skip current  •  /killall — stop all  •  /refresh — refresh now
```

A **🔄 Refresh** inline button allows instant manual redraw.

---

## ⚙️ Configuration Tuning

### Throughput vs. Stability

| Goal | Adjust |
|------|--------|
| Faster bulk downloads | ↑ `MAX_CONCURRENT_DOWNLOADS` |
| Fewer FloodWait errors | ↓ `MAX_UPLOAD_WORKERS`, ↑ `FLOOD_WAIT_DELAY` |
| Reduce Telegram API calls | ↑ `FETCH_BATCH_SIZE` |
| More responsive status UI | ↓ `STATE_LOG_INTERVAL` |
| Reduce edit rate on busy batches | ↑ `STATE_LOG_INTERVAL` |

### Memory / Disk

Downloaded files are stored temporarily in `downloads/<chat_id>/`. Use `/cleanup` to purge them, or they are removed automatically after each successful upload. `psutil` is used by `/stats` to report live disk usage.

---

## 🛡 Fault Tolerance & Retry System

### FloodWait

The bot catches both `FloodWait` and `FloodPremiumWait` errors. On `FloodWait(x)`, it waits `x + FLOOD_WAIT_DELAY` seconds before retrying. This applies to both downloads and uploads.

### Consecutive Error Limit

If `CONSECUTIVE_ERROR_LIMIT` slots fail back-to-back, the batch is aborted to avoid hammering the API on a persistent issue.

### Partial File Fallback

If a download is interrupted mid-stream, the bot detects a truncated file and sends it anyway with a caption flagging the partial state. The slot is marked `PARTIAL` and is retryable.

### Abandoned Slots

When a file cannot be sent (e.g. content too large, unsupported type), the bot sends a plain-text placeholder message to hold the sequence position. The slot is marked `ABANDONED` and can be recovered via `/retry`.

---

## 🎛 User Settings

Open `/settings` to see and toggle per-chat options:

| Setting | Default | Description |
|---------|---------|-------------|
| 📌 Status bar always at bottom | **On** | After each file, the status message is deleted and re-sent so it stays as the newest message |

Settings are stored in-memory and reset on redeployment. Future versions may persist them.

---

## 🗂 Code Structure

Since this is intentionally a **single-file bot**, all components live in `main_v4.py`:

```
main_v4.py
├── Logging setup             (RotatingFileHandler, console)
├── PyroConf                  (all env var config in one place)
├── TaskStatus (Enum)         (slot state machine)
├── UserSettings              (per-chat preferences)
├── UX helpers                (fmt_size, fmt_time, progress_bar, ...)
├── TaskState (dataclass)     (per-slot mutable state)
├── BatchStateManager         (live Telegram UI, update queue)
├── Batch Registry            (_batch_registry, _batch_worker_tasks)
├── File helpers              (get_download_path, cleanup_download, ...)
├── Pyrogram clients          (user + bot)
├── Message handlers          (handle_download, handle_batch_download, ...)
│   ├── Single /dl handler
│   ├── Batch /bdl handler
│   │   ├── fetch_worker()
│   │   ├── download_worker()
│   │   ├── upload_worker()  (ordered sender)
│   │   └── status_logger()  (live UI loop)
│   └── /retry handler
├── Command routers           (/start, /help, /stats, /killall, /skip, ...)
├── FastAPI lifespan          (start/stop Pyrogram clients)
└── FastAPI webhook           (POST /webhook — routes all updates)
```

---

## 📝 Logging

Logs are written to both the console and `logs.txt` (5 MB rolling, 10 backups).

Format:
```
[DD-Mon-YY HH:MM:SS AM/PM - LEVEL] - function_name() - Line N: module - message
```

- Pyrogram's own logs are suppressed to `ERROR` level to reduce noise.
- Send `/logs` in Telegram to download the current log file as a document.
- Old `logs.txt` is deleted on each bot restart (clean slate).

---

## ⚠️ Known Limitations

- **Settings are in-memory** — toggled preferences reset on each redeploy.
- **No multi-batch support per chat** — starting a new `/bdl` while one is running cancels the old one.
- **Large files** — Telegram has a 2 GB upload limit for bots. Files exceeding this will be marked `ABANDONED`.
- **Private channels** — the user account (`SESSION_STRING`) must be a member of the source channel.
- **Album ordering** — grouped media (albums) are sent as a unit; individual photos within an album preserve their internal order.

---

## 📥 Download Source Files

You can download the full source package (includes `main_v4.py` and `requirements.txt`) below:

**[⬇️ Download tg_bulk_downloader.zip](./assets/img/tg_bulk_downloader.zip)**

---

## 🙏 Credits

- **Author:** [@TheSmartBisnu](https://t.me/itsSmartDev)
- **MTProto client:** [Pyrofork](https://github.com/Mayuri-Chan/pyrofork) (Pyrogram fork)
- **Web framework:** [FastAPI](https://fastapi.tiangolo.com/)

---

*Built with ❤️ for the Telegram automation community.*
