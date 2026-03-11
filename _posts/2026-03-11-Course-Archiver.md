---
layout: post
title: "Building an Automated Video Course Archiver: Fetch, Download & Upload to Telegram"
date: 2026-03-11 10:00:00 +0530
categories: [automation, python, telegram, gradio]
tags: [python, automation, telegram, classplus, ffmpeg, gradio, threading, video-downloader]
author: "Akshay"
description: "A deep dive into a fully automated, resumable pipeline that fetches streaming video links from a Classplus course, downloads them via FFmpeg, uploads them to a Telegram channel, and manages everything from a live Gradio dashboard."
image: /assets/img/video-pipeline-banner.png
---

# 🎥 Building an Automated Video Course Archiver

> **Fetch → Download → Upload → Repeat.** A fully automated, fault-tolerant pipeline for archiving Classplus course videos to Telegram — complete with a live web dashboard.

---

## Table of Contents

- [Overview](#overview)
- [System Architecture](#system-architecture)
- [Prerequisites](#prerequisites)
- [File Structure](#file-structure)
- [Script 1: Fetching Video Links (`fetch_videos_v2.py`)](#script-1-fetching-video-links)
- [Script 2: Downloading Videos (`download_videos_v2.py`)](#script-2-downloading-videos)
- [Script 3: Uploading to Telegram (`upv3.py`)](#script-3-uploading-to-telegram)
- [Script 4: The Dashboard (`main_v3.py`)](#script-4-the-dashboard)
- [Data Flow & State Files](#data-flow--state-files)
- [Configuration Reference](#configuration-reference)
- [Running the Pipeline](#running-the-pipeline)
- [Fault Tolerance & Resumability](#fault-tolerance--resumability)
- [Download Source Files](#download-source-files)

---

## Overview

This project is a **four-script automation system** that:

1. **Scans** a Classplus course's API to collect all video stream URLs
2. **Downloads** those videos to local disk using FFmpeg with multi-threaded concurrency
3. **Uploads** the downloaded files sequentially to a Telegram channel
4. **Orchestrates** all three steps from a single Gradio web dashboard with scheduling, live logs, and status tracking

Every script is designed with **resumability** at its core — if a run is interrupted at any point, the next run picks up exactly where it left off. No work is ever duplicated.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    main_v3.py (Dashboard)                    │
│         Gradio UI · Scheduler · Process Manager             │
└────────────────────┬────────────────────────────────────────┘
                     │ orchestrates
        ┌────────────▼────────────────────────┐
        │                                     │
        ▼                                     │
┌───────────────┐                             │
│fetch_videos   │──► course_data_with_        │
│   _v2.py      │    videos.json              │
└───────────────┘         │                  │
        │                 ▼                  │
        │     ┌───────────────────┐          │
        │     │download_videos    │──► course_downloads/
        │     │   _v2.py          │    *.mp4
        │     └───────────────────┘          │
        │               │                   │
        │               ▼                   │
        │     ┌───────────────────┐          │
        │     │    upv3.py        │──► Telegram Channel
        │     │ (Telegram Upload) │    @channel
        │     └───────────────────┘          │
        └─────────────────────────────────────┘
```

Each script produces and consumes **JSON state files** that act as the shared memory between stages. This decoupled design means any script can be run independently or restarted at any time.

---

## Prerequisites

### System Dependencies

```bash
# FFmpeg (required for downloading HLS streams)
sudo apt install ffmpeg        # Ubuntu/Debian
brew install ffmpeg            # macOS

# Python 3.8+
python3 --version
```

### Python Packages

```bash
pip install requests python-dotenv gradio colorlog tqdm
```

### Optional: Local Telegram Bot API Server

For uploading files larger than 50 MB (the cloud API limit), the uploader uses a local Bot API server on `127.0.0.1:8081`. You can run the official server from [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) or use Docker:

```bash
docker run -d -p 8081:8081 aiogram/telegram-bot-api \
  --api-id=YOUR_API_ID \
  --api-hash=YOUR_API_HASH
```

### Environment File

Create a `.env` file in your working directory:

```ini
ACCESS_TOKEN=your_classplus_jwt_token_here
COURSE_SAVE_PATH=./course_output
```

---

## File Structure

```
project/
├── fetch_videos_v2.py         # Step 1: Fetch stream URLs from Classplus
├── download_videos_v2.py      # Step 2: Download videos via FFmpeg
├── upv3.py                    # Step 3: Upload to Telegram
├── main_v3.py                 # Dashboard: Orchestrates all 3 scripts
├── .env                       # Your credentials (never commit this)
└── course_output/             # All generated state and downloaded files
    ├── course_data_with_videos.json   # Fetcher state
    ├── download_manifest.json         # Downloader state
    ├── upload_state.json              # Uploader state
    ├── download_links_log.jsonl       # Fetcher JSONL log
    ├── video_downloader_log.jsonl     # Downloader JSONL log
    ├── telegram_uploader_log.jsonl    # Uploader JSONL log
    └── course_downloads/              # Downloaded .mp4 files
        ├── Folder Name/
        │   ├── Video Title.mp4
        │   └── ...
        └── ...
```

---

## Script 1: Fetching Video Links

**File:** `fetch_videos_v2.py`

This script is the **entry point** of the pipeline. It authenticates with the Classplus API, recursively scans the course folder tree, and resolves the final playable `.m3u8` URL for each video.

### How It Works

**On first run:**
1. Calls `GET /v2/course/content/get` recursively to build a complete folder/video tree
2. For each video, calls the JW Player signed URL endpoint to get the master `.m3u8` playlist
3. Parses the playlist to find the highest-quality variant stream URL
4. Saves the full tree (with URLs) to `course_data_with_videos.json`

**On subsequent runs:**
1. Loads the existing state file
2. Runs a **stale hash check** — compares `contentHashId` values against fresh API data. If no changes are found in the first 10 items, it skips the full refresh (performance optimization)
3. Processes only videos with status `pending` or `failed`

### Resumability & Retry Logic

```python
MAX_VIDEO_ATTEMPTS = 500  # Each video gets up to 500 tries across all runs
CONSECUTIVE_ERROR_LIMIT = 10  # Exit early if token appears expired
```

Video statuses cycle through: `pending` → `completed` / `failed` → `permanently_failed`

State is saved **after every single video** — so even a crash mid-run loses at most one video's work.

### Token Expiry Detection

The script tracks consecutive 403/404 errors. If 10 errors occur back-to-back, it raises a `CriticalErrorExit` exception, saves progress, logs a clear message, and exits cleanly — rather than hammering the API with a dead token.

```python
class CriticalErrorExit(Exception):
    pass

# Triggered inside get_final_video_url()
if consecutive_error_counter >= CONSECUTIVE_ERROR_LIMIT:
    raise CriticalErrorExit("Please check your ACCESS_TOKEN.")
```

### Key Configuration

| Variable | Default | Description |
|---|---|---|
| `COURSE_ID` | `516707` | Classplus course ID |
| `BASE_URL` | Classplus v2 API | Content listing endpoint |
| `MAX_VIDEO_ATTEMPTS` | `500` | Retries per video across all runs |
| `CONSECUTIVE_ERROR_LIMIT` | `10` | 403/404s before forced exit |
| `API_DELAY` | `0.5s` | Delay between API calls |

---

## Script 2: Downloading Videos

**File:** `download_videos_v2.py`

This script reads the JSON state from the fetcher and downloads all videos to local disk using FFmpeg, with up to **10 concurrent downloads** running simultaneously.

### Architecture: Worker Pool + State Manager

The script uses two distinct concurrency layers:

```
Main Thread
    │
    ├── ThreadPoolExecutor (10 workers)
    │       ├── Worker 1: ffmpeg -i <url> video1.mp4
    │       ├── Worker 2: ffmpeg -i <url> video2.mp4
    │       ├── ...
    │       └── Worker 10: ffmpeg -i <url> video10.mp4
    │
    └── StateManager Thread
            └── Reads Queue → Updates manifest → Saves to disk every 5s
```

This design ensures:
- **No I/O contention** — only one thread ever writes to disk
- **Maximum throughput** — a new download starts the instant any worker finishes
- **Data integrity** — state is saved periodically even if the main process crashes

### FFmpeg Integration

Each download runs FFmpeg to convert the `.m3u8` HLS stream directly to `.mp4`:

```bash
ffmpeg -y -i <m3u8_url> -c copy -bsf:a aac_adtstoasc output.mp4
```

The `-c copy` flag means no re-encoding — the video is simply remuxed, making downloads as fast as your connection allows.

### Progress Bars

When `tqdm` is available, each worker shows a live progress bar tied to the video's duration (via `ffprobe`):

```
Overall Progress  |████████████░░░░░| 43/100 [02:15<03:01]
Lecture 01 - Intro.mp4       |████████████████| 1234.0s/1234.0s
Lecture 02 - Basics.mp4      |████████░░░░░░░░|  621.0s/1234.0s
```

### State Sync on Resume

When resuming, the script calls `_refresh_manifest_from_source()` to:
- Update video names and URLs from the latest fetcher output
- Reset any `failed` downloads to `pending` so they get retried with fresh URLs

### Key Configuration

| Variable | Default | Description |
|---|---|---|
| `MAX_CONCURRENT_DOWNLOADS` | `10` | Parallel download threads |
| `SAVE_INTERVAL_SECONDS` | `5.0` | How often state is saved to disk |
| `INPUT_DIR` | `./course_output` | Where to read links file from |
| `DOWNLOAD_ROOT_DIR` | `./course_output/course_downloads` | Where to save `.mp4` files |

---

## Script 3: Uploading to Telegram

**File:** `upv3.py`

This script reads `download_manifest.json`, synchronizes with any existing upload state, and uploads all completed `.mp4` files to a Telegram channel — strictly **one at a time**, in order.

### Why Sequential?

Upload order matters for Telegram channels — viewers expect lectures in the correct sequence. A concurrent uploader would cause messages to appear out of order due to network variability. Sequential upload guarantees perfect ordering.

### State Synchronization

On each run, `synchronize_states()` merges the download manifest with the existing upload state:

1. Finds all videos with `downloadStatus: completed` that aren't yet in the upload state → adds them as `pending`
2. Resets any `uploading` status to `pending` (these were interrupted mid-upload last time)
3. Preserves all previously `completed` upload records

### Message Link Tracking

After each successful upload, the script constructs and saves a direct `t.me/...` link:

```python
# Public channel (e.g., @mychannel)
tele_message_link = f"https://t.me/{chat_username}/{message_id}"

# Private supergroup (e.g., -10012345678)
chat_id_short = str(CHAT_ID).replace("-100", "")
tele_message_link = f"https://t.me/c/{chat_id_short}/{message_id}"
```

This link is stored in `upload_state.json` alongside the `telegramFileId` and `widget_id` for each video.

### Caption Format

Each uploaded video gets a structured caption showing its folder path and title:

```
📁 Chapter 1 / Week 3 / Organic Chemistry

- 📄 Lecture 14: Reaction Mechanisms
```

### Rate Limit & Retry Handling

```python
MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 8

# On HTTP 429 (Too Many Requests):
retry_after = int(e.response.json()['parameters']['retry_after'])
time.sleep(retry_after)  # Respects Telegram's own backoff instruction
```

### Graceful Interruption

If Ctrl+C is pressed mid-upload, the current video is marked `pending` (not `failed`) so the next run re-uploads it cleanly from the beginning. No partial uploads are ever left in a `completed` state.

### Key Configuration

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | Your bot token | Telegram Bot API token |
| `CHAT_ID` | `@nifobry` | Target channel (public `@name` or private `-100...`) |
| `BASE_URL_TEMPLATE` | `http://127.0.0.1:8081` | Local Bot API server for large files |
| `MAX_RETRIES` | `5` | Upload retry attempts per file |
| `STARTING_WIDGET_ID` | `1` | Sequential ID counter for uploads |

---

## Script 4: The Dashboard

**File:** `main_v3.py`

The dashboard is a **Gradio web application** that ties all three scripts together into a manageable control panel. It runs each script as a subprocess, streams their output to log files in real time, and exposes controls through a browser-based UI.

### UI Layout

```
┌─────────────────────────────────────────────────────────────┐
│  🎥 Video Processing Pipeline Dashboard                      │
│  [ Compact log view ]  [ Dark mode ]                         │
├────────────────────────┬────────────────────────────────────┤
│  ⚡ Controls           │  📜 Logs                            │
│  [▶ Start] [⏹ Stop]   │                                     │
│  [🚀 Run Now] [⏭ Skip]│  Application Logs (Dashboard)       │
│                        │  ┌────────────────────────────────┐│
│  Schedule: [──●──] 14h │  │ 2025-07-15 10:23:01 [INFO]... ││
│  [Apply Interval]      │  └────────────────────────────────┘│
│                        │                                     │
│  📊 Pipeline Status    │  Subprocess Logs (Scripts)          │
│  ● Running             │  ┌────────────────────────────────┐│
│  Step 2/3: download... │  │ [10:23:05] ✅ SUCCESS: URL...  ││
│  Last Run: 10:23:01    │  └────────────────────────────────┘│
│  Next Run: 00:23:01    │                                     │
│  ✔️3  ❌1  ⏹️0  ⏭️0   │  [🔄 Refresh All]                  │
│                        │                                     │
│  🔑 Token Management   │                                     │
│  [••••••••••••] Update │                                     │
└────────────────────────┴────────────────────────────────────┘
```

### Key Features

**Scheduling:** Start a repeating schedule that runs immediately, then every N hours (1–168h range, user-configurable via slider). The next scheduled time is always shown in the status panel.

**Manual Control:**
- **▶ Start Schedule** — begins the periodic runner
- **⏹ Stop All** — halts the scheduler and sends SIGINT to any running subprocess, escalating to SIGTERM and SIGKILL if needed
- **🚀 Run Now** — one-shot manual trigger (works alongside or without a schedule)
- **⏭ Skip Step** — terminates the current script and advances the pipeline to the next one

**Subprocess Management:** Each script runs as a child process with its stdout/stderr piped to `subprocess.log` via a background streaming thread. The main app also writes its own structured log to `app.log`. Both logs are tailed and shown in the UI.

**Status Indicators:** Color-coded status dot:
- 🟢 `limegreen` — Completed / Idle
- 🟡 `goldenrod` — Waiting for next scheduled run
- 🟠 `orangered` — Currently running
- 🔴 `red` — Failed / Stopped

**Token Management:** The ACCESS_TOKEN can be updated directly from the UI without restarting. The new value is written to `.env` and immediately applied to `os.environ`.

**Auto-Refresh:** A `gr.Timer(5)` component polls `master_refresh_cb()` every 5 seconds to update status, logs, and button interactivity without any page reload.

### Process Lifecycle

```python
# Graceful shutdown sequence (GRACE_SHUTDOWN_SECONDS = 5)
1. Send SIGINT (Ctrl+C equivalent)
2. Wait up to 5 seconds
3. If still alive → SIGTERM
4. Wait 2 seconds
5. If still alive → SIGKILL
```

### Log Viewing Modes

| Mode | App Log Lines | Subprocess Log Lines |
|---|---|---|
| Expanded (default) | 1500 | 1500 |
| Compact | 400 | 400 |

---

## Data Flow & State Files

Understanding the JSON state files is key to understanding how the pipeline is resumable.

### `course_data_with_videos.json` (Fetcher Output)

```json
[
  {
    "contentType": 1,
    "id": 12345,
    "name": "Chapter 1 - Introduction",
    "children": [
      {
        "contentType": 2,
        "id": 67890,
        "name": "Lecture 01 - Overview",
        "contentHashId": "abc123xyz",
        "duration": 3612,
        "downloadStatus": "completed",
        "finalUrl": "https://cdn.jwplayer.com/.../index.m3u8",
        "retryCount": 1
      }
    ]
  }
]
```

### `download_manifest.json` (Downloader Output)

Extends the fetcher JSON with download-specific fields:

```json
{
  "localPath": "Chapter 1 - Introduction/Lecture 01 - Overview.mp4",
  "downloadStatus": "completed",
  "downloadError": null
}
```

### `upload_state.json` (Uploader Output)

Extends the download manifest with upload-specific fields:

```json
{
  "uploadStatus": "completed",
  "telegramFileId": "BQACAgIAAxkBAAI...",
  "tele_message_link": "https://t.me/nifobry/42",
  "widget_id": 42,
  "uploadError": null
}
```

---

## Configuration Reference

All key settings are defined at the top of each script as module-level constants, making them easy to find and adjust.

| Setting | File | Default | Description |
|---|---|---|---|
| `ACCESS_TOKEN` | `fetch_videos_v2.py` | `.env` | Classplus JWT token |
| `COURSE_ID` | `fetch_videos_v2.py` | `516707` | Target course ID |
| `MAX_VIDEO_ATTEMPTS` | `fetch_videos_v2.py` | `500` | Max retries per video |
| `CONSECUTIVE_ERROR_LIMIT` | `fetch_videos_v2.py` | `10` | 403/404s before abort |
| `MAX_CONCURRENT_DOWNLOADS` | `download_videos_v2.py` | `10` | Parallel ffmpeg workers |
| `SAVE_INTERVAL_SECONDS` | `download_videos_v2.py` | `5.0` | State save frequency |
| `BOT_TOKEN` | `upv3.py` | hardcoded | Telegram Bot token |
| `CHAT_ID` | `upv3.py` | `@nifobry` | Target Telegram channel |
| `MAX_RETRIES` | `upv3.py` | `5` | Upload retry attempts |
| `DEFAULT_INTERVAL_HOURS` | `main_v3.py` | `14` | Default pipeline schedule |
| `GRACE_SHUTDOWN_SECONDS` | `main_v3.py` | `5` | Time before SIGTERM escalation |

---

## Running the Pipeline

### Option A: Dashboard (Recommended)

```bash
# 1. Set up environment
echo "ACCESS_TOKEN=your_token_here" > .env

# 2. Install dependencies
pip install requests python-dotenv gradio colorlog tqdm

# 3. Launch the dashboard
python main_v3.py
# Open browser at http://127.0.0.1:7860

# 4. Click "▶ Start Schedule" or "🚀 Run Now"
```

### Option B: Run Scripts Individually

```bash
# Step 1: Fetch video URLs
python fetch_videos_v2.py

# Step 2: Download videos (requires ffmpeg)
python download_videos_v2.py

# Step 3: Upload to Telegram (requires local Bot API server)
python upv3.py
```

### Option C: Automate with Cron

```bash
# Run the full pipeline every day at 2 AM
0 2 * * * cd /path/to/project && python fetch_videos_v2.py && python download_videos_v2.py && python upv3.py
```

---

## Fault Tolerance & Resumability

This pipeline was designed with one guiding principle: **any script can be interrupted at any time and safely restarted.**

| Scenario | Behavior |
|---|---|
| Fetcher interrupted mid-scan | Resumes from last saved video on next run |
| Token expires during fetch | Detects 10 consecutive 403s → saves & exits cleanly |
| Downloader crashes | State Manager has already saved progress; resumes on restart |
| Upload interrupted (Ctrl+C) | In-progress video reset to `pending`; not marked failed |
| Script process killed by OS | State saved every 5 seconds; at most 5s of work lost |
| New videos added to course | Stale hash check detects changes and refreshes URLs |
| Previously failed downloads | Auto-reset to `pending` on next downloader run |

---

## Download Source Files

All four Python scripts are available as a single zip archive:

> 📦 **[Download video_pipeline_scripts.zip](./assets/img/video_pipeline_scripts.zip)**

**Contents:**

| File | Size | Description |
|---|---|---|
| `fetch_videos_v2.py` | ~18 KB | Classplus API scraper & URL resolver |
| `download_videos_v2.py` | ~16 KB | Multi-threaded HLS downloader |
| `upv3.py` | ~20 KB | Sequential Telegram uploader |
| `main_v3.py` | ~22 KB | Gradio dashboard & scheduler |

---

## Notes & Disclaimer

- This tool is intended for **personal archiving** of content you have legitimate access to.
- The Classplus `ACCESS_TOKEN` is a JWT that expires periodically. When it does, update it via the dashboard's token management panel or directly in `.env`.
- The local Telegram Bot API server (`127.0.0.1:8081`) is required for files larger than 50 MB. For smaller files, you can switch `BASE_URL_TEMPLATE` to `https://api.telegram.org/bot{}`.
- Always keep your `.env` file out of version control. Add it to `.gitignore`.

---

*Built with Python · Gradio · FFmpeg · Telegram Bot API*
