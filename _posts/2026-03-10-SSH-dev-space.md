---
layout: post
title: "SSH Dev Space — Remote Terminal on HuggingFace Spaces via gsocket"
date: 2026-03-10
description: "Turn a HuggingFace Space into a fully-featured SSH remote development environment, accessible from anywhere through gsocket peer-to-peer tunnelling — no open ports, no firewall rules."
tags: [docker, ssh, gsocket, huggingface, devops, remote-dev, python]
---

# 🖥️ SSH Dev Space

> Turn a **HuggingFace Space** into a fully-featured SSH remote development environment — accessible from anywhere through **gsocket peer-to-peer tunnelling**, with no open ports, no firewall rules, and no VPN.

---

## ✨ Features

- 🔐 **Key-based SSH only** — password auth is disabled; your authorized keys are baked in at build time
- 🌐 **gsocket tunnel** — punches through NAT/firewalls using the Global Socket relay network; no port-forwarding needed
- 🔑 **Random secret per boot** — a fresh 7-character secret is generated on every container start and printed to logs
- 🩺 **HTTP health-check server** — a tiny Python server on port `7860` keeps HuggingFace satisfied
- 👤 **Non-root `user` account** — passwordless `sudo`, standard `bash` shell
- 🧩 **VS Code Server ready** — one commented line in the `Dockerfile` to enable a browser IDE on port `8080`

---

## 🗂 Project Structure

```
.
├── Dockerfile        # Full container definition — SSH, gsocket, user setup
└── entrypoint.sh     # Runtime: generates secret, starts sshd + gsocket + health-check HTTP
```

---

## ⚙️ How It Works

### Architecture Overview

```
Your local machine
      │
      │  gs-netcat -s <SECRET>
      ▼
 Global Socket Relay  ◀──────────────────────────┐
                                                  │ gsocket tunnel
                                         HuggingFace Space (Docker)
                                                  │
                                         gs-netcat -l -s <SECRET>
                                                  │
                                         sshd :2222 (internal)
                                                  │
                                         /bin/bash (user@container)
```

### Step-by-step

1. **Container starts** → `entrypoint.sh` generates a random 7-letter secret (e.g. `xktpmrb`).
2. **`sshd`** launches on internal port `2222` — never exposed publicly.
3. **`gs-netcat`** connects outward to the Global Socket relay, advertising the secret. No inbound port is needed.
4. **HTTP server** starts on port `7860` — returns a simple HTML page to pass HuggingFace health checks.
5. **You connect** from your machine using the secret printed in the Space logs.

---

## 🔐 SSH Key Setup

The `Dockerfile` bakes authorized public keys directly into `/home/user/.ssh/authorized_keys` at build time. **Before deploying, replace the placeholder keys with your own.**

Generate a key pair if you don't have one:

```bash
ssh-keygen -t ed25519 -C "hf-space"
cat ~/.ssh/id_ed25519.pub
```

Then edit the `Dockerfile` — find the `ARG SSH_PUBLIC_KEY` section and substitute your public key. You can also pass it at build time:

```bash
docker build --build-arg SSH_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" .
```

---

## 🚀 Deployment

### Prerequisites

- [gsocket](https://github.com/hackerschoice/gsocket) installed on your **local machine**
- Your SSH public key ready

### Local install of gsocket

```bash
# macOS
brew install gsocket

# Linux (build from source)
git clone https://github.com/hackerschoice/gsocket.git
cd gsocket && autoreconf -fi && ./configure && make && sudo make install
```

### Docker (local test)

```bash
docker build -t ssh-dev-space .
docker run --rm -it ssh-dev-space
```

Watch the logs for a line like:

```
  SECRET: xktpmrb
  Connect from your local machine with:
  ssh -o "ProxyCommand=gs-netcat -s xktpmrb -q" user@localhost
```

### HuggingFace Spaces

1. Create a new Space with the **Docker** SDK.
2. Push `Dockerfile` and `entrypoint.sh`.
3. Open **Logs** in the Space UI — wait for the `SECRET:` line to appear.
4. Connect from your local machine (see below).

---

## 💻 Connecting

Once you have the secret from the logs:

```bash
# Interactive shell
gs-netcat -s <SECRET> -i

# SSH with ProxyCommand (gives you full SSH features: port forwarding, scp, etc.)
ssh -o "ProxyCommand=gs-netcat -s <SECRET> -q" user@localhost

# Copy a file to the space
scp -o "ProxyCommand=gs-netcat -s <SECRET> -q" myfile.txt user@localhost:/home/user/
```

Every container restart generates a **new secret** — check the logs again after a restart.

---

## 📁 `entrypoint.sh` Walkthrough

| Step | What happens |
|---|---|
| **1** | Generates `GSOCKET_SECRET` — 7 random lowercase letters from `/dev/urandom` |
| **2** | Starts `sshd -D -e` on port `2222`; verifies PID is alive |
| **3** | Starts `gs-netcat -l` forwarding to `127.0.0.1:2222`; prints the connect command |
| **4** | Launches a Python `http.server` on port `7860` as the foreground process (keeps the container alive) |

The script uses `set -e` — any failed step exits immediately with a clear error message.

---

## 🛠 `Dockerfile` Highlights

| Section | Details |
|---|---|
| **Base image** | `python:3.9` (Debian-based, gives full apt ecosystem) |
| **SSH hardening** | Port `2222`, key-auth only, PAM disabled, no empty passwords, root login by key only |
| **`user` account** | UID `1000`, passwordless `sudo`, `bash` as default shell |
| **gsocket** | Built from source — no distro package needed, works everywhere |
| **Exposed port** | Only `7860` (HTTP health-check) — SSH never hits a public port |

---

## 🧩 Optional: VS Code in Browser

Uncomment one line in the `Dockerfile` to add [code-server](https://github.com/coder/code-server) (VS Code in browser):

```dockerfile
RUN curl -fsSL https://code-server.dev/install.sh | sh
```

Then start it in `entrypoint.sh`:

```bash
code-server --bind-addr 0.0.0.0:8080 --auth none &
```

---

## ⚠️ Security Notes

- **Replace the SSH keys** in the `Dockerfile` before any real use — the placeholder keys in this repo are public examples.
- The `user` account has passwordless `sudo` — suitable for a personal dev environment; tighten this for shared deployments.
- gsocket secrets are single-use per session; there is no persistent exposure after a container restart.

---

## 📦 Download Source

Get both project files as a single archive:

➡️ **[⬇️ Download ssh-dev-space.zip](./img/ssh-dev-space.zip)**

Includes: `Dockerfile`, `entrypoint.sh`

---

## 📄 License

MIT — use freely, attribution appreciated.
