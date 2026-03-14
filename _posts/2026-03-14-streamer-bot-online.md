---
layout: post
title: "Advanced Stream Bot v3.3.0 — A Deep Dive"
date: 2026-03-14
categories: [python, streaming, telegram, fastapi, devops]
tags: [telegram-bot, ffmpeg, fastapi, rtmp, live-streaming, docker, pyav, sse]
description: "A comprehensive guide to the Advanced Stream Bot — a production-ready Telegram-controlled RTMP live streaming engine built with FastAPI, PyAV, and APScheduler."
author: "Akshay"
---

> **Advanced Stream Bot v3.3.0** — A production-ready, Telegram-controlled RTMP live streaming engine with real-time dashboards, playlist support, logo overlays, auto-reconnect, scheduled jobs, and Server-Sent Events. Zero UI required — just your Telegram chat.

---

## Table of Contents

- [Overview](#overview)
- [Architecture at a Glance](#architecture-at-a-glance)
- [Feature Highlights](#feature-highlights)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Configuration & Settings](#configuration--settings)
  - [Quality Presets](#quality-presets)
  - [Video Settings](#video-settings)
  - [Audio Settings](#audio-settings)
  - [Network & Reconnect](#network--reconnect)
  - [Logo Overlay](#logo-overlay)
- [API Endpoints](#api-endpoints)
- [Real-Time Systems](#real-time-systems)
  - [Live View (Telegram Bubble)](#live-view-telegram-bubble)
  - [Server-Sent Events (SSE)](#server-sent-events-sse)
  - [Outbound Message Queue](#outbound-message-queue)
- [Stream Lifecycle](#stream-lifecycle)
- [Scheduler & Automated Jobs](#scheduler--automated-jobs)
- [Session Management](#session-management)
- [Docker Deployment](#docker-deployment)
  - [Dockerfile Explained](#dockerfile-explained)
  - [Running on Hugging Face Spaces](#running-on-hugging-face-spaces)
  - [Running Locally](#running-locally)
- [Telegram Webhook Setup](#telegram-webhook-setup)
- [Changelog — v3.3.0](#changelog--v330)
- [Known Limitations](#known-limitations)
- [Download Source Files](#download-source-files)

---

## Overview

**Advanced Stream Bot** is a self-hosted, Telegram-bot-controlled live streaming service. It accepts one or more video/audio source URLs, transcodes them in real time using [PyAV](https://pyav.basswood-io.com/) (a Python binding for FFmpeg), and pushes the output to any RTMP-compatible destination — YouTube Live, Twitch, Facebook Live, custom media servers, and more.

The entire control plane lives inside a Telegram chat. Users configure resolution, codecs, bitrates, logo overlays, playlists, loop counts, and scheduled jobs through inline keyboards and guided conversation flows — no web admin panel needed. For advanced users and CI/CD pipelines, a full REST API and SSE dashboard are also exposed.

**Key strengths:**

- **Zero-client-side buffering** — webhook-only Telegram mode means no polling loop; responses are dispatched synchronously with the next webhook call.
- **Deterministic live-view updates** — a background thread rewrites a single Telegram message every 2 seconds with fresh stats and logs, capped at exactly one pending edit per bubble to prevent stale-edit accumulation.
- **Graceful failure recovery** — configurable reconnect attempts with delay, watchdog thread, and clean abort-during-reconnect semantics.
- **Extensible quality pipeline** — from `ultrafast` 360p at 800 kbps to `slow` 4K at 6 Mbps, with full manual override for every parameter.

---

## Architecture at a Glance

```
Telegram User
     │  (Webhook POST /webhook)
     ▼
┌──────────────────────────────────────────┐
│              FastAPI App                 │
│  ┌─────────────┐   ┌──────────────────┐  │
│  │  Webhook    │   │   REST Endpoints  │  │
│  │  Handler    │   │  /status /poll   │  │
│  │  (sync)     │   │  /sessions       │  │
│  └──────┬──────┘   └──────────────────┘  │
│         │                                │
│  ┌──────▼──────────────────────────────┐ │
│  │         Session Manager             │ │
│  │   user_sessions{chat_id → state}    │ │
│  └──────┬──────────────────────────────┘ │
│         │                                │
│  ┌──────▼──────────────────────────────┐ │
│  │        Stream Engine (Thread)        │ │
│  │  PyAV decode → transcode → encode   │ │
│  │  Logo overlay  │  Watchdog thread    │ │
│  └──────┬──────────────────────────────┘ │
│         │                                │
│  ┌──────▼──────────────────────────────┐ │
│  │   Live View Updater (Thread, 2s)     │ │
│  │   SSE Publisher  │  Outbound Queue   │ │
│  └──────┬──────────────────────────────┘ │
└─────────┼────────────────────────────────┘
          │
    RTMP Destination
    (YouTube / Twitch / etc.)
```

---

## Feature Highlights

### 🎬 Multi-Source Playlists
Build a queue of input URLs (HLS, RTMP, direct file URLs, local paths). The bot streams each source in order, looping as configured — once, N times, or infinitely.

### 📊 Real-Time Telegram Live View
A single Telegram message acts as a live dashboard — updating every 2 seconds with stream state, uptime, frames encoded, bytes sent, and the last 4–15 log lines. Users can **Pause**, **Resume**, **Refresh**, or **Close** the live view bubble at any time.

### 🌐 Server-Sent Events (SSE) Dashboard
Web clients connect to `GET /events/{chat_id}` for a persistent SSE stream delivering state changes, log lines, and heartbeats. A lighter `GET /stream-log/{chat_id}` endpoint provides log-only SSE.

### ⚙️ Granular Quality Control
Choose from four built-in quality presets or override every parameter individually:

| Preset | Resolution | Video Bitrate | Audio Bitrate | Encoder Preset |
|--------|-----------|---------------|---------------|----------------|
| low    | 854×480   | 800k          | 96k           | superfast      |
| medium | 1280×720  | 1500k         | 128k          | medium         |
| high   | 1920×1080 | 3000k         | 192k          | fast           |
| ultra  | 1920×1080 | 6000k         | 256k          | slow           |

### 🖼️ Logo / Watermark Overlay
Upload any PNG/JPEG; the bot composites it at configurable position, scale (as fraction of frame width), opacity, and margin — all in-process via Pillow, no separate FFmpeg filter chain.

### 🔁 Auto-Reconnect & Watchdog
On stream error: configurable delay, configurable max attempts, clean abort signal respected immediately. A watchdog thread tracks the last-frame timestamp and marks the stream as errored if no frames arrive within the read timeout window.

### 📅 APScheduler Integration
Schedule streams to start automatically at configured times. Scheduler-triggered streams enqueue Telegram notifications to the relevant user on the next webhook response.

### 🛠️ Force Reboot
`/reboot` command and "Force Reboot" inline button forcibly restart the bot process — last-resort recovery for stuck states without SSH access.

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Web framework | **FastAPI** | HTTP API, webhook handler, SSE endpoints |
| ASGI server | **Uvicorn** | Production async server |
| Media engine | **PyAV** (`av`) | FFmpeg bindings — decode, encode, mux |
| Image processing | **Pillow** | Logo overlay compositing |
| Scheduler | **APScheduler** | Background job scheduling |
| Telegram API | **python-telegram-bot** / **telebot** | Webhook response builders |
| HTTP client | **httpx** / **requests** | Outbound HTTP calls |
| Async I/O | **asyncio** | SSE queue management, main event loop |
| Containerisation | **Docker** | Reproducible deployment |

---

## Project Structure

```
.
├── app.py               # Main application — all logic in one file
├── requirements.txt     # Python dependencies
└── Dockerfile           # Container build instructions
```

The entire application is intentionally a single-file monolith (`app.py`, ~3200 lines). This makes it trivial to deploy to Hugging Face Spaces, Railway, Render, or any PaaS that expects a simple `app.py` entry point.

---

## Configuration & Settings

All user-configurable settings are stored in the `DEFAULT_USER_SETTINGS` dict and copied into each chat session on first contact. Settings persist in memory for the lifetime of the process.

### Quality Presets

Apply a whole profile in one command. After applying, individual fields can still be overridden:

```
/quality low | medium | high | ultra | source
```

### Video Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `video_codec` | `libx264` | Encoder. Supports `libx264`, `h264_nvenc`, `h264_qsv`, `libx265`, `hevc_nvenc`, `hevc_qsv`, `copy` |
| `resolution` | `1280x720` | Output WxH. Use `source` to pass through original dimensions |
| `fps` | `30` | Output frame rate |
| `gop_size` | `60` | Keyframe interval (frames) |
| `video_bitrate` | `1500k` | Target video bitrate |
| `ffmpeg_preset` | `medium` | Encoder speed/quality tradeoff |
| `video_pix_fmt` | `yuv420p` | Pixel format (required by most streaming platforms) |
| `video_thread_count` | `0` | Encoder threads. `0` = auto |

### Audio Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `audio_codec` | `aac` | Encoder. Supports `aac`, `opus`, `libmp3lame`, `copy` |
| `audio_bitrate` | `128k` | Target audio bitrate |
| `audio_sample_rate` | `44100` | Sample rate in Hz. `0` = pass through source rate |
| `audio_channels` | `2` | Channel count. `0` = pass through source count |

### Network & Reconnect

| Setting | Default | Description |
|---------|---------|-------------|
| `open_timeout_seconds` | `15` | Max time to open the input container |
| `read_timeout_seconds` | `30` | Max time between frames before watchdog triggers |
| `reconnect_on_stream_error` | `True` | Auto-reconnect on stream error |
| `reconnect_delay_seconds` | `5` | Wait between reconnect attempts |
| `max_reconnect_attempts` | `3` | Maximum reconnect attempts before giving up |

### Logo Overlay

| Setting | Default | Description |
|---------|---------|-------------|
| `logo_enabled` | `False` | Enable/disable overlay |
| `logo_position` | `top_right` | One of `top_left`, `top_right`, `bottom_left`, `bottom_right`, `center` |
| `logo_scale` | `0.10` | Logo width as fraction of frame width (10%) |
| `logo_opacity` | `0.85` | Alpha value (0.0–1.0) |
| `logo_margin_px` | `10` | Pixel margin from the nearest edges |

---

## API Endpoints

The FastAPI app exposes the following endpoints:

### `POST /webhook`
Main Telegram webhook receiver. Processes incoming messages, commands, and callback queries. Returns a single JSON response containing the next Telegram Bot API method to execute (sendMessage, editMessageText, answerCallbackQuery, etc.).

### `GET /status/{chat_id}`
Returns a full JSON snapshot of the session state for a given chat — suitable for external dashboard polling.

```json
{
  "chat_id": 123456789,
  "state": "streaming",
  "state_icon": "🟢",
  "frames_encoded": 18420,
  "bytes_sent": 41943040,
  "uptime_str": "00:10:14",
  "reconnect_attempt": 0,
  "active_output_url": "rtmp://a.rtmp.youtube.com/live2/...",
  "playlist_index": 0,
  "playlist_count": 3
}
```

### `GET /poll/{chat_id}`
Lightweight polling endpoint. Returns the pending `editMessageText` payload (if any), then clears the queue. Designed for a Telegram Mini App or web client to call every 2 seconds and forward the edit directly to the Telegram API — enabling real-time live-view updates without waiting for an incoming webhook.

### `GET /events/{chat_id}`
Full SSE stream. Delivers `state_change`, `log`, `log_batch`, and `heartbeat` events.

### `GET /stream-log/{chat_id}`
Log-only SSE stream. Delivers only `log` and `log_batch` events — lighter than `/events`.

### `POST /notify/{chat_id}`
Internal endpoint to push a JSON event to SSE subscribers and (optionally) queue a Telegram message. Body: any JSON. If a `"text"` field is present, it is also queued as a `sendMessage`.

### `GET /sessions`
Lists all active sessions with their current state, frame counts, SSE subscriber counts, and queued message counts.

### `GET /health`
Health check endpoint. Returns version, session count, scheduler job count, SSE connection count, and queued message count.

### `GET /`
Root info endpoint. Returns all available endpoint URLs and current runtime summary.

---

## Real-Time Systems

### Live View (Telegram Bubble)

The live view system is one of the most sophisticated parts of the bot. It takes inspiration from the `logg.py` pattern:

1. When a user taps **📊 Status** or **📋 Logs**, the bot sends a new message and registers its `message_id` as the live-view bubble for that chat.
2. A background daemon thread (`LiveViewUpdater`) wakes every 2 seconds and iterates over all registered live views.
3. For each chat, it builds fresh combined text (status block + last N log lines) and compares it with `last_sent`. If the text changed, it enqueues an `editMessageText` payload.
4. The critical detail: before appending, it removes any existing `editMessageText` for the same `message_id` from the outbound queue — so only the freshest edit is ever pending. This prevents Telegram `"message not modified"` errors and queue bloat.
5. On terminal states (`idle`, `stopped`, `completed`, `error`), one final forced update is sent and the live view is unregistered.

Controls available in the live-view keyboard:
- **⏸ Pause Updates** / **▶️ Resume Updates** — temporarily suspends/resumes Telegram edits (SSE still fires).
- **🔄 Refresh** — forces an immediate rebuild and enqueue.
- **❌ Close** — sends a final update and unregisters the bubble.
- **Stream controls** (Stop, Pause Stream, Resume Stream) — context-aware row shown only during active streaming.

### Server-Sent Events (SSE)

Every state change, log line, and background action also fires a push event over SSE using `asyncio.Queue` objects, one per connected web client:

```javascript
// Example: connecting to the SSE stream from a web dashboard
const evtSource = new EventSource("/events/123456789");
evtSource.onmessage = (e) => {
  const event = JSON.parse(e.data);
  if (event.type === "state_change") updateStateUI(event.state);
  if (event.type === "log")          appendLogLine(event.line);
};
```

Event types:

| Type | Payload fields | Description |
|------|---------------|-------------|
| `state_change` | `state`, `frames_encoded`, `bytes_sent`, `uptime_str`, `logs` | Emitted on every state transition and every live-view tick |
| `log` | `line` | Single log line |
| `log_batch` | `lines[]` | Multiple log lines at once |
| `heartbeat` | — | Emitted every 5s of SSE idle to keep the connection alive |

### Outbound Message Queue

Since the bot runs in webhook-only mode (no outbound API calls), all responses to the user must be returned synchronously as the body of the `/webhook` HTTP response. Background threads that need to notify users enqueue message dicts here; the webhook handler pops and returns them on the next user interaction.

A dedicated `_outbound_queue_lock` (threading lock) serialises all queue mutations. The live-view system additionally enforces a cap of one pending `editMessageText` per message ID, preventing queue growth during periods of low webhook activity.

---

## Stream Lifecycle

```
idle
 └──[start command]──► starting
                            │
                      [input opened]
                            │
                            ▼
                        streaming ◄─────────────────────┐
                            │                            │
                    [user pause]    [stream error + reconnect_on_error]
                            │                            │
                            ▼                       reconnecting
                          paused                         │
                            │                    [reconnect OK]
                    [user resume]                        │
                            │                     [max attempts]
                            ▼                            │
                        streaming                        ▼
                            │                          error
                    [natural end]
                            │
                            ▼
                        completed ──► idle (next loop)
                            │
                     [no more loops]
                            ▼
                          stopped
```

State transitions trigger:
- SSE `state_change` event (all clients)
- Live-view bubble update (Telegram)
- Telegram notification message (on terminal states)

---

## Scheduler & Automated Jobs

[APScheduler](https://apscheduler.readthedocs.io/) runs in background mode on the UTC timezone. Scheduled jobs are stored in `_job_store` (a plain dict keyed by `job_id`) because APScheduler `Job` objects are frozen and cannot carry arbitrary metadata.

When a scheduled job fires, it:
1. Looks up the session for the relevant `chat_id`.
2. Starts the stream using the saved settings.
3. Enqueues a Telegram notification via `enqueue_outbound_message` — delivered to the user on their next webhook interaction.

This means scheduled streams work entirely without polling or long-running connections — the notification is simply held in memory until the user next sends a message.

---

## Session Management

Each `chat_id` gets its own isolated session dict, merged from:

- `DEFAULT_USER_SETTINGS` — all configurable preferences
- `DEFAULT_SESSION_RUNTIME_STATE` — runtime-only state (stream thread refs, frame counts, log lines, etc.)

Sessions are held in the module-level `user_sessions` dict and are **not persisted to disk** — they reset on process restart. Each session also has a dedicated `threading.Lock` in `session_locks` for thread-safe mutation.

The `get_user_session(chat_id)` function is the single source of truth: it creates and initialises the session on first call, and returns the existing one thereafter.

---

## Docker Deployment

### Dockerfile Explained

```dockerfile
FROM python:3.9-slim

COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
```

- **Base image**: `python:3.9-slim` — minimal Debian-based Python 3.9, no dev tools. Keeps the image lean.
- **Dependency layer first**: `requirements.txt` is copied and installed before the source code. This way, the dependency layer is cached by Docker and only rebuilt when `requirements.txt` changes — not on every code change.
- **Port 7860**: This is the default port expected by Hugging Face Spaces. For other platforms use `8000` or whatever your reverse proxy points to.
- **`--no-cache-dir`**: Saves ~50–100 MB in the final image by not storing the pip download cache.

> **Note**: There is a commented-out `WORKDIR /app` in the Dockerfile. Uncomment this line for cleaner, reproducible paths when running multiple containers or debugging inside the container.

### Running on Hugging Face Spaces

1. Create a new Space with the **Docker** SDK.
2. Upload `app.py`, `requirements.txt`, and `Dockerfile`.
3. Set the following Secrets (environment variables) in the Space settings:
   - `TELEGRAM_BOT_TOKEN` — your bot token from [@BotFather](https://t.me/BotFather)
4. The Space will auto-build and expose the app at `https://your-space.hf.space`.
5. Register the webhook (see below).

### Running Locally

```bash
# Build the image
docker build -t stream-bot .

# Run with your bot token
docker run -p 7860:7860 \
  -e TELEGRAM_BOT_TOKEN=your_token_here \
  stream-bot
```

Or without Docker:

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=your_token_here uvicorn app:app --host 0.0.0.0 --port 7860
```

---

## Telegram Webhook Setup

After the server is running, register the webhook with Telegram once:

```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook" \
  -d "url=https://your-domain.com/webhook" \
  -d "allowed_updates=[\"message\",\"callback_query\"]"
```

To verify:

```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/getWebhookInfo"
```

The bot operates in **webhook-only mode** — it never polls Telegram. All messages are received as HTTP POSTs to `/webhook`, and all responses are returned synchronously in the HTTP response body as a single Bot API method dict.

---

## Changelog — v3.3.0

This release focused on correctness, real-time UX, and reliability:

- **Fixed** — `frames_encoded` now counts decoded input frames correctly. Previously, encoder buffering caused the counter to read `0` until the encoder flushed.
- **Fixed** — `live_log_lines_user` is now cleared on stream start, so stale logs from a previous session don't persist into the new one.
- **Fixed** — `last_sent` is reset on stream start so the live-view bubble refreshes immediately rather than waiting for content to differ from the last session.
- **Fixed** — Raw FFmpeg/libav log lines are now captured via a queue-drained thread, surfacing codec init messages, bitrate feedback, and error lines in the live log.
- **Fixed** — Log tail display increased from 8 → 15 lines.
- **Fixed** — The `error` field in status messages no longer shows raw log lines — it shows only the human-readable error notification.
- **Fixed** — Error field is cleared on clean stop or abort.
- **Fixed** — Stats (uptime/frames/bytes) now update correctly in the live-view bubble.
- **Fixed** — Abort works instantly during reconnect sleep (no longer waits the full delay).
- **Fixed** — The watchdog does not mark the stream as errored after an intentional stop.
- **Fixed** — Stale edits no longer accumulate in the outbound queue (cap = 1 per message ID).
- **New** — `GET /poll/{chat_id}` endpoint returns the pending live-view edit for proactive refresh by a web client or Telegram Mini App.
- **New** — Live-view status+log bubble auto-edits every 2 seconds.
- **New** — Pause/Resume/Refresh/Close inline controls for the live-view bubble.
- **New** — Force Reboot button and `/reboot` command for last-resort process recovery.
- **New** — SSE `/events/{chat_id}` for a real-time web dashboard.
- **New** — Scheduler-triggered streams enqueue outbound Telegram notifications.

---

## Known Limitations

- **In-memory sessions only** — all settings and stream state are lost on process restart. A Redis or SQLite backend would be needed for persistence.
- **Single-output streaming** — each session streams to one RTMP destination. Multi-destination (simulcast) is not implemented.
- **No audio/video sync repair** — if the source has drift or discontinuities, they are passed through. DTS/PTS correction is applied but not gap-filling.
- **Webhook-only mode** — outbound Telegram API calls (`sendMessage` from background threads) are not supported. Notifications are logged and surfaced in the live view but not delivered proactively to Telegram unless the user sends a message first. The `/poll` endpoint is provided as a workaround for web clients.
- **No persistent scheduler storage** — scheduled jobs are lost on restart.

---

## Download Source Files

All three source files (`app.py`, `Dockerfile`, `requirements.txt`) are bundled below for your convenience:

➡️ **[Download advanced-stream-bot.zip](./assets/img/advanced-stream-bot.zip)**

> The archive contains:
> - `app.py` — Main FastAPI application (~3,200 lines)
> - `Dockerfile` — Container build instructions
> - `requirements.txt` — Python dependency manifest

---

*Built with ❤️ using FastAPI, PyAV, APScheduler, and the Telegram Bot API.*
