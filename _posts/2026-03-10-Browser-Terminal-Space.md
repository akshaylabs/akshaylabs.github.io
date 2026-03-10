---
layout: post
title: "Browser Terminal Space — Full Linux Shell in Your Browser via ttyd"
date: 2026-03-10
description: "A HuggingFace Space that serves a full interactive Linux terminal directly in the browser using ttyd — no SSH client, no local setup, just open and type."
tags: [docker, ttyd, huggingface, terminal, browser, devops, python]
---

# 🟢 Browser Terminal Space

> A **HuggingFace Space** that serves a fully interactive Linux terminal **directly in your browser** using [ttyd](https://github.com/tsl0922/ttyd) — no SSH client, no gsocket, no local tooling. Just open the Space URL and start typing.

---

## ✨ Features

- 🌐 **Browser-native terminal** — ttyd streams a real shell over WebSocket; works in any modern browser
- 🎨 **Catppuccin Mocha theme** — dark, easy-on-the-eyes colour palette baked into the launch command
- 🔠 **16px font** — comfortable default size, adjustable per-session in the browser
- 👤 **Non-root `user` shell** — drops into a clean `bash` session with passwordless `sudo`
- 💬 **Custom prompt & welcome message** — coloured `PS1` and a greeting on every login
- 🐳 **Single-file deployment** — two files, one `docker run`, live in under a minute

---

## 🗂 Project Structure

```
.
├── Dockerfile        # Container: installs ttyd, creates user, sets prompt
└── entrypoint.sh     # Runtime: launches ttyd on port 7860 with theme + flags
```

---

## ⚙️ How It Works

### Architecture Overview

```
Browser (you)
    │
    │  HTTP/WebSocket  →  port 7860
    ▼
HuggingFace Space (Docker)
    │
    └── ttyd --port 7860 --writable su - user
                │
                └── /bin/bash  (user@hf-space)
```

1. **ttyd** is a lightweight C binary that exposes any command as a WebSocket terminal.
2. The container launches `ttyd` as the foreground process (via `exec`), which takes over PID 1.
3. ttyd runs `su - user` as the terminal command — every browser connection gets a fresh `bash` session as the non-root `user`.
4. HuggingFace's health check hits port `7860` over HTTP — ttyd serves both the terminal UI and the WebSocket on the same port, satisfying the check automatically.

---

## 🎨 Terminal Theme

The `entrypoint.sh` applies a full **Catppuccin Mocha** colour palette via ttyd's `-t theme=` flag:

| Element | Colour |
|---|---|
| Background | `#1e1e2e` |
| Foreground | `#cdd6f4` |
| Cursor | `#f5c2e7` |
| Red | `#f38ba8` |
| Green | `#a6e3a1` |
| Blue | `#89b4fa` |
| Magenta | `#cba6f7` |
| Cyan | `#89dceb` |

To change the theme, replace the JSON object in the `-t 'theme=...'` argument with any [xterm.js-compatible theme](https://github.com/mbadolato/iTerm2-Color-Schemes).

---

## 🚀 Deployment

### Docker (local test)

```bash
docker build -t browser-terminal .
docker run --rm -p 7860:7860 browser-terminal
```

Open [http://localhost:7860](http://localhost:7860) — a terminal appears immediately.

### HuggingFace Spaces

1. Create a new Space with the **Docker** SDK.
2. Push `Dockerfile` and `entrypoint.sh`.
3. Wait for the build to complete.
4. Click **Open in new tab** (or visit the Space URL directly) — the terminal loads in the browser.

No secrets, no env vars, no configuration needed.

---

## 🛠 `Dockerfile` Walkthrough

| Section | Details |
|---|---|
| **Base image** | `python:3.9` (Debian-based; Python included for scripting tasks in the shell) |
| **ttyd install** | Pre-built binary fetched from GitHub releases — no compilation needed |
| **`user` account** | UID `1000`, password `devpassword`, passwordless `sudo` |
| **Prompt** | Coloured `PS1`: green username + blue working directory |
| **Welcome message** | `echo "🟢 Welcome to your HF Dev Space!"` appended to `.bashrc` |
| **Exposed port** | `7860` only — ttyd handles both HTTP and WebSocket on this single port |

---

## 🛠 `entrypoint.sh` Walkthrough

| Flag | Purpose |
|---|---|
| `--port 7860` | Bind to HuggingFace's required port |
| `--writable` | Enable keyboard input (without this the terminal is read-only) |
| `-t fontSize=16` | Default font size |
| `-t 'theme=...'` | Full Catppuccin Mocha colour scheme |
| `su - user` | The command ttyd runs per connection — drops into non-root `bash` |
| `exec` | Replaces the shell process with ttyd, making it PID 1 (clean signals) |

---

## 🔒 Security Notes

- The `user` account has the password `devpassword` — **change this** in the `Dockerfile` before any shared or production deployment:
  ```dockerfile
  RUN echo "user:YOUR_STRONG_PASSWORD" | chpasswd
  ```
- ttyd serves the terminal **unauthenticated** by default. To add HTTP Basic Auth:
  ```bash
  exec ttyd --port 7860 --writable --credential admin:yourpassword su - user
  ```
- HuggingFace Spaces are public by default — consider setting the Space to **Private** if the terminal should be personal-access only.

---

## 🔄 Comparison with SSH Dev Space

| Feature | Browser Terminal Space | SSH Dev Space |
|---|---|---|
| Access method | Browser (HTTP/WS) | SSH client + gsocket |
| Local tools needed | None | `gs-netcat` |
| Auth | HTTP Basic (optional) | SSH key-pair |
| Port exposure | `7860` public | No public port |
| Per-session secret | ❌ | ✅ (random on boot) |
| Best for | Quick access, demos | Secure long-term dev |

---

## 📦 Download Source

Get both project files as a single archive:

➡️ **[⬇️ Download browser-terminal-space.zip](./img/browser-terminal-space.zip)**

Includes: `Dockerfile`, `entrypoint.sh`

---

## 📄 License

MIT — use freely, attribution appreciated.
