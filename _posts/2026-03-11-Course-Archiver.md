---
layout: post
title: "Building an Automated Video Course Archiver: Fetch, Download & Upload to Telegram"
date: 2026-03-11 10:00:00 +0530
categories: [automation, python, telegram, gradio]
tags: [python, automation, telegram, classplus, ffmpeg, gradio, threading, video-downloader, course-scraper]
author: "Akshay"
description: "A deep dive into a fully automated, resumable pipeline that catalogues, fetches streaming video links from a Classplus course, downloads them via FFmpeg, uploads them to a Telegram channel, and manages everything from a live Gradio dashboard."
image: /assets/img/video-pipeline-banner.png
---

# 🎥 Building an Automated Video Course Archiver

> **Catalogue → Fetch → Download → Upload → Repeat.** A fully automated, fault-tolerant pipeline for archiving Classplus course videos to Telegram — complete with a live web dashboard.

---

## Table of Contents

- [Overview](#overview)
- [System Architecture](#system-architecture)
- [Prerequisites](#prerequisites)
- [File Structure](#file-structure)
- [Script 0a: JSON Cataloguer (`All-in-One_JSON-based.py`)](#script-0a-json-cataloguer)
- [Script 0b: Directory Cataloguer (`directory-based.py`)](#script-0b-directory-cataloguer)
- [Script 1: Fetching Video Links (`fetch_videos_v2.py`)](#script-1-fetching-video-links)
- [Script 2: Downloading Videos (`download_videos_v2.py`)](#script-2-downloading-videos)
- [Script 3: Uploading to Telegram (`upv3.py`)](#script-3-uploading-to-telegram)
- [Script 4: The Dashboard (`main_v3.py`)](#script-4-the-dashboard)
- [Cataloguer Comparison: JSON vs Directory](#cataloguer-comparison-json-vs-directory)
- [Data Flow & State Files](#data-flow--state-files)
- [Configuration Reference](#configuration-reference)
- [Running the Pipeline](#running-the-pipeline)
- [Fault Tolerance & Resumability](#fault-tolerance--resumability)
- [Download Source Files](#download-source-files)

---

## Overview

This project is a **six-script automation system** that:

1. **Catalogues** (Option A) a Classplus course into a single structured JSON file — useful for a quick audit of all content types
2. **Catalogues** (Option B) the same course into a mirrored directory tree on disk — one JSON file per item, browsable like a real filesystem
3. **Fetches** playable `.m3u8` stream URLs for every video with full resumability and retry logic
4. **Downloads** those videos to local disk using FFmpeg with multi-threaded concurrency
5. **Uploads** the downloaded files sequentially to a Telegram channel
6. **Orchestrates** steps 3–5 from a single Gradio web dashboard with scheduling, live logs, and status tracking

Every script is designed with **resumability** at its core — if a run is interrupted at any point, the next run picks up exactly where it left off. No work is ever duplicated.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    main_v3.py (Dashboard)                    │
│         Gradio UI · Scheduler · Process Manager             │
└────────────────────┬────────────────────────────────────────┘
                     │ orchestrates (Steps 3–5)
                     │
┌────────────────────▼────────────────────────────────────────┐
│  [Optional Step 0 — choose one]                             │
│                                                             │
│  0a: All-in-One_JSON-based.py ──► course_content.json       │
│      (Single JSON, all content types)                       │
│                                                             │
│  0b: directory-based.py ──► course_content/                 │
│      (Mirrored folder tree, one .json file per item)        │
└─────────────────────────────────────────────────────────────┘
                     │
        ┌────────────▼────────────────────────┐
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
├── All-in-One_JSON-based.py   # Step 0a (optional): JSON catalogue of full course
├── directory-based.py         # Step 0b (optional): Directory-mirrored catalogue
├── fetch_videos_v2.py         # Step 1: Fetch stream URLs from Classplus
├── download_videos_v2.py      # Step 2: Download videos via FFmpeg
├── upv3.py                    # Step 3: Upload to Telegram
├── main_v3.py                 # Dashboard: Orchestrates Steps 1–3
├── .env                       # Your credentials (never commit this)
├── course_output/             # Output from Steps 1–3
│   ├── course_content.json            # Step 0a output
│   ├── course_data_with_videos.json   # Step 1 state
│   ├── download_manifest.json         # Step 2 state
│   ├── upload_state.json              # Step 3 state
│   ├── download_log.jsonl             # Step 0a/0b JSONL log
│   ├── download_links_log.jsonl       # Step 1 JSONL log
│   ├── video_downloader_log.jsonl     # Step 2 JSONL log
│   ├── telegram_uploader_log.jsonl    # Step 3 JSONL log
│   └── course_downloads/              # Downloaded .mp4 files
│       ├── Folder Name/
│       │   ├── Video Title.mp4
│       │   └── ...
│       └── ...
└── course_content/            # Output from Step 0b (directory-based)
    └── root/
        ├── index.json                 # Lists all items in this folder
        ├── Lecture 01 - Overview.json # Individual item metadata
        ├── Reference Notes.pdf.json
        └── Chapter 1 - Basics/        # Subfolder mirroring course structure
            ├── index.json
            ├── Video 01.json
            └── ...
```

---

## Script 0a: JSON Cataloguer

**File:** `All-in-One_JSON-based.py`

This is the **optional first step** — a standalone audit tool that scans an entire Classplus course and dumps everything (videos, documents, tests, and all folder hierarchy) into a single clean JSON file. Use it to explore a course's structure before committing to a full download pipeline.

### What Makes It Different

Unlike `fetch_videos_v2.py`, this script does **not** resolve playable video URLs. Its goal is purely to catalogue — quickly building a complete map of all content types in one shot. It's the right tool when you want to answer questions like:
- How many videos/docs/tests does this course have?
- What's the folder structure?
- Which content is locked?

### Content Types Captured

| `contentType` | Type | Fields Captured |
|---|---|---|
| `1` | Folder | `description`, `resources`, `counts` (videos/docs/tests), `children` |
| `2` | Video | `vidKey`, `thumbnailUrl`, `videoType`, `duration`, `contentHashId` |
| `3` | Document | `format`, `url`, `isContentLocked` |
| `4` | Test | `testId`, `maxMarks`, `URL` |

### How It Works

```
recursively_build_structure(folder_id=None)
    │
    ├── fetch_folder_content(course_id, folder_id)
    │       └── Handles pagination via offset loop until no items remain
    │
    ├── extract_relevant(item)
    │       └── Strips raw API response to a clean minimal dict per type
    │
    └── For each folder found → recurse into children
            └── Attaches result as nested 'children' list
```

The entire tree is built **in memory** and saved as one atomic write to `course_content.json` at the end — making this a fast, single-pass scan.

### Pagination Support

This script handles courses with many items per folder using an `offset`-based loop:

```python
while True:
    resp = session.get(BASE_URL, params=params)
    items = resp.json()['data']['courseContent']
    if not items:
        break
    all_items.extend(items)
    params['offset'] += len(items)
    time.sleep(0.2)
```

This is more thorough than `fetch_videos_v2.py` which does not implement explicit pagination.

### Authentication Headers

This script uses a fuller set of mobile-app headers to closely mimic the Classplus Android app, which can be helpful for courses with stricter API access controls:

```python
HEADERS = {
    'user-agent': 'Mobile-Android',
    'mobile-agent': 'Mobile-Android',
    'api-version': '52',
    'is-apk': '1',
    'region': 'IN',
    'x-requested-with': 'co.jones.stibl',
    'x-access-token': ACCESS_TOKEN,
    ...
}
```

### Sample Output (`course_content.json`)

```json
[
  {
    "contentType": 1,
    "id": 98765,
    "name": "Unit 1 - Fundamentals",
    "description": "Core concepts",
    "counts": { "documents": 3, "videos": 12, "tests": 2 },
    "children": [
      {
        "contentType": 2,
        "id": 11111,
        "name": "Intro Lecture",
        "duration": 2847,
        "contentHashId": "xyz987abc",
        "videoType": 1
      },
      {
        "contentType": 3,
        "id": 22222,
        "name": "Reference Notes.pdf",
        "format": "pdf",
        "url": "https://...",
        "isContentLocked": false
      },
      {
        "contentType": 4,
        "id": 33333,
        "name": "Unit 1 Quiz",
        "testId": 44444,
        "maxMarks": 20
      }
    ]
  }
]
```

### Key Configuration

| Variable | Default | Description |
|---|---|---|
| `ACCESS_TOKEN` | hardcoded | Classplus JWT token |
| `COURSE_ID` | `733740` | Target course ID |
| `OUTPUT_FILENAME` | `course_content.json` | Output file name |
| `REQUEST_TIMEOUT` | `30s` | Per-request timeout |
| `MAX_RETRIES` | `5` | Retry attempts on server errors |
| `RETRY_BACKOFF_FACTOR` | `0.5` | Exponential backoff multiplier |

> **Tip:** After running this script, inspect `course_content.json` to confirm the course ID and structure before running `fetch_videos_v2.py`.

---

## Script 0b: Directory Cataloguer

**File:** `directory-based.py`

This is an **alternative cataloguing approach** that saves each piece of course content as its own individual `.json` file, mirroring the course's folder hierarchy directly onto the filesystem. Instead of one big JSON blob, you get a browsable directory tree you can explore in any file manager.

### How It Works

```
recursively_save(folder_id=None, path=BASE_SAVE_PATH)
    │
    ├── fetch_folder_content(course_id, folder_id)
    │       └── Pagination loop (limit=50, offset increments)
    │
    ├── For each item → extract_relevant() → save_content_file()
    │       └── Writes individual <item_name>.json to current directory
    │
    ├── For each subfolder found → recurse with subfolder's ID
    │
    └── Write index.json listing all filenames in the current folder
```

### Filesystem Output

The script creates a directory tree that exactly mirrors the course structure:

```
course_content/
└── root/
    ├── index.json                        ← lists all items in this folder
    ├── Lecture 01 - Overview.json        ← video metadata
    ├── Reference Notes.pdf.json          ← document metadata
    ├── Unit 1 Quiz.json                  ← test metadata
    └── Chapter 1 - Fundamentals/         ← subfolder becomes a real directory
        ├── index.json
        ├── Intro Video.json
        └── ...
```

Each `index.json` is a simple list of filenames present in that directory, acting as a table of contents:

```json
[
  "Lecture 01 - Overview.json",
  "Reference Notes.pdf.json",
  "Unit 1 Quiz.json",
  "Chapter 1 - Fundamentals"
]
```

### Filename Sanitization (Two-Level)

The script attempts to save each file using the raw content name. If the OS rejects it (special characters, reserved names), it falls back to a sanitized version with an MD5 hash suffix to avoid collisions:

```python
# Level 1: raw name (may fail on some OSes)
raw_filepath = save_dir / f"{original_name}.json"

# Level 2: sanitized fallback with MD5 hash
sanitized_name = f"{name[:72]}_{hashlib.md5(name.encode()).hexdigest()[:6]}.json"
```

Both attempts are logged to `download_log.jsonl` so you can audit which files needed fallback names.

### Partial Scan with `START_FOLDER_ID`

A unique feature of this script: you can scan just one subtree instead of the whole course.

```python
# Scan entire course
START_FOLDER_ID = None

# Scan only a specific chapter/folder
START_FOLDER_ID = 38232234
```

This is useful when you want to re-scan or inspect a single section without waiting for the entire course to be traversed again.

### Sample Item Output (`Intro Video.json`)

```json
{
  "contentType": 2,
  "id": 11111,
  "name": "Intro Video",
  "description": "Course introduction",
  "vidKey": "abc123",
  "thumbnailUrl": "https://cdn.example.com/thumb.jpg",
  "videoType": 1,
  "duration": 2847,
  "contentHashId": "xyz987abc"
}
```

### Key Configuration

| Variable | Default | Description |
|---|---|---|
| `ACCESS_TOKEN` | hardcoded | Classplus JWT token |
| `COURSE_ID` | `516707` | Target course ID |
| `START_FOLDER_ID` | `None` | `None` = full course; set an int to scan a subtree |
| `BASE_SAVE_PATH` | `./course_content` | Root directory for all output |
| `INDEX_FILENAME` | `index.json` | Name of the per-folder index file |

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

## Cataloguer Comparison: JSON vs Directory

All three cataloguing scripts talk to the same Classplus API but serve different purposes. Here's a full side-by-side comparison:

| Feature | `All-in-One_JSON-based.py` | `directory-based.py` | `fetch_videos_v2.py` |
|---|---|---|---|
| **Primary purpose** | Single-file course audit | Filesystem-mirrored audit | Resolve playable video URLs |
| **Output format** | One `course_content.json` | One `.json` per item in OS folders | `course_data_with_videos.json` |
| **Browsable output** | ❌ Open in editor | ✅ Open in file manager | ❌ Open in editor |
| **Per-folder index** | ❌ No | ✅ `index.json` in each dir | ❌ No |
| **Partial subtree scan** | ❌ Always from root | ✅ `START_FOLDER_ID` | ❌ Always from root |
| **Content types** | Videos + Docs + Tests | Videos + Docs + Tests | Videos only |
| **Video URL resolution** | ❌ `contentHashId` only | ❌ `contentHashId` only | ✅ Final `.m3u8` URL |
| **Resumability** | ❌ Full rescan every run | ❌ Full rescan every run | ✅ Per-video state tracking |
| **Per-video retry tracking** | ❌ No | ❌ No | ✅ Up to 500 attempts |
| **Token expiry detection** | ❌ No | ❌ No | ✅ Exits after 10× 403s |
| **Pagination** | ✅ Offset loop | ✅ Offset loop (limit=50) | ❌ Single request per folder |
| **Filename sanitization** | Basic | ✅ Two-level with MD5 fallback | N/A |
| **Auth headers** | Full mobile headers | Full mobile headers | Minimal |
| **Used in dashboard** | ❌ Standalone | ❌ Standalone | ✅ Step 1 of pipeline |
| **Course ID (default)** | `733740` | `516707` | `516707` |

**When to use which:**

- **`All-in-One_JSON-based.py`** — Quick audit of a new course. Want one file you can search with `Ctrl+F` or `jq`.
- **`directory-based.py`** — Want to browse/inspect course content visually. Need to scan only a specific chapter. Course has many items with special characters in names.
- **`fetch_videos_v2.py`** — Ready to actually download videos. Need resumable, production-grade URL resolution that handles token expiry gracefully.

**Recommended workflow:**
1. Run either `0a` or `0b` to audit the course and confirm structure
2. Run `fetch_videos_v2.py` to resolve all playable URLs
3. Run the full pipeline via the dashboard to download and upload

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
| `ACCESS_TOKEN` | `All-in-One_JSON-based.py` | hardcoded | Classplus JWT token |
| `COURSE_ID` | `All-in-One_JSON-based.py` | `733740` | Target course ID for cataloguing |
| `OUTPUT_FILENAME` | `All-in-One_JSON-based.py` | `course_content.json` | Catalogue output file |
| `ACCESS_TOKEN` | `directory-based.py` | hardcoded | Classplus JWT token |
| `COURSE_ID` | `directory-based.py` | `516707` | Target course ID |
| `START_FOLDER_ID` | `directory-based.py` | `None` | `None` = full course; int = subtree only |
| `BASE_SAVE_PATH` | `directory-based.py` | `./course_content` | Root output directory |
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

### Option A: Dashboard (Recommended for Steps 1–3)

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
# Step 0a (optional): Catalogue into a single JSON file
python All-in-One_JSON-based.py
# → Produces: course_output/course_content.json

# Step 0b (optional): Catalogue into a mirrored directory tree
python directory-based.py
# → Produces: course_content/root/<folders and .json files>

# Step 0b (partial scan — specific folder only):
# Edit START_FOLDER_ID = 38232234 in the script, then:
python directory-based.py

# Step 1: Fetch video URLs
python fetch_videos_v2.py
# → Produces: course_output/course_data_with_videos.json

# Step 2: Download videos (requires ffmpeg)
python download_videos_v2.py
# → Produces: course_output/course_downloads/*.mp4

# Step 3: Upload to Telegram (requires local Bot API server)
python upv3.py
# → Uploads to Telegram, produces: course_output/upload_state.json
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

All six Python scripts are available as a single zip archive:

> 📦 **[Download video_pipeline_scripts.zip](./assets/img/video_pipeline_scripts.zip)**

**Contents:**

| File | Description |
|---|---|
| `All-in-One_JSON-based.py` | Full course cataloguer → single JSON file |
| `directory-based.py` | Full course cataloguer → mirrored directory tree |
| `fetch_videos_v2.py` | Classplus API scraper & `.m3u8` URL resolver |
| `download_videos_v2.py` | Multi-threaded HLS downloader via FFmpeg |
| `upv3.py` | Sequential Telegram uploader with message link tracking |
| `main_v3.py` | Gradio dashboard & scheduler |

---

## Notes & Disclaimer

- This tool is intended for **personal archiving** of content you have legitimate access to.
- The Classplus `ACCESS_TOKEN` is a JWT that expires periodically. When it does, update it via the dashboard's token management panel or directly in `.env`.
- The local Telegram Bot API server (`127.0.0.1:8081`) is required for files larger than 50 MB. For smaller files, you can switch `BASE_URL_TEMPLATE` to `https://api.telegram.org/bot{}`.
- Always keep your `.env` file out of version control. Add it to `.gitignore`.

---

*Built with Python · Gradio · FFmpeg · Telegram Bot API*
