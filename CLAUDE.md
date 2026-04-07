# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Scriptius Audio PoC — a minimal Chrome Extension (Manifest V3) that validates dual-stream audio capture for sales call analysis. It captures tab audio from HubSpot (Unitalk Web Dialer) via `chrome.tabCapture` and the salesperson's microphone via `getUserMedia`, then displays live RMS levels in a popup.

The full spec is in `audio-poc-spec.md` (in Ukrainian).

## Architecture

- **manifest.json** — MV3 manifest with `tabCapture` and `offscreen` permissions
- **background.js** — Service Worker: coordinates offscreen document lifecycle, calls `chrome.tabCapture.getMediaStreamId()`, routes messages between popup and offscreen. Has a guard `if (msg.target === 'offscreen') return` to prevent message self-loop.
- **offscreen.html / offscreen.js** — Offscreen Document (reason: `USER_MEDIA`): receives streamId, creates tab and mic MediaStreams, runs AudioContext + AnalyserNode pipelines, sends RMS levels every 500ms. Plays tab audio back via `new Audio()` to prevent muting.
- **popup.html / popup.js** — UI: Start/Stop buttons, live RMS level bars, scrollable log. Checks mic permission before starting capture.
- **permissions.html / permissions.js** — Dedicated extension page opened in a tab to request microphone permission (offscreen documents cannot show permission dialogs).
- **icons/** — Placeholder PNGs (16/48/128px, green #4CAF50 with "S")

### Message flow

```
Popup checks mic permission:
  if not granted → opens permissions.html in new tab → user clicks Allow → tab closes
  permissions.html → background: { type: "mic_permission_granted" } → proxied to popup

Popup → background: { type: "start_tab_capture" }
background → creates offscreen doc, gets streamId via chrome.tabCapture.getMediaStreamId()
background → offscreen: { type: "start_tab_capture", target: "offscreen", streamId }
offscreen → background → popup: { type: "audio_levels", tab_rms, mic_rms } (every 500ms)
Popup → background → offscreen: { type: "stop_capture" }
```

## Known Gotchas (Chrome Extension API)

- **tabCapture mutes the tab** — `chrome.tabCapture.getMediaStreamId()` + `getUserMedia` silences the original tab audio. Fix: create `new Audio()` with `srcObject = tabStream` and call `.play()` in offscreen.js.
- **Offscreen can't show permission prompts** — `getUserMedia` for microphone in offscreen document fails with "Permission dismissed" because there's no visible UI. Fix: request mic permission from a dedicated extension page (`permissions.html`) opened in a real tab before starting capture.
- **Background message self-loop** — `chrome.runtime.sendMessage()` in the service worker is received by its own `onMessage` listener. Fix: tag messages with `target: 'offscreen'` and guard with `if (msg.target === 'offscreen') return` at the top of the background listener.
- **RMS baseline** — Mic RMS ~0.0001-0.0006 is normal background noise (not silence). Speech produces RMS 0.01+. Tab RMS during audio playback: 0.01-0.08.

## Loading the Extension

1. Open `chrome://extensions/`, enable Developer mode
2. "Load unpacked" → select the project folder
3. Open HubSpot tab with Unitalk active (or any tab with audio for testing), click extension icon
4. To reload after code changes: click the refresh icon on the extension card in `chrome://extensions/`
