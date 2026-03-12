---
layout: default
title: rclone-uploader — Universal Rclone Upload Framework
date: 2026-03-12
tags: [Programming, Learn] # add tag
---
---

# 📦 rclone-uploader

> **A universal, interruption-safe, production-grade framework for bulk cloud uploads — powered by [rclone](https://rclone.org/).**

Works with **any** rclone-supported backend: Google Drive, Amazon S3, Dropbox, Mega, Backblaze B2, OneDrive, SFTP, and [70+ more](https://rclone.org/overview/).

---

## ✨ Why Use This?

Raw `rclone copy` is great for one-off transfers. But when you have:

- **Multiple folders** to upload to different remotes
- **Unreliable connections** that drop mid-transfer
- **Large datasets** that take hours and need to be resumable
- **CI/CD pipelines** that need structured exit codes and logs

...you need something smarter. That's what this framework provides.

---

## 🚀 Quickstart

```bash
# 1. Clone the framework
git clone https://github.com/your-username/rclone-uploader.git
cd rclone-uploader

# 2. Make the script executable
chmod +x rclone-uploader.sh

# 3. Configure your rclone remotes (if you haven't already)
rclone config

# 4. Create your job definitions
cp config/jobs.example.conf config/my-jobs.conf
# Edit config/my-jobs.conf with your own paths and remotes

# 5. Run!
./rclone-uploader.sh --groups config/my-jobs.conf
```

---

## 📁 Project Structure

```
rclone-uploader/
├── rclone-uploader.sh        # ← The framework (one file, no dependencies)
├── config/
│   └── jobs.example.conf     # ← Copy and edit this for your use case
└── README.md
```

---

## ⚙️ Job Definitions (`jobs.conf`)

Everything about **what** to upload and **where** lives in a plain-text `.conf` file. No code changes needed.

### File Format

```ini
# [settings] — optional, override framework defaults
[settings]
BASE_PATH   = /path/to/your/files
TRANSFERS   = 4
MAX_RETRIES = 5

# [jobs] — one job per line: label|local_path|remote_path
[jobs]

# Photo backups
photos-2022|Photos/2022|gdrive:Backups/Photos/2022
photos-2023|Photos/2023|gdrive:Backups/Photos/2023

# Course videos to Mega
course-videos|Courses/Videos|mega:Courses/Videos

# Server backups to S3
db-backup|/var/backups/postgres|s3:my-bucket/databases
```

### Job Line Format

```
label | local_relative_path | remote_path
```

| Field | Description |
|---|---|
| `label` | Unique name for the job. Used in logs and `--force`. No spaces — use hyphens or underscores. |
| `local_relative_path` | Path to the source folder, relative to `BASE_PATH` (or absolute). |
| `remote_path` | Full rclone remote destination: `remotename:bucket/path` |

### `[settings]` Keys

All settings are optional and can also be overridden by CLI flags.

| Key | Default | Description |
|---|---|---|
| `BASE_PATH` | `.` (current dir) | Root that local job paths are relative to |
| `RCLONE_CONFIG` | rclone default | Path to your `rclone.conf` |
| `RCLONE_BIN` | searches `$PATH` | Path to rclone binary |
| `LOG_DIR` | `<BASE_PATH>/.rclone_logs` | Where logs and state files are stored |
| `TRANSFERS` | `4` | Parallel file transfers |
| `CHECKERS` | `8` | Parallel metadata checkers |
| `MAX_RETRIES` | `5` | Max attempts per job before marking FAIL |
| `RETRY_SLEEP` | `30` | Seconds to wait between retry attempts |

---

## 🖥️ CLI Reference

```
USAGE
  ./rclone-uploader.sh --groups <jobs.conf> [options]

REQUIRED
  --groups FILE          Path to your job definitions .conf file

PATH OPTIONS
  --base PATH            Override BASE_PATH from the conf file
  --config FILE          rclone config file path
  --rclone-bin PATH      Path to rclone binary
  --log-dir PATH         Override log/state directory

TRANSFER OPTIONS
  --transfers N          Parallel transfers (default: 4)
  --checkers N           Parallel checkers (default: 8)
  --max-retries N        Max attempts per job (default: 5)
  --retry-sleep SEC      Seconds between retries (default: 30)

VERIFICATION
  --no-verify            Skip post-upload integrity check
  --deep-verify          Use checksums instead of file-size comparison

RUN CONTROL
  --dry-run              Simulate everything, no files transferred
  --force-all            Reprocess all jobs, ignore saved state
  --force LABEL          Reprocess one specific job only
  --list-jobs            Print all job labels and exit

OTHER
  --version              Print framework version
  -h|--help              Show help
```

---

## 📋 Common Usage Patterns

**Normal run (auto-resumes if run again):**
```bash
./rclone-uploader.sh --groups config/my-jobs.conf
```

**Dry run — see what would happen without transferring:**
```bash
./rclone-uploader.sh --groups config/my-jobs.conf --dry-run
```

**Resume after interruption:**
```bash
# Just run it again — completed jobs are skipped automatically
./rclone-uploader.sh --groups config/my-jobs.conf
```

**Re-run one failed job:**
```bash
./rclone-uploader.sh --groups config/my-jobs.conf --force photos-2024
```

**Force re-upload everything from scratch:**
```bash
./rclone-uploader.sh --groups config/my-jobs.conf --force-all
```

**Thorough verification with checksums:**
```bash
./rclone-uploader.sh --groups config/my-jobs.conf --deep-verify
```

**Override base path at runtime:**
```bash
./rclone-uploader.sh --groups config/my-jobs.conf --base /mnt/external-drive
```

**Use more parallel transfers on a fast connection:**
```bash
./rclone-uploader.sh --groups config/my-jobs.conf --transfers 16 --checkers 16
```

---

## 🔬 How It Works

### 1. Singleton Lock
When the script starts, it acquires an exclusive `flock` lock. If you try to run a second instance pointing at the same log directory, it exits immediately — preventing duplicate uploads.

### 2. State Machine
Every job is tracked in a `state.tsv` file inside the log directory:

```
photos-2022    OK        1
photos-2023    RUNNING   2
course-videos  FAIL      5
db-backup      PENDING   0
```

| State | Meaning |
|---|---|
| `PENDING` | Not yet attempted |
| `RUNNING` | In progress (or crashed mid-run) |
| `OK` | Uploaded and verified — will be skipped on next run |
| `FAIL` | Exhausted all retries |

The state file is written **atomically** (write to `.new`, then rename) so it's never corrupted by a mid-write crash.

### 3. Retry Loop
Each job retries up to `MAX_RETRIES` times. Between each attempt it sleeps for `RETRY_SLEEP` seconds. Upload and verification are each separate steps — a verification failure triggers a full re-upload retry.

### 4. Upload + Verify
By default, after each successful `rclone copy`, the script runs `rclone check --size-only` to confirm all files arrived. With `--deep-verify`, it uses full checksums instead (slower but catches bit-level corruption).

### 5. Progress Flag Safety
The `-P` (progress) flag is only added during real runs. Using it with `--dry-run` causes rclone to exit with a non-zero code, which would incorrectly trigger the failure path. The framework handles this automatically.

### 6. Exit Codes
| Code | Meaning |
|---|---|
| `0` | All jobs succeeded |
| `1` | Fatal error (bad config, rclone not found, etc.) |
| `2` | Partial failure — one or more jobs failed |

Exit code `2` also writes a `failed_jobs.txt` file in the log directory, listing failed jobs in the same `label|source|remote` format — ready for use in CI/CD pipelines or automated alerting.

---

## 📂 Log Files

All logs are stored in `<BASE_PATH>/.rclone_logs/` (or your custom `--log-dir`):

| File | Contents |
|---|---|
| `master.log` | Timestamped, color-annotated log of everything |
| `state.tsv` | Job state machine — tracks PENDING/RUNNING/OK/FAIL |
| `<label>.log` | Detailed rclone output for each individual job |
| `failed_jobs.txt` | Written only on failure — lists failed jobs for CI/CD |

**Live monitoring:**
```bash
tail -f /path/to/source/.rclone_logs/master.log
```

**Inspect one job's rclone output:**
```bash
cat /path/to/source/.rclone_logs/photos-2024.log
```

---

## 🌍 Supported Backends

Any backend supported by rclone works out of the box:

| Backend | Remote Format |
|---|---|
| Google Drive | `gdrive:folder/path` |
| Amazon S3 | `s3:bucket-name/path` |
| Dropbox | `dropbox:folder/path` |
| Mega | `mega:folder/path` |
| Backblaze B2 | `b2:bucket-name/path` |
| Microsoft OneDrive | `onedrive:folder/path` |
| SFTP | `sftp:folder/path` |
| And 70+ more | See [rclone.org/overview](https://rclone.org/overview/) |

---

## 🔧 Requirements

| Requirement | Notes |
|---|---|
| **Bash 4.0+** | Uses associative arrays (`declare -A`) |
| **rclone** | Any recent version. [Install guide](https://rclone.org/install/) |
| **flock** | Part of `util-linux` — pre-installed on most Linux distros |
| **Configured remotes** | Run `rclone config` to set up your backends |

---

## 💡 Tips

**Multiple job files for different projects:**
```bash
./rclone-uploader.sh --groups config/photos.conf
./rclone-uploader.sh --groups config/work-docs.conf --log-dir /var/log/work-upload
```

**Run on a schedule with cron:**
```cron
0 2 * * * /path/to/rclone-uploader.sh --groups /path/to/config/backup.conf >> /var/log/rclone-cron.log 2>&1
```

**Use in a CI/CD pipeline:**
```yaml
- name: Upload assets
  run: ./rclone-uploader.sh --groups config/ci-jobs.conf --no-verify
  # Exit code 2 = partial failure — pipeline will catch it
```

**Check which jobs exist in a conf file:**
```bash
./rclone-uploader.sh --groups config/my-jobs.conf --list-jobs
```

---

## 📥 Download

➡️ **[Download rclone-upload-framework.zip](./assets/img/rclone-upload-framework.zip)**

The zip includes the framework (`rclone-uploader.sh`), the example config, and the original scripts below.

---

## 🗂️ Origin — Where This Started

This framework grew out of two personal scripts I was working on for uploading pharmacy course videos to cloud storage. Nothing fancy — just me figuring things out.

**`upload_all.sh`** — my script for uploading new course batches (9 groups across 4 Mega remotes).

**`old_upload.sh`** — a second script I wrote later for archiving old recorded lectures (5 buckets across 4 remotes). This one ended up slightly better written — it handled comment lines in the job table properly, which the first one didn't.

Both scripts are included in the zip as-is, in the `originals/` folder. They work fine for my specific folder structure, but the whole point of this framework was to take what I learned writing them and turn it into something anyone could use without editing source code.

---

## 📜 License

MIT License — free to use, modify, and distribute.

---

*Built on top of the excellent [rclone](https://rclone.org/) project.*
