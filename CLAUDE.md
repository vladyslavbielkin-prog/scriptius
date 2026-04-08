# CLAUDE.md

## What

Scriptius — real-time dual-stream audio capture and transcription tool for sales call analysis (Ukrainian language), capturing both the client (system audio) and salesperson (microphone) as separate streams with speaker attribution.

## Architecture Overview

Three components:

- **Desktop Agent** (`scriptius-native/`) — Swift macOS app, captures system-wide audio via CoreAudio Process Tap API, serves PCM chunks over WebSocket (port 9001)
- **Server** (`server/`) — Python FastAPI backend, receives PCM audio via WebSocket `/audio`, runs VAD + Google Cloud Speech STT (Chirp), returns transcripts and VAD events
- **Webapp** (`webapp/` = `server/public/`) — vanilla JS UI served as static files by FastAPI, connects to both Agent and Server via WebSockets, captures mic audio

```
System Audio → Desktop Agent (ws://localhost:9001) → Webapp → Server (/audio WS) → Google STT → Transcript
Microphone → Webapp (getUserMedia) ────────────────────↗
```

## Stack

| Component | Technologies |
|-----------|-------------|
| Desktop Agent | Swift 5.9, CoreAudio, AudioToolbox, Network.framework, macOS 14.2+ |
| Server | Python 3.12, FastAPI, uvicorn, google-cloud-speech ≥2.38.0 |
| Webapp | Vanilla JS, Web Audio API, AudioWorklet, WebSocket, Browser SpeechRecognition |
| AI Analysis | Google Gemini (gemini-2.5-flash, gemini-2.5-flash-lite), google-genai SDK |
| Deploy | Docker, Fly.io (region: ams, app: `scriptius`) |
| Audio format | 16kHz mono 16-bit PCM (LINEAR16) throughout |

## Project Structure

```
├── scriptius-native/              # Desktop Agent (macOS)
│   ├── Package.swift              # Swift 5.9 package, macOS 14+, CoreAudio + AudioToolbox
│   └── Sources/ScriptiusAudio/
│       ├── main.swift             # Entry point; --server flag → WS mode, default → native messaging
│       ├── AudioTapManager.swift  # CoreAudio Process Tap, aggregate device, IO proc, 48→16kHz downsample
│       ├── WebSocketServer.swift  # Network.framework WS server on port 9001, broadcasts to all clients
│       └── NativeMessaging.swift  # Chrome Native Messaging protocol (4-byte length + JSON, legacy)
├── server/
│   ├── main.py                    # FastAPI app, includes audio_ws router, mounts public/ as static
│   ├── audio_ws.py                # WebSocket /audio endpoint, SpeechDetector (VAD), STT streaming (v2/v1)
│   ├── app/
│   │   ├── ai_analysis.py        # CallAnalyzer: Gemini-powered qualification, profiling, value questions
│   │   └── session.py            # CallSession: conversation state, client profile, deduplication
│   ├── requirements.txt           # fastapi, uvicorn[standard], google-cloud-speech, python-dotenv
│   ├── Dockerfile                 # python:3.12-slim, uvicorn on port 8000
│   ├── fly.toml                   # Fly.io config: ams region, shared-cpu-1x, 1GB RAM
│   ├── .env.example               # Template for required env vars
│   └── public/                    # Webapp files served at / (identical to webapp/)
│       ├── index.html
│       ├── app.js
│       ├── style.css
│       └── pcm-processor.js
├── webapp/                        # Webapp source (copied to server/public/)
│   ├── index.html                 # UI: controls, audio levels, transcript area, status dots
│   ├── app.js                     # Main logic: dual WS clients, mic capture, Browser SR, transcript UI
│   ├── style.css                  # Responsive styling
│   └── pcm-processor.js          # AudioWorklet: float32→int16 conversion, 1600-sample (100ms) chunks
└── docs/archive/
    └── audio-poc-spec.md          # Original PoC spec (Ukrainian, archived)
```

## Audio Pipeline

### Client audio (system → transcript)

