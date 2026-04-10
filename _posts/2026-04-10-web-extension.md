---
layout: post
title: "Building a YouTube Video Cropper Chrome Extension from Scratch"
date: 2026-04-10 10:00:00 +0530
categories: [chrome-extension, javascript, youtube, browser]
tags: [chrome-extension, javascript, manifest-v3, youtube, css, content-script, popup, browser-extension]
author: "Akshay"
description: "A complete walkthrough of a Manifest V3 Chrome extension that crops the bottom of YouTube videos using CSS clip-path and translateY — hiding watermarks, banners, and ticker bars in both normal and fullscreen modes."
image: /assets/img/yt-cropper-banner.png
---

# ✂️ Building a YouTube Video Cropper Chrome Extension

> **Clip it. Shift it. Hide it.** A lightweight Manifest V3 Chrome extension that crops the bottom of YouTube videos to hide watermarks, banners, and embedded overlays — in both normal and fullscreen mode.

---

## Table of Contents

- [Overview](#overview)
- [How It Works — The Core Idea](#how-it-works--the-core-idea)
- [File Structure](#file-structure)
- [manifest.json — Extension Blueprint](#manifestjson--extension-blueprint)
- [content.js — The CSS Injector](#contentjs--the-css-injector)
- [popup.html — The Control Panel UI](#popuphtml--the-control-panel-ui)
- [popup.js — UI Logic & State Management](#popupjs--ui-logic--state-management)
- [Data Flow: How All Four Files Connect](#data-flow-how-all-four-files-connect)
- [The Fullscreen Problem & Its Solution](#the-fullscreen-problem--its-solution)
- [Storage & Persistence](#storage--persistence)
- [Installing the Extension Locally](#installing-the-extension-locally)
- [Download Source Files](#download-source-files)

---

## Overview

This is a **Manifest V3 Chrome extension** with four files:

| File | Role |
|---|---|
| `manifest.json` | Declares extension metadata, permissions, and script injection rules |
| `content.js` | Runs inside YouTube pages; injects and removes CSS to crop the video |
| `popup.html` | The extension's popup UI — toggle, slider, presets, preview |
| `popup.js` | Drives all UI interactions and syncs state to storage and the content script |

The entire crop effect is achieved with just two CSS properties — `clip-path` and `transform: translateY` — applied directly to YouTube's `<video>` element via an injected `<style>` tag.

---

## How It Works — The Core Idea

Cropping a video element sounds simple: clip the bottom. But there's a catch — if you clip without compensating, the visible portion of the video shifts upward inside its container, leaving an ugly gap at the bottom of the player. The fix is elegant:

```
1. clip-path: inset(0px 0px Npx 0px)   → clips N pixels from the bottom
2. transform: translateY(N/2 px)        → shifts the video DOWN by half the crop
                                           to re-center the visible portion
```

Shifting by exactly half the cropped amount re-centers the remaining visible area within the original bounding box. The player dimensions stay the same — only the video's rendered content changes.

```
Before crop:          After clip only:      After clip + shift:
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│              │      │              │      │              │
│   VIDEO      │      │   VIDEO      │      │   VIDEO      │
│   CONTENT    │      │   CONTENT    │      │   CONTENT    │
│              │      │              │      │              │
│▓▓▓▓▓▓▓▓▓▓▓▓│      └──────────────┘      └──────────────┘
│  banner/wm   │        (gap at bottom)       (re-centered ✓)
└──────────────┘
```

---

## File Structure

```
youtube-video-cropper/
├── manifest.json     # Extension config & permissions
├── content.js        # Injected into YouTube pages
├── popup.html        # Extension popup UI
├── popup.js          # Popup logic & Chrome API calls
└── icons/
    ├── icon16.png
    ├── icon48.png
    └── icon128.png
```

> **Note:** The `icons/` folder with PNG icons is required for the extension to load. You can generate simple placeholder icons or use any 16×16, 48×48, and 128×128 PNG images.

---

## manifest.json — Extension Blueprint

```json
{
  "manifest_version": 3,
  "name": "YouTube Video Cropper",
  "version": "1.0",
  "permissions": ["activeTab", "storage", "scripting"],
  "host_permissions": ["https://www.youtube.com/*"],
  "content_scripts": [
    {
      "matches": ["https://www.youtube.com/*"],
      "js": ["content.js"],
      "run_at": "document_idle"
    }
  ]
}
```

### Key Decisions Explained

**`manifest_version: 3`** — The current standard for Chrome extensions. MV3 replaced background pages with service workers, tightened CSP, and moved remote code execution restrictions. This extension doesn't need a background service worker at all — popup.js handles everything synchronously.

**Permissions:**
- `activeTab` — Allows the popup to query and message the currently active tab
- `storage` — Enables `chrome.storage.local` for persisting crop settings across browser restarts
- `scripting` — Required to programmatically inject `content.js` into a tab when the popup opens on an already-loaded YouTube page that didn't receive the content script at page load

**`host_permissions: ["https://www.youtube.com/*"]`** — Restricts the extension to YouTube only. Without this, `content_scripts` matching and `storage` access would still work, but sending messages to the tab would be blocked.

**`run_at: "document_idle"`** — Content script runs after the page's DOM is fully parsed and initial scripts have run. This ensures `document.body` exists when the `MutationObserver` is registered.

---

## content.js — The CSS Injector

This script runs silently inside every YouTube page. It has one job: inject or remove a `<style>` tag that applies the crop CSS to the video element.

### Style Injection

```javascript
function getInjectedStyle(cropPx) {
  return `
    .html5-video-container video,
    #movie_player video {
      clip-path: inset(0px 0px ${cropPx}px 0px) !important;
      transform: translateY(${Math.round(cropPx / 2)}px) !important;
      transform-origin: center top !important;
      transition: none !important;
    }
  `;
}
```

The style targets two selectors to cover both the normal player (`.html5-video-container video`) and fullscreen mode (`#movie_player video`). `!important` overrides any existing YouTube transforms. `transition: none` prevents any animated flicker when the crop value changes.

### Style Tag Management

Rather than appending a new `<style>` tag on every update, the script reuses a single one identified by `id="yt-crop-injected-style"`:

```javascript
function injectStyle(cropPx) {
  let el = document.getElementById(STYLE_ID);
  if (!el) {
    el = document.createElement('style');
    el.id = STYLE_ID;
    document.head.appendChild(el);
  }
  el.textContent = getInjectedStyle(cropPx);  // update in-place
}
```

This is more efficient and avoids style tag accumulation on long sessions.

### MutationObserver — Surviving YouTube Navigation

YouTube is a Single Page Application (SPA). When you navigate from one video to another, the DOM is partially replaced without a full page reload — which means injected `<style>` tags can be lost. The `MutationObserver` detects these DOM changes and re-applies the crop:

```javascript
const observer = new MutationObserver(() => {
  if (settings.enabled) applyToVideo();
});
observer.observe(document.body, { childList: true, subtree: true });
```

### Fullscreen Handling

Entering and exiting fullscreen triggers layout recalculations that can displace the injected transform. The script listens to both standard and WebKit-prefixed fullscreen events and re-applies the style with a short delay to let the browser finish its layout pass:

```javascript
document.addEventListener('fullscreenchange', () => {
  setTimeout(applyToVideo, 200);
});
document.addEventListener('webkitfullscreenchange', () => {
  setTimeout(applyToVideo, 200);
});
```

### Message Listener

The content script accepts two message types from the popup:

```javascript
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'UPDATE_CROP') {
    settings = { ...settings, ...msg.settings };
    applyToVideo();
    sendResponse({ ok: true });
  }
  if (msg.type === 'GET_STATUS') {
    sendResponse({ settings });
  }
  return true; // keeps the message channel open for async sendResponse
});
```

`return true` at the end is critical — without it, the message channel closes before `sendResponse` can be called asynchronously.

---

## popup.html — The Control Panel UI

The popup is a self-contained 300px wide panel styled to match YouTube's dark aesthetic. It has four functional zones:

```
┌────────────────────────────────┐
│  🎬  YouTube Video Cropper     │  ← Header (red, branded)
│      Hide bottom banners       │
├────────────────────────────────┤
│  Enable cropping      [toggle] │  ← Toggle row
├────────────────────────────────┤
│  [video preview] Crop: 40px    │  ← Live preview bar
│                 ~8% at 1080p   │
├────────────────────────────────┤
│  CROP AMOUNT (PIXELS)          │  ← Crop section
│  [════════●════] [40px]        │    Range slider + badge
│  [20px][40px][60px][80px]...   │    Preset buttons
│  Custom value: [____]          │    Manual number input
├────────────────────────────────┤
│  ● Active — cropping 40px  [Reset] │  ← Footer status
└────────────────────────────────┘
```

### Design Highlights

**Dark YouTube theme:** Background `#0f0f0f`, accent `#ff0000` (YouTube red), subtle card backgrounds at `#1a1a1a` and `#2a2a2a`. The color palette is intentionally pulled from YouTube's own design system so the extension feels native.

**Custom range slider:** The default browser range input is replaced via `-webkit-appearance: none` with a custom 4px track and 16px red thumb:

```css
input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #ff0000;
  cursor: pointer;
  transition: transform 0.15s;
}
input[type="range"]::-webkit-slider-thumb:hover {
  transform: scale(1.2);
}
```

**Live crop preview:** A miniature 80×45px video thumbnail visually shows how much of the bottom will be cropped. A red overlay `div` (`.crop-indicator`) grows from the bottom as the crop value increases, giving instant visual feedback without needing to switch to a real YouTube tab.

**Disabled overlay:** When cropping is toggled off, the entire crop section fades to 40% opacity and becomes non-interactive via a single CSS class:

```css
.disabled-overlay {
  opacity: 0.4;
  pointer-events: none;
}
```

**Status dot:** A 6px circle in the footer glows green (`#4caf50` with `box-shadow`) when active, and is dark grey when inactive — a subtle but clear state indicator.

---

## popup.js — UI Logic & State Management

This script is the brain of the popup. It manages state, drives all UI updates, and bridges the popup to the content script via Chrome's messaging APIs.

### State Object

```javascript
let settings = {
  enabled: false,  // whether cropping is active
  bottomPx: 40     // how many pixels to crop from the bottom
};
```

This object is the single source of truth. Every UI update reads from it; every user interaction writes to it before calling `updateUI()` and `saveAndSend()`.

### `updateUI()` — Reactive Rendering

A single function updates every visual element based on the current `settings` object:

```javascript
function updateUI() {
  const px = settings.bottomPx;

  cropSlider.value = Math.min(px, 200);   // slider caps at 200
  valueBadge.textContent = px + 'px';
  manualInput.value = px;

  // Visual preview height (proportional to crop amount)
  const heightPx = Math.max(3, Math.round((px / 200) * 22));
  cropIndicator.style.height = heightPx + 'px';

  previewTitle.textContent = `Crop: ${px}px from bottom`;
  previewDetail.textContent = getDescription(px);

  // Highlight matching preset button
  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.classList.toggle('active', parseInt(btn.dataset.px) === px);
  });

  // Toggle overlay and status
  cropSection.classList.toggle('disabled-overlay', !settings.enabled);
  statusDot.classList.toggle('on', settings.enabled);
  statusText.textContent = settings.enabled
    ? `Active — cropping ${px}px`
    : 'Inactive';
}
```

All three input methods (slider, preset buttons, manual number input) funnel through a single `setCrop(px)` function that normalises the value and calls `updateUI()` + `saveAndSend()`:

```javascript
function setCrop(px) {
  settings.bottomPx = Math.max(1, Math.min(400, parseInt(px) || 40));
  updateUI();
  saveAndSend();
}
```

### `saveAndSend()` — Storage + Messaging

Every settings change triggers two actions simultaneously:

```javascript
function saveAndSend() {
  // 1. Persist to storage (survives popup close and browser restart)
  chrome.storage.local.set({ [STORAGE_KEY]: settings });

  // 2. Send live update to the active YouTube tab
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (!tabs[0]?.url?.includes('youtube.com')) return;

    chrome.tabs.sendMessage(tab.id, {
      type: 'UPDATE_CROP',
      settings: settings
    }).catch(() => {
      // Content script not yet injected — inject it first, then message
      chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ['content.js']
      }).then(() => {
        chrome.tabs.sendMessage(tab.id, { type: 'UPDATE_CROP', settings });
      });
    });
  });
}
```

The `.catch()` fallback handles a real edge case: if the user pins the extension toolbar button and opens the popup on a YouTube tab that was loaded *before* the extension was installed, `content.js` was never injected. The fallback programmatically injects it via `chrome.scripting.executeScript`, then sends the message.

### Contextual Descriptions

The preview text changes based on crop size to give users meaningful guidance:

```javascript
const DESCRIPTIONS = {
  small:  'Removes a thin strip. Good for small watermarks.',         // ≤ 30px
  medium: 'Removes ~8% of video at 1080p. Good for most banners.',   // 31–70px
  large:  'Removes a significant portion. Good for ticker bars.',     // 71–120px
  xlarge: 'Removes a large strip. Use for very tall overlays.'        // > 120px
};
```

---

## Data Flow: How All Four Files Connect

```
User opens popup
      │
      ▼
popup.js loads saved settings from chrome.storage.local
      │
      ▼
updateUI() renders current state to popup.html controls
      │
User changes slider / preset / toggle / manual input
      │
      ▼
setCrop(px) or toggle handler updates settings object
      │
      ├──► updateUI()          → updates popup.html visually (instant)
      │
      └──► saveAndSend()
               │
               ├──► chrome.storage.local.set()   → persisted for next session
               │
               └──► chrome.tabs.sendMessage()
                          │
                          ▼
                    content.js (running in YouTube tab)
                    receives UPDATE_CROP message
                          │
                          ▼
                    applyToVideo()
                          │
                    ┌─────┴──────┐
                    │            │
               enabled?        not enabled?
                    │            │
              injectStyle()  removeStyle()
            (adds/updates     (removes
            <style> tag)      <style> tag)
```

The flow is entirely **event-driven** — nothing polls. Changes propagate from the popup to the page in milliseconds via Chrome's message passing.

---

## The Fullscreen Problem & Its Solution

YouTube's fullscreen mode is the trickiest part of this extension. When you go fullscreen, YouTube uses `position: absolute` with calculated `top` and `left` values to center the video inside the full-screen container. If you naively apply `transform: translateY` after YouTube has already positioned the element, the centering math breaks and you get a visually off-center video.

The extension solves this by using the same CSS rule for both normal and fullscreen — it doesn't try to detect fullscreen mode or apply different rules. Instead, it lets YouTube center the video normally, then applies `translateY(cropPx / 2)` on top. Since YouTube's centering already places the video correctly, the additional half-crop shift re-centers the *cropped* portion consistently, regardless of whether the player is fullscreen or not.

The `fullscreenchange` event listeners exist only to re-trigger `applyToVideo()` with a 200ms delay — this handles cases where the fullscreen transition temporarily removes or resets the video element's styles.

---

## Storage & Persistence

Settings are stored in `chrome.storage.local` under the key `'yt_crop_settings'`:

```json
{
  "yt_crop_settings": {
    "enabled": true,
    "bottomPx": 60
  }
}
```

`chrome.storage.local` is preferred over `localStorage` for extensions because:
- It's accessible from both the popup context and the content script context
- It persists across popup closes (popups are destroyed every time they close)
- It survives browser restarts
- It supports async callbacks, fitting naturally into Chrome's extension APIs

Both `content.js` (on page load via `loadAndApply()`) and `popup.js` (on popup open) independently read from storage to restore the last-used settings.

---

## Installing the Extension Locally

Since this extension isn't on the Chrome Web Store, you install it as an **unpacked extension**:

```
1. Download and unzip the source files (link below)
2. Add icon PNG files to an icons/ subfolder:
      icons/icon16.png   (16×16)
      icons/icon48.png   (48×48)
      icons/icon128.png  (128×128)
3. Open Chrome and navigate to: chrome://extensions
4. Enable "Developer mode" (toggle in the top-right corner)
5. Click "Load unpacked"
6. Select the folder containing manifest.json
7. The extension icon will appear in your toolbar
8. Navigate to any YouTube video and click the extension icon
```

> **Tip:** Any YouTube-red colored 128×128 PNG works as a placeholder icon. The extension will load without errors as long as all three icon sizes exist.

### Reloading After Changes

If you edit any of the files:
1. Go to `chrome://extensions`
2. Find "YouTube Video Cropper"
3. Click the refresh icon (↺) on the extension card
4. Reload the YouTube tab

---

## Download Source Files

All four extension files are available as a single zip archive:

> 📦 **[Download yt_cropper.zip](./img/yt-crop-extension.zip)**

**Contents:**

| File | Description |
|---|---|
| `manifest.json` | Extension declaration, permissions, and content script config |
| `content.js` | CSS injector; runs inside YouTube pages |
| `popup.html` | Extension popup UI with dark YouTube-themed design |
| `popup.js` | UI logic, state management, and Chrome API interactions |

> **Remember:** After unzipping, create an `icons/` folder with `icon16.png`, `icon48.png`, and `icon128.png` before loading the extension.

---

## Notes

- The extension only activates on `https://www.youtube.com/*` — it has no access to any other website.
- No data is sent anywhere. All settings are stored locally in your browser via `chrome.storage.local`.
- The crop is purely visual — it doesn't affect video downloading, quality, or playback in any way.
- If YouTube updates its player DOM structure (class names or IDs), the CSS selectors in `content.js` may need updating.

---

*Built with Manifest V3 · Chrome Extensions API · Pure CSS · No dependencies*
