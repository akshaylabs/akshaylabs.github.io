---

layout: post
title: "Telegram Media Downloader Bot — FastAPI + Pyrogram Webhook"
date: 2026-03-10
tags: [telegram, bot, pyrogram, fastapi, docker, webhook]
---------------------------------------------------------

# 🚀 Telegram Media Downloader Bot

A high‑performance **Telegram webhook bot** built with **FastAPI** and **Pyrogram** that can download and resend media from Telegram posts — including photos, videos, audio, documents, and media groups.

Designed for **Docker deployments** (including serverless containers), this project runs as a single app process with webhook support — no polling required.

---

## ✨ Features

* 📥 Download media from any Telegram post link
* 🖼 Supports photos, videos, audio, documents, stickers, animations
* 🧩 Handles media groups (albums)
* 🔁 Batch download post ranges
* ⚡ Webhook-based (fast & scalable)
* 🐳 Docker-ready deployment
* 🧹 Temp file auto-cleanup
* 📊 Live bot system stats
* 🧵 Concurrent task management
* 📝 Rotating log files

---

## 🏗 Tech Stack

* **Backend:** FastAPI
* **Telegram Client:** Pyrogram (Bot + User session)
* **Server:** Uvicorn
* **Container:** Docker
* **Utilities:** python-dotenv, psutil

---

## 📁 Project Structure

```
.
├── main.py            # Single-file webhook bot
├── config.env         # Environment configuration
├── requirements.txt   # Python dependencies
├── Dockerfile         # Container build instructions
└── downloads/         # Temporary media storage (auto-created)
```

---

## ⚙️ Environment Configuration

Create a file named **`config.env`** and fill in your Telegram credentials:

```
API_ID=
API_HASH=
BOT_TOKEN=
SESSION_STRING=
WEBHOOK_SECRET=

MAX_CONCURRENT_DOWNLOADS=1
BATCH_SIZE=1
FLOOD_WAIT_DELAY=10
```

### 🔑 Where to get credentials

* **API_ID / API_HASH:** [https://my.telegram.org](https://my.telegram.org)
* **BOT_TOKEN:** Create bot via @BotFather
* **SESSION_STRING:** Generate via Pyrogram tools or session generators

---

## ▶️ Run Locally

### 1️⃣ Install dependencies

```bash
pip install -r requirements.txt
```

### 2️⃣ Start server

```bash
uvicorn main:app --host 0.0.0.0 --port 7860
```

### 3️⃣ Set Telegram Webhook

```bash
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=<YOUR_SERVER_URL>/webhook
```

---

## 🐳 Docker Deployment

### Build Image

```bash
docker build -t tg-media-bot .
```

### Run Container

```bash
docker run -d -p 7860:7860 --env-file config.env tg-media-bot
```

---

## 🤖 Bot Commands

| Command              | Description          |
| -------------------- | -------------------- |
| `/start`             | Welcome message      |
| `/help`              | Show usage guide     |
| `/dl <url>`          | Download single post |
| `/bdl <start> <end>` | Batch download range |
| `/stats`             | System statistics    |
| `/cleanup`           | Remove temp files    |
| `/logs`              | Get log file         |
| `/killall`           | Cancel running tasks |

You can also **paste a Telegram post link directly** without using commands.

---

## 🔄 How It Works

1. Telegram sends updates to the webhook endpoint
2. FastAPI receives and parses commands
3. User session fetches original post
4. Media is downloaded temporarily
5. Bot account re-uploads media to user
6. Temp files are cleaned automatically

---

## 🧠 Concurrency & Limits

* Configurable parallel downloads
* FloodWait auto-handling
* Batch throttling delay
* Telegram file size checks

---

## 📝 Logs

* Rotating log file: `logs.txt`
* Auto-regenerated each run
* Downloadable via `/logs`

---

## 🧹 Cleanup

Temporary files are stored in:

```
downloads/<chat_id>/
```

Removed automatically after sending.
Manual cleanup available via `/cleanup`.

---

## 🔐 Requirements

* Bot must be admin (if private channel)
* User session must be member of target chat
* Valid Telegram post links

---

## 📦 Download Source Code

You can download all project files as a ZIP archive here:

👉 **[Download Project ZIP](./img/telegram-media-downloader-bot.zip)**

---

## ⭐ Support

If this project helped you, consider starring the repo and sharing it with others!