```
All system processes
  → CoreAudio Process Tap (CATapDescription, stereo mixdown)
  → Aggregate Device (tap + output device as clock master)
  → IO Proc callback (AudioDeviceCreateIOProcIDWithBlock)
  → Downsample: 48kHz stereo float32 → 16kHz mono int16
  → Base64-encode, send as JSON {type: "system_audio", audio, samples} via WS port 9001
  → Webapp decodes base64 → raw PCM bytes
  → Binary frame [0x00 | pcm] → Server /audio WS
  → SpeechDetector (VAD) + Google Cloud Speech v2 (Chirp)
  → {type: "transcript", speaker: "client", text, interim} back to Webapp
```

### Sales audio (microphone → transcript)

```
Microphone
  → getUserMedia (echoCancellation, noiseSuppression)
  → AudioContext (sampleRate: 16kHz)
  → AudioWorklet (pcm-processor.js): float32 → int16, 100ms chunks
  → Binary frame [0x01 | pcm] → Server /audio WS
  → SpeechDetector (VAD) + Google Cloud Speech v2 (Chirp)
  → {type: "transcript", speaker: "sales", text, interim} back to Webapp
```

Additionally, Webapp runs **Browser SpeechRecognition** on the mic stream for real-time interim results. Chirp finals are authoritative; Browser SR cards get deduplicated (removed if substring of Chirp result). VAD events gate Browser SR to prevent echo pickup.

## WebSocket Protocol

### Agent WS (`ws://localhost:9001`)

| Direction | Message |
|-----------|---------|
| Webapp → Agent | `{command: "start"}` — begin system audio capture |
| Webapp → Agent | `{command: "stop"}` — stop capture |
| Webapp → Agent | `{command: "ping"}` — keepalive |
| Agent → Webapp | `{type: "system_audio", audio: "<base64-pcm>", samples: N}` — PCM chunk (~100ms) |
| Agent → Webapp | `{type: "system_rms", rms: N}` — RMS level (every 500ms) |
| Agent → Webapp | `{type: "capture_started", mode: "system_wide"}` — capture confirmed |
| Agent → Webapp | `{type: "error", message: "..."}` — error |

### Backend WS (`ws://[server]/audio`)

| Direction | Format | Message |
|-----------|--------|---------|
| Webapp → Server | Binary | `[0x00 \| pcm]` — client (system) audio frame |
| Webapp → Server | Binary | `[0x01 \| pcm]` — sales (mic) audio frame |
| Webapp → Server | JSON | `{type: "start_call"}` — initialize STT streams |
| Webapp → Server | JSON | `{type: "end_call"}` — graceful shutdown |
| Server → Webapp | JSON | `{type: "transcript", speaker: "client\|sales", text: "...", interim: bool}` |
| Server → Webapp | JSON | `{type: "vad_event", speaker: "client\|sales", event: "speech_start\|speech_end"}` |
| Server → Webapp | JSON | `{type: "analysis", data: {qualificationStatus, clientProfile, summary, sentiment, ...}}` |
| Server → Webapp | JSON | `{type: "valueQuestions", questions: [{id, text, batch}], batch: N}` |

## STT Configuration

- **Engine**: Google Cloud Speech v2 (Chirp model) — primary; v1 (`latest_long`) — fallback
- **Language**: `uk-UA` (Ukrainian)
- **Region**: `europe-west4`
- **Auto punctuation**: enabled
- **Interim results**: not supported by Chirp v2 for uk-UA (Browser SR used instead)
- **Session duration**: 270 seconds, then auto-reconnect (max 10 reconnects)
- **Overlap buffer**: last 8 seconds of audio replayed on reconnect for deduplication
- **Silence keepalive**: empty frame every 5 seconds to prevent Google timeout
- **Buffer target**: 1600 bytes (~100ms) accumulated before sending to STT
- **Filler word suppression**: utterances <4 words where all words are fillers (так, ага, ок, угу, добре, розумію, ну, да, мгм, гм, ааа, еее) are suppressed
- **Overlap deduplication**: word-aligned prefix matching between sessions to skip/trim duplicate finals

## VAD (Voice Activity Detection)

- **Location**: `server/audio_ws.py`, class `SpeechDetector`
- **Method**: energy-based RMS (via `audioop.rms()`)
- **RMS threshold**: 350 (configurable `VAD_SPEECH_RMS`)
- **Frame size**: 640 bytes (20ms at 16kHz 16-bit mono)
- **Speech entry**: 3+ consecutive frames above threshold (60ms)
- **Speech exit**: 15+ consecutive frames below threshold (300ms debounce)
- **Behavior**: emits `speech_start` / `speech_end` events only — does **NOT** gate audio to STT (all frames always sent)

