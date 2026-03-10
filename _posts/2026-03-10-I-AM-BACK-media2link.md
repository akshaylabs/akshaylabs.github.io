---
layout: post
title: "Link Generator Bot — Telegram File-to-URL Service"
date: 2026-03-10
description: "A lightweight Telegram bot that accepts any file and returns a permanent public download link. Built with Pyrogram, FastAPI, and deployable on HuggingFace Spaces via Docker."
tags: [telegram, bot, python, fastapi, pyrogram, docker, huggingface]
---

# 🔗 Link Generator Bot

> A lightweight Telegram bot that accepts **any file** — documents, photos, videos, audio, stickers — and instantly returns a **permanent public download link**.

Built with [Pyrogram](https://docs.pyrogram.org/) + [FastAPI](https://fastapi.tiangolo.com/), designed to run on **HuggingFace Spaces** (or any Docker host).

---

## ✨ Features

- 📎 **Supports all Telegram media types** — documents, photos, videos, audio, voice, stickers, animations, video notes
- 🔗 **Permanent shareable links** — anyone with the link can download the file
- 📊 **Live progress updates** — real-time download/upload progress bar right in the chat
- ⚙️ **Bot stats command** — disk usage, memory, CPU, active jobs at a glance
- ⏱ **Configurable file expiry** — set TTL in minutes or keep files forever (`0`)
- 🚦 **FloodWait handling** — gracefully retries on Telegram rate limits
- 🐳 **Docker-ready** — single `docker run` deployment, HuggingFace Spaces compatible

---

## 🗂 Project Structure

```
.
├── main.py           # FastAPI app + Pyrogram bot + webhook handler
├── fileManager.py    # File storage, expiry tracking, URL generation
├── requirements.txt  # Python dependencies
├── config.env        # Environment variable defaults
└── Dockerfile        # Container definition
```

---

## ⚙️ How It Works

### Architecture Overview

```
Telegram ──webhook──▶ FastAPI /webhook
                            │
                     parse update
                            │
              ┌─────────────┴─────────────┐
              │ command?                  │ file/media?
              ▼                           ▼
       /start /help /stats        handle_media()
              │                           │
         instant reply            Pyrogram download
                                          │
                                   FileManager.save_file()
                                          │
                                   return /download/{id}
```

1. Telegram sends webhook updates to `POST /webhook`.
2. Text commands (`/start`, `/help`, `/stats`) are answered instantly via webhook response — no extra round-trip.
3. File messages are processed asynchronously: Pyrogram downloads the file, `FileManager` stores it locally and registers it with a UUID, and the bot edits its status message with the final link.

### `fileManager.py`

`FileManager` handles all file lifecycle concerns:

| Method | Description |
|---|---|
| `save_file(data, filename)` | Writes bytes to `fl/` directory, registers entry with expiry |
| `get_entry_by_id(uid)` | Looks up a file by UUID, revalidates its TTL on access |
| `check_files()` | Background sweep — deletes expired files from disk and registry |
| `get_url(entry)` | Builds the public download URL for a stored file |

File metadata is persisted to `collector_data.json` so it survives process restarts.

A background thread (via the `@syncmethod` decorator) calls `check_files()` on a configurable interval to prune expired files automatically.

### `main.py`

The FastAPI application exposes three endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Health check |
| `/download/{file_id}` | GET | Serve a stored file by UUID |
| `/webhook` | POST | Receive Telegram updates |

The Pyrogram client runs in the **same process** as FastAPI using the `lifespan` context manager — no separate worker process needed.

Progress updates are throttled to **one edit per 2.5 seconds** to avoid Telegram edit rate limits.

---

## 🚀 Deployment

### Prerequisites

- Telegram Bot Token — get one from [@BotFather](https://t.me/BotFather)
- Telegram API credentials — from [my.telegram.org](https://my.telegram.org)
- A public HTTPS URL for the webhook (HuggingFace Spaces provides this automatically)

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token |
| `API_ID` | ✅ | Telegram API ID (integer) |
| `API_HASH` | ✅ | Telegram API hash |
| `SPACE_URL` | ✅ | Public base URL of your deployment (e.g. `https://yourspace.hf.space`) |
| `FILE_EXPIRE_MINUTES` | ❌ | Minutes before files are deleted. `0` = keep forever (default: `0`) |

Create a `config.env.local` file (never commit this) with your secrets:

```env
BOT_TOKEN=your_bot_token_here
API_ID=12345678
API_HASH=your_api_hash_here
SPACE_URL=https://yourname-link-gen.hf.space
FILE_EXPIRE_MINUTES=0
```

### Docker

```bash
# Build
docker build -t link-gen-bot .

# Run
docker run -d \
  --env-file config.env.local \
  -p 7860:7860 \
  link-gen-bot
```

### HuggingFace Spaces

1. Create a new Space with the **Docker** SDK.
2. Push all project files.
3. Add your secrets in **Settings → Repository Secrets**.
4. Set your webhook:
   ```
   https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<your-space>.hf.space/webhook
   ```
5. The Space will build and start automatically.

---

## 💬 Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message with usage info |
| `/help` | Full help with supported file types |
| `/stats` | Live server stats (disk, RAM, CPU, stored files) |

Any file sent without a command is automatically processed.

---

## 📦 Dependencies

```
pyrofork        # Pyrogram fork with extended support
tgcrypto        # C extension for faster Telegram crypto
python-dotenv   # .env file loading
fastapi         # Async web framework
uvicorn         # ASGI server
psutil          # System stats for /stats command
```

---

## 📁 Download Source

Get all project files as a single archive:

➡️ **[⬇️ Download link-generator-bot.zip](./img/link-generator-bot.zip)**

Includes: `main.py`, `fileManager.py`, `Dockerfile`, `config.env`, `requirements.txt`

---

## 📄 License

MIT — use freely, attribution appreciated.
