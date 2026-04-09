# Scriptius — Changes on 2026-04-09

## Transcription Speed Improvements

### Problem
Live transcription had 5+ second delays. Text only appeared after the speaker finished talking.

### Changes
1. **Switched STT engine**: `chirp_v2` (no interim results for Ukrainian) → `latest_long_v1` (supports interim results). Text now appears word-by-word as the speaker talks.
2. **Removed audio buffering**: Server was accumulating 1600 bytes before sending to Google. Now every audio chunk goes directly to Google STT with zero buffering (like legacy project).
3. **Reduced AudioWorklet buffer**: 100ms (1600 samples) → 30ms (480 samples). Audio reaches the server 70ms sooner.
4. **Reduced queue poll timeout**: 100ms → 20ms. Less idle wait per chunk.
5. **Lazy STT streams**: STT streams only start when actual audio arrives for that channel. Previously the client channel started immediately with no audio, timed out, and caused reconnects that disrupted the sales channel too.
6. **Non-blocking Gemini calls**: All 3 Gemini API calls (`generate_content`) were synchronous, blocking the entire asyncio event loop for 1-10 seconds. Wrapped in `asyncio.to_thread()` so audio streaming continues uninterrupted during AI analysis.
7. **Removed console.log spam**: Frontend was JSON.stringify-ing every interim/VAD message (30+ times/sec), causing browser jank.
8. **Simplified frontend transcript display**: Removed VAD-driven "..." placeholder cards, finalize timers, time-estimation insertion sorting. Text now appears immediately when Google sends it.

### Result
~3x faster perceived transcription. Interims appear within ~1 second of speech start.

---

## AI Analysis Speed Improvements

### Problem
Checkboxes and value questions updated slowly (5+ seconds after speaking).

### Changes
1. **Fast analysis debounce**: 0.25s → 0.1s
2. **Value questions model**: `gemini-2.5-flash` (~10s) → `gemini-2.5-flash-lite` (~1-2s)
3. **Immediate needs extraction**: Separate parallel Gemini call fires on every client transcript with zero debounce. Uses a minimal focused prompt for faster response.
4. **Needs only from client**: Extraction only fires when the CLIENT speaks, not on sales rep questions.
5. **Needs language**: Always written in conversation language (not English).
6. **Confirmed needs only**: Sales rep asking "do you have problems with X?" is NOT added as a need. Only added when client confirms/states the problem.

---

## Client Needs & Problems (new section)

### What changed
- Renamed from "Conversation Summary" to "Client Needs & Problems"
- Removed sentiment and objections from UI
- Up to 20 bullet points (was 5)
- Points are locked — once added, never removed (unless client contradicts)
- Duplicates and similar points filtered (substring matching)
- Two extraction paths: immediate (per client transcript) + backup (fast analysis)

---

## Recommended Offer (redesigned)

### What changed
- Shows 3 bullet points (always visible):
  - Certificate of completion
  - Access to LMS platform and community
  - Final project with mentor support
- Course name comes from the selected dropdown (or HubSpot deal name), not AI guess
- Price shown below bullet points
- Smaller, more compact fonts

### Client Readiness Bar
- Each segment has its own color: dark red → light red → yellow → light green → dark green
- Calculated from: qualification questions checked + client needs count + value questions asked
- Labels: Low → Early → Neutral → Warm → Ready

---

## Qualification Questions (dynamic)

### What changed
- Questions adapt based on available HubSpot data:
  - **All data available** (2 questions): "Чи зручно говорити?" + natural confirmation like "Бачу що ви вказали, що працюєте маркетологом в ІТ індустрії уже 8 років. Скажіть, все вірно?"
  - **Partial data** (3-4 questions): Confirmation + questions for missing fields
  - **No data** (4 questions): Same as before
- Min 2, max 4 questions
- Pain/goals question changed to: "Скажіть, а чим зацікавив вас наш курс? Чим він міг би бути вам корисним?"
- Experience field handles both years ("уже 8 років") and levels ("на рівні Senior")

---

## Value Justification Questions (UI)

### What changed
- Round 1 and Round 2 displayed side by side in two columns (was stacked vertically)

---

## Multi-language Support

### What changed
- Added **Country selector** (Ukraine / USA) in client card
- When USA selected:
  - STT uses English as primary language (`en-US`)
  - Qualification questions in English
  - Offer bullet points in English
  - AI analysis (needs, value questions, recommended offer) generates in English
- When Ukraine selected: everything in Ukrainian
- STT auto-detects between Ukrainian, English, and Russian

---

## HubSpot Integration (new)

### What it does
Paste a HubSpot deal URL → client card fills instantly.

### Components
- `server/app/hubspot.py` — HubSpot API client, REST endpoints
- HubSpot Deal input field in client card UI
- Auto-start call after HubSpot data loads

### Field mapping
| HubSpot | Scriptius |
|---|---|
| Contact firstname + lastname | Name |
| Deal name | Course (auto-added to dropdown if custom) |
| Deal position | Position |
| Deal experience | Experience |
| Deal company | Company |
| Deal industry | Industry |

### Setup
- Create HubSpot Private App with `crm.objects.deals.read` + `crm.objects.contacts.read` scopes
- Add access token to `server/.env` as `HUBSPOT_ACCESS_TOKEN`
- Full docs: `docs/hubspot-setup.md`

### Data priority
- Deal properties checked first
- Contact properties as fallback
- AI analysis never overwrites HubSpot data with null

---

## UI Layout Changes

### What changed
- Right sidebar (Client Needs + Recommended Offer): both cards stretch full viewport height, top half / bottom half
- Left sidebar (Client Card + Notes): both cards stretch full viewport height, top half / bottom half
- Client card fields in 3 compact rows (was 4 with full-width pain/goal)
- Removed Pain Points and Goal from client card UI (kept in backend for triggers)
- Country and Course on same line
- HubSpot Deal input field added to client card

---

## Desktop Agent (Swift)

### What changed
- Fixed WebSocket "Socket is not connected" error (POSIX 57)
- Dead connections cleaned up on send errors
- Normal disconnects no longer log scary error messages

---

## New Files
- `docs/hubspot-setup.md` — HubSpot integration setup guide
- `skill.md` — How each Scriptius component works (for sales reps)
- `server/app/hubspot.py` — HubSpot API integration
- `server/public/hubspot-calling.html` — HubSpot Calling Extension widget (experimental)
- `CHANGELOG-2026-04-09.md` — This file

## Modified Files
- `server/audio_ws.py` — STT engine, lazy streams, language support, non-blocking
- `server/app/ai_analysis.py` — Dynamic prompts, immediate needs, language support
- `server/app/session.py` — locked_summary, forced_language, null-safe profile update
- `server/main.py` — HubSpot router
- `server/requirements.txt` — added httpx, audioop-lts
- `webapp/app.js` — Transcript simplification, HubSpot loader, country selector, i18n
- `webapp/index.html` — New UI sections, HubSpot field, country selector
- `webapp/style.css` — Full-height sidebars, compact layout, readiness colors
- `webapp/pcm-processor.js` — Reduced buffer to 30ms
