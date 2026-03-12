---
layout: post
title: "Classplus Test Solution Fetcher — A Complete Technical Guide"
date: 2026-03-12
categories: [python, automation, reverse-engineering]
tags: [classplus, api, python, requests, jwt, automation]
description: "A deep-dive into four Python scripts that automate fetching test questions and solutions from the Classplus student CMS platform, progressing from a simple proof-of-concept to a robust, production-ready tool."
---

# 🎓 Classplus Test Solution Fetcher

> A suite of Python scripts for fetching test questions and solutions from the Classplus student CMS platform — evolving from a simple two-step proof-of-concept into a robust, bulk-processing automation tool.

---

## 📖 Table of Contents

- [Overview](#overview)
- [How Authentication Works](#how-authentication-works)
- [The API Flow](#the-api-flow)
- [Script Breakdown](#script-breakdown)
  - [1. `instant-fetch.py` — The Proof of Concept](#1-instant-fetchpy--the-proof-of-concept)
  - [2. `submit-autosubmit.py` — The Optimized Single-Test Fetcher](#2-submit-autosubmitpy--the-optimized-single-test-fetcher)
  - [3. `submit-gracefully.py` — The Reliable Fetcher](#3-submit-gracefullypy--the-reliable-fetcher)
  - [4. `bulk-processor.py` — The Production-Grade Bulk Processor](#4-bulk-processorpy--the-production-grade-bulk-processor)
- [Key Concepts Explained](#key-concepts-explained)
- [Setup & Usage](#setup--usage)
- [Download Source Files](#download-source-files)

---

## Overview

The Classplus CMS exposes a RESTful API that the student web app uses to start tests, submit answers, and retrieve solutions. This project reverse-engineers that API to automate the process of fetching all test questions and their model solutions — useful for students who want to review material offline or study without time pressure.

The four scripts in this repo represent an **iterative development journey**, each one addressing shortcomings of the previous.

---

## How Authentication Works

All requests to the Classplus API require a **JWT (JSON Web Token)** passed as the `x-cms-access-token` header.

```
x-cms-access-token: eyJhbGciOiJIUzM4NCIsInR5cCI6IkpXVCJ9...
```

A JWT has three parts separated by dots: a **header**, a **payload**, and a **signature**. The payload (the middle part) is Base64-encoded and contains fields like:

| Field | Description |
|---|---|
| `userId` | Your Classplus user account ID |
| `studentId` | Your enrollment ID |
| `testId` | The specific test this token unlocks |
| `courseId` | The course the test belongs to |
| `exp` | Expiry timestamp (Unix epoch) |

### How to Get Your Token

1. Open the Classplus test in **Chrome**.
2. Press `F12` → go to the **Network** tab.
3. Click **Start Test** in the UI.
4. Find the `start` request to `cms-gcp.classplusapp.com`.
5. Under **Request Headers**, copy the value of `x-cms-access-token`.

> ⚠️ **Tokens expire.** The `exp` field in the JWT payload tells you when. Most tokens are valid for ~7 days. If you get `401 Unauthorized` errors, you need to refresh your token.

---

## The API Flow

Every script follows the same fundamental sequence of HTTP calls:

```
POST /test/start
      ↓
  (optional) POST /question/submit  ← repeat per question
      ↓
POST /test/evaluate
      ↓
GET  /test/{testId}/student/{studentTestId}/solutions
```

### Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/student/api/v2/test/start` | `POST` | Begins a test session, returns questions |
| `/student/api/v2/question/submit` | `POST` | Saves a single question's answer |
| `/student/api/v2/test/evaluate` | `POST` | Finalizes/submits the test |
| `/student/api/v2/test/{id}/student/{sid}/solutions` | `GET` | Retrieves full solutions with explanations |

---

## Script Breakdown

### 1. `instant-fetch.py` — The Proof of Concept

**Purpose:** Validates the core hypothesis — can we start a test, immediately evaluate it with no answers, and still get the solutions?

**Approach:** The instant method — skips question submission entirely.

```python
# Start the test
response = requests.post(start_url, json={"testId": TEST_ID_PAYLOAD}, headers=base_headers)
student_test_id = start_data['data']['studentTestId']

# Immediately evaluate with zero answers
evaluate_payload = {
    "testId": test_id,
    "studentTestId": student_test_id,
    "questions": [],        # No answers submitted at all
    "timeTaken": 5000,      # Fake 5-second duration
    "autoSubmitType": 1     # Force-submit flag
}
requests.post(evaluate_url, json=evaluate_payload, headers=headers)

# Fetch solutions
requests.get(solution_url, headers=headers)
```

**Why it might fail:** Some tests validate that at least one question was interacted with before solutions are unlocked. Submitting zero questions can result in the solutions endpoint returning an error or empty data.

**Output:** `solutions_{testId}.json`

---

### 2. `submit-autosubmit.py` — The Optimized Single-Test Fetcher

**Purpose:** Adds a "session activation" step to work around the zero-question problem, while keeping things fast.

**Key improvement:** Submits **only the first question** (with a blank answer) before evaluating. This activates the session without wasting time looping over all questions.

```python
# Submit ONLY the first question to activate the session
first_question = start_data['data']['test']['sections'][0]['questions'][0]

question_payload = {
    "questions": [{
        "_id": first_question['_id'],
        "selectedOptions": [],   # Blank answer
        "markForReview": False,
        "timeTaken": 1500
    }],
    "timeTaken": 1500
}
requests.post(submit_url, json=question_payload, headers=headers_submit)

# Now force evaluate with the "switched app" auto-submit reason
evaluate_payload = {
    "reasonForSubmit": "Student switched from test for 3 times",
    "autoSubmitType": 3   # Different flag — simulates tab-switching penalty
}
```

**`autoSubmitType` values explained:**

| Value | Meaning |
|---|---|
| `1` | Standard student-initiated submit |
| `3` | Auto-submit due to app-switching (cheating detection) |

Using `autoSubmitType: 3` with the corresponding `reasonForSubmit` string mimics the platform's own cheating-detection auto-submit, which appears to bypass some solution-lock checks.

**Output:** `test_solutions_optimized.json`

---

### 3. `submit-gracefully.py` — The Reliable Fetcher

**Purpose:** Maximizes compatibility by fully mimicking a real student completing the test.

**Key improvement:** Submits **every single question** individually before evaluating. This is the most "legitimate" flow and is least likely to be rejected by the server.

```python
total_time_taken = 0

for i, q in enumerate(questions_from_start):
    time_per_question = 1500  # 1.5 seconds per question
    total_time_taken += time_per_question

    question_payload = {
        "questions": [{
            "_id": q['_id'],
            "solution": "",            # Blank answer
            "selectedOptions": [],
            "fillUpsAnswers": [],
            "markForReview": False,
            "timeTaken": time_per_question
        }],
        "timeTaken": total_time_taken  # Cumulative time
    }
    requests.post(submit_url, json=question_payload, headers=headers_submit)
    print(f"  -> Submitting Q{i+1}/{len(questions_from_start)}... Status: {submit_response.status_code}")

    if submit_response.status_code != 200:
        print("  ⚠️  Submission failed. Stopping.")
        break
```

**Trade-off:** Slower than `submit-autosubmit.py` for tests with many questions, but much more reliable. The cumulative `timeTaken` counter is carefully maintained to look realistic.

**Output:** `test_solutions.json`

---

### 4. `bulk-processor.py` — The Production-Grade Bulk Processor

**Purpose:** Processes an entire course worth of tests automatically, with state management, retries, and resumability.

This is the most sophisticated script and introduces several new systems:

#### 🗂️ Master File Architecture

Instead of hardcoding a single test ID, `bulk-processor.py` works from a `master_course.json` file — a pruned, enhanced version of the raw course content tree.

```
course.json (raw, from API)
      ↓  --init flag
master_course.json (tests only, with processingInfo)
      ↓  normal run
outputs/{testId}/questions.json
outputs/{testId}/solutions.json
```

The `--init` process uses a recursive `_prune_and_enhance_node()` function that walks the course content tree and:
- **Keeps** items with `contentType == 4` (tests) and adds a `processingInfo` block
- **Discards** items with other content types (videos, PDFs, etc.)
- **Discards** folders that become empty after pruning
- **Extracts** the JWT token from the test's URL automatically

```python
def _prune_and_enhance_node(node):
    # Keep & enhance tests
    if node.get("contentType") == 4 and node.get("testId") and node.get("URL"):
        node["processingInfo"] = {
            "status": "pending",
            "token": None,           # Extracted from URL
            "questionsPath": None,
            "solutionsPath": None,
            "lastAttemptTimestamp": None,
            "lastError": None
        }
        # Extract token from the test URL's query string
        parsed_url = urlparse(node["URL"])
        token = parse_qs(parsed_url.query).get('token', [None])[0]
        node["processingInfo"]["token"] = token
        return node

    # Discard non-folders (videos, PDFs)
    if node.get("contentType") != 1:
        return None

    # Recursively prune folder children
    pruned_children = [_prune_and_enhance_node(c) for c in node.get("children", [])]
    pruned_children = [c for c in pruned_children if c]  # Remove Nones
    if pruned_children:
        node["children"] = pruned_children
        return node
    return None  # Discard empty folders
```

#### 🔁 Retry Mechanism

All HTTP requests go through `make_request_with_retry()`:

```python
def make_request_with_retry(method, url, **kwargs):
    for attempt in range(MAX_RETRIES):  # Default: 3
        try:
            response = requests.request(method, url, **kwargs, timeout=30)
            if response.ok:   # Any 2xx status
                return response
            else:
                print(f"⚠️ Failed with {response.status_code}. Retrying ({attempt+1}/{MAX_RETRIES})")
        except requests.exceptions.RequestException as e:
            print(f"❌ Exception: {e}. Retrying ({attempt+1}/{MAX_RETRIES})")
        time.sleep(RETRY_DELAY_SECONDS)  # Default: 5s between retries
    return None
```

#### 💾 Atomic File Saves

To prevent data corruption if the script is interrupted mid-write:

```python
def save_master_file(data, filepath):
    temp_filepath = filepath + ".tmp"
    with open(temp_filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(temp_filepath, filepath)  # Atomic on most OS
```

Write to a `.tmp` file first, then atomically replace the real file. This means a crash during a write will leave either the old complete file or the new complete file — never a half-written one.

#### 🛑 Graceful Interruption

```python
try:
    for i, test_obj in enumerate(tests_to_process):
        # ... process test ...
        save_master_file(master_data, args.master_file)  # Save after EVERY test

except KeyboardInterrupt:
    print("\n❗️ Interrupted. Saving current state before exiting...")
    save_master_file(master_data, args.master_file)
    sys.exit(1)
```

Press `Ctrl+C` at any time and all progress up to the last completed test is saved. The next run will automatically skip already-completed tests.

#### 🎛️ CLI Flags

```
python bulk-processor.py --init                          # Build master file from course.json
python bulk-processor.py                                 # Fetch questions only (safe, no submit)
python bulk-processor.py --fetch-answers                 # Submit + evaluate + get solutions (graceful)
python bulk-processor.py --fetch-answers --quick-submit  # Submit + evaluate + get solutions (fast)
python bulk-processor.py --master-file custom.json       # Use a custom master file path
```

---

## Key Concepts Explained

### Why Submit Blank Answers?

The solutions endpoint only becomes accessible after a test has been "evaluated" (submitted). By submitting blank answers and forcing evaluation, the script unlocks the solutions without requiring correct answers. The `selectedOptions: []` and `solution: ""` fields represent unanswered questions.

### The `timeTaken` Field

All submit and evaluate payloads include a `timeTaken` field in milliseconds. The scripts use realistic-looking values (1500ms per question = 1.5 seconds) to avoid triggering any server-side anomaly detection. The evaluate payload adds extra time (`total + 5000ms`) to account for the time "spent" on the submission UI.

### `accept-encoding: br` (Brotli)

`bulk-processor.py` requests Brotli-compressed responses (`br`). This requires the `brotli` Python package. If you remove this header, the server falls back to gzip, which `requests` handles automatically — but responses will be larger.

### Atomic Writes with `os.replace()`

`os.replace()` is atomic on POSIX systems (Linux/macOS) and is atomic on Windows for files on the same drive. This is critical for the master file, which is the single source of truth — corruption here would mean losing all progress tracking.

---

## Setup & Usage

### Prerequisites

```bash
pip install requests brotli
```

### Quick Start (Single Test)

Use `submit-gracefully.py` for the most reliable experience:

1. Open the relevant Classplus test in Chrome.
2. Copy your `x-cms-access-token` from DevTools → Network → Headers.
3. Edit the `ACCESS_TOKEN` and `testId` constants in the script.
4. Run:

```bash
python submit-gracefully.py
```

The solution will be saved to `test_solutions.json`.

### Bulk Processing with `bulk-processor.py`

1. **Export your course data** from the Classplus API and save it as `course.json`.
2. **Initialize** the master file:

```bash
python bulk-processor.py --init
```

3. **Fetch questions only** (no test submissions — safe for browsing):

```bash
python bulk-processor.py
```

4. **Fetch full solutions** (submits tests to unlock answers):

```bash
# Graceful mode (reliable, slower)
python bulk-processor.py --fetch-answers

# Quick mode (faster, may fail on strict tests)
python bulk-processor.py --fetch-answers --quick-submit
```

5. **Resume after interruption** — just re-run the same command. Tests already marked `completed` are skipped automatically.

### Output Structure

```
outputs/
├── {testId_1}/
│   ├── questions.json     # Full test data from the start API
│   └── solutions.json     # Solutions with explanations
├── {testId_2}/
│   ├── questions.json
│   └── solutions.json
└── ...
master_course.json          # Updated with status of each test
```

---

## Script Comparison

| Feature | `instant-fetch.py` | `submit-autosubmit.py` | `submit-gracefully.py` | `bulk-processor.py` |
|---|---|---|---|---|
| Questions submitted | 0 | 1 (first only) | All | All (configurable) |
| Speed | ⚡ Fastest | ⚡ Fast | 🐢 Slow | Configurable |
| Reliability | ⚠️ Low | ✅ Medium | ✅ High | ✅ High |
| Retry logic | ❌ | ❌ | ❌ | ✅ (3 retries) |
| Bulk processing | ❌ | ❌ | ❌ | ✅ |
| Resumable | ❌ | ❌ | ❌ | ✅ |
| Progress tracking | ❌ | ❌ | ❌ | ✅ |
| CLI interface | ❌ | ❌ | ❌ | ✅ |

---

## Download Source Files

📦 **[Download all scripts as a ZIP](./assets/img/classplus-solution-fetcher.zip)**

The archive contains:
- `instant-fetch.py` — Proof of concept
- `submit-autosubmit.py` — Optimized single-test fetcher
- `submit-gracefully.py` — Reliable single-test fetcher
- `bulk-processor.py` — Production bulk processor

---

*Built for educational and personal study purposes. Always respect the platform's terms of service.*