## AI Analysis

**Location**: `server/app/ai_analysis.py` (class `CallAnalyzer`), `server/app/session.py` (class `CallSession`)

### Models

| Model | Role | Temperature |
|-------|------|-------------|
| `gemini-2.5-flash-lite` | Fast analysis (qualification + profile extraction) | 0.1 |
| `gemini-2.5-flash` | Full analysis (summary, sentiment, offer) + value question generation | 0.3 / 0.4 |

### Fast analysis (debounce 0.25s)

Triggered on every new transcript. Extracts:
- **Qualification tracking** — 4 predefined questions (availability, role/industry, experience, pain/goals); status: `asked` / `answered` / `null`
- **Client profile** — 8 fields: name, role, company, industry, experience, painPoints, goal, course
- **Value question status** — tracks which generated value questions have been asked/answered

### Full analysis (debounce 1.5s)

Triggered on every new transcript. Generates:
- **Summary for offer** — up to 5 bullet points of client statements useful for closing
- **Client sentiment** — Positive / Neutral / Skeptical / Negative + reason
- **Objection handling** — rebuttal if client raised an objection
- **Recommended offer** — best-fit course with price

### Value question generation

Personalized sales questions generated in two batches:
- **Batch 1** (5 questions) — triggered when ≥2 profile tag fields (industry, experience, company, painPoints, goal) are filled
- **Batch 2** (5 deeper follow-ups) — triggered when ≥2 questions from batch 1 have been asked

Questions are tailored to client's industry, role, and pain points. Style: short (<15 words), no jargon, expert-level thinking.

## Desktop Agent

**How it works**: Creates a CoreAudio Process Tap that captures a stereo mixdown of all system audio. Wraps the tap in an Aggregate Device (with the default output device as clock master to prevent drift). Registers an IO Proc callback that fires on every audio cycle, downsamples from device rate (typically 48kHz) to 16kHz mono int16 PCM. Drains PCM buffer every 100ms via timer, reports RMS every 500ms.

**Two modes**:
1. `--server` flag: WebSocket server on port 9001 (used by Webapp)
2. Default (no args): Chrome Native Messaging via stdin/stdout (legacy)

**Build & Run**:
```bash
cd scriptius-native
swift build -c release
.build/release/ScriptiusAudio --server
# Now listening on ws://localhost:9001
```

**Requirements**: macOS 14.2+ (Process Tap API), microphone/audio permission in System Settings.

## Commands

```bash
# Server (local)
cd server
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000

# Desktop Agent
cd scriptius-native
swift build -c release
.build/release/ScriptiusAudio --server

# Webapp — served automatically by FastAPI at http://localhost:8000

# Deploy to Fly.io
cd server
fly deploy
```

## Environment Variables

Stored in `server/.env` (see `server/.env.example`):

| Variable | Description |
|----------|-------------|
| `GOOGLE_PROJECT_ID` | GCP project ID |
| `GOOGLE_CREDENTIALS_JSON` | Full service account JSON (embedded, not a file path) |
| `GOOGLE_STT_LOCATION` | GCP region for Speech v2 API (`europe-west4`) |
| `STT_ENGINE` | `chirp_v2` (primary) or `latest_long_v1` (v1 fallback) |
| `GEMINI_API_KEY` | Google Gemini API key for AI analysis |

## Code Style

- **Webapp**: No build tools, no bundlers — vanilla JS, single files. No frameworks.
- **Server**: Minimal file structure — `main.py` (entry) + `audio_ws.py` (all logic). No ORM, no complex abstractions.
- **Swift**: No external dependencies — system frameworks only (CoreAudio, AudioToolbox, Network).
- **Language**: STT configured for Ukrainian (`uk-UA`). Code comments and variable names in English.

## What's NOT Built Yet

These features are planned but **do not exist in the codebase** — do not look for them:

- HubSpot CRM integration (contact matching, call logging)
- Full product UI (current UI is functional but minimal/dev-oriented)
- User authentication and authorization
- Call history / database storage
- Multi-user / multi-tenant support
