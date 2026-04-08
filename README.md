# Scriptius

Real-time dual-stream call transcription and AI analysis for sales teams.

Captures system audio (client on the phone) and microphone (salesperson) as separate streams, transcribes them live, and provides real-time AI coaching — qualification tracking, client profiling, objection handling.

## What it does

- Dual-stream capture: system audio + microphone as separate, attributed channels
- Live transcription via Google Cloud Speech STT (Ukrainian, `uk-UA`)
- AI analysis powered by Gemini: qualification checklist, client profile extraction, call summary, objection handling, value questions
- Sub-second latency via WebSocket streaming throughout

## Architecture

```
System Audio → Desktop Agent (ws://localhost:9001) ──┐
                                                      ├─→ Backend /audio WS → Google STT → Transcripts
Microphone  → Browser (getUserMedia) ────────────────┘                     → Gemini     → AI Analysis
```

Three components:

| Component | Tech |
|-----------|------|
| Desktop Agent | Swift 5.9, CoreAudio Process Tap, macOS 14.2+ |
| Backend | Python 3.12, FastAPI, Google Cloud Speech v1 (`latest_long`) |
| Frontend | Vanilla JS, Web Audio API, AudioWorklet, WebSocket |
| Deploy | Fly.io (`ams` region) |

## Quick start (local dev)

**Prerequisites**: Python 3.12+, Swift 5.9+, macOS 14.2+, Google Cloud credentials.

```bash
# 1. Backend
cd server
pip install -r requirements.txt
cp .env.example .env        # fill in GOOGLE_PROJECT_ID, GOOGLE_CREDENTIALS_JSON, etc.
uvicorn main:app --host 0.0.0.0 --port 8000

# 2. Desktop Agent (separate terminal)
cd scriptius-native
swift build -c release
.build/release/ScriptiusAudio --server
# → Listening on ws://localhost:9001

# 3. Open http://localhost:8000
```

Grant **Screen Recording** permission to Terminal before running the agent (System Settings → Privacy & Security → Screen Recording). Required for CoreAudio Process Tap to capture system audio.

## Environment variables

| Variable | Description |
|----------|-------------|
| `GOOGLE_PROJECT_ID` | GCP project ID |
| `GOOGLE_CREDENTIALS_JSON` | Service account JSON (full content, not a file path) |
| `GOOGLE_STT_LOCATION` | Speech v2 API region (e.g. `europe-west4`) |
| `STT_ENGINE` | `chirp_v2` or `latest_long_v1` (default: `chirp_v2`) |
| `GEMINI_API_KEY` | Gemini API key for AI analysis |

## Deploy

```bash
cd server
fly deploy
```

Configured for Fly.io `ams` region, `shared-cpu-1x`, 1 GB RAM. See `server/fly.toml`.

## Project structure

```
scriptius-native/   Swift Desktop Agent — CoreAudio capture, WS server on :9001
server/             Python backend — FastAPI, STT streaming, AI analysis
  app/              CallSession, CallAnalyzer (Gemini)
  public/           Static webapp (copy of webapp/)
webapp/             Frontend source — vanilla JS, Web Audio API
docs/archive/       Original spec (Ukrainian)
```

For deeper technical documentation see [CLAUDE.md](CLAUDE.md).

## Setting up on a new Mac (end users)

See [SETUP.md](SETUP.md).
