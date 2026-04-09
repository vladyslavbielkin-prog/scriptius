import os
import json
import asyncio
import audioop
import collections
import dataclasses
import time
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google.oauth2 import service_account as gsa

logger = logging.getLogger("scriptius.stt")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

router = APIRouter()

# ── Config ────────────────────────────────────────────────────────────────────
GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", "")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_STT_LOCATION = os.getenv("GOOGLE_STT_LOCATION", "europe-west4")
STT_ENGINE = os.getenv("STT_ENGINE", "chirp_v2")  # "chirp_v2" or "latest_long_v1"

RECONNECT_SECONDS = 270
OVERLAP_MAX_BYTES = 8 * 32000  # ~8 seconds of 16kHz 16-bit mono
BUFFER_TARGET = 640   # bytes before sending to STT (~20ms at 16kHz 16-bit mono)

# ── VAD config ────────────────────────────────────────────────────────────────
VAD_FRAME_BYTES = 640          # 20ms frame at 16kHz 16-bit mono
VAD_SPEECH_RMS = 350           # RMS threshold for speech frame
VAD_SPEECH_FRAMES = 3          # consecutive speech frames to enter speech (60ms)
VAD_SILENCE_FRAMES = 15        # silence frames after speech to end it (300ms debounce)
VAD_PRE_BUFFER_BYTES = 9600    # 300ms rolling pre-speech buffer (15 × 640) [unused, kept for reference]

FILLER_WORDS = frozenset({
    "так", "ага", "ок", "угу", "добре", "розумію", "ну", "да",
    "мгм", "гм", "ааа", "еее",
})


# ── Event callbacks ──────────────────────────────────────────────────────────

@dataclasses.dataclass
class EventCallbacks:
    on_transcript: object = None   # (speaker, text, is_final) -> None
    on_vad_event: object = None    # (speaker, event) -> None
    on_call_start: object = None   # (session_id) -> None
    on_call_end: object = None     # (session_id) -> None


def _load_credentials():
    if not GOOGLE_CREDENTIALS_JSON or not GOOGLE_PROJECT_ID:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON and GOOGLE_PROJECT_ID must be set")
    return gsa.Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS_JSON),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


# ── Speech detector (parallel VAD — events only, does NOT gate audio) ────────

class SpeechDetector:
    """Energy-based VAD that detects speech boundaries without gating audio.
    All audio goes directly to the STT queue; this class only emits events."""

    def __init__(self, speaker: str, session_id: str):
        self.speaker = speaker
        self.session_id = session_id
        self._pending = bytearray()
        self._speech_count = 0
        self._silence_count = 0
        self.in_speech = False
        self.events: list[str] = []  # collected per feed() call

    def feed(self, pcm: bytes) -> None:
        self.events.clear()
        self._pending.extend(pcm)
        while len(self._pending) >= VAD_FRAME_BYTES:
            frame = bytes(self._pending[:VAD_FRAME_BYTES])
            del self._pending[:VAD_FRAME_BYTES]
            rms = audioop.rms(frame, 2)
            is_speech = rms > VAD_SPEECH_RMS

            if not self.in_speech:
                if is_speech:
                    self._speech_count += 1
                    if self._speech_count >= VAD_SPEECH_FRAMES:
                        self.in_speech = True
                        self._silence_count = 0
                        self.events.append("speech_start")
                        logger.info(f"[{self.session_id}][{self.speaker}] VAD: speech start")
                else:
                    self._speech_count = 0
            else:
                if is_speech:
                    self._silence_count = 0
                else:
                    self._silence_count += 1
                    if self._silence_count >= VAD_SILENCE_FRAMES:
                        self.in_speech = False
                        self._speech_count = 0
                        self._silence_count = 0
                        self.events.append("speech_end")
                        logger.info(f"[{self.session_id}][{self.speaker}] VAD: speech end")


def _is_filler_only(text: str) -> bool:
    """Return True if text is short and contains only filler words."""
    words = text.lower().strip().rstrip(".!?,;:").split()
    return len(words) < 4 and all(w.strip(".,!?") in FILLER_WORDS for w in words)


def _find_overlap(prev: str, curr: str) -> str:
    """Find the longest suffix of prev that is a prefix of curr (word-aligned)."""
    prev_words = prev.split()
    curr_words = curr.split()
    if not prev_words or not curr_words:
        return ""
    max_overlap = min(len(prev_words), len(curr_words))
    for length in range(max_overlap, 0, -1):
        if prev_words[-length:] == curr_words[:length]:
            return " ".join(curr_words[:length])
    return ""


# Silence frame sent during VAD silence to keep Google stream alive
SILENCE_FRAME = b'\x00' * BUFFER_TARGET
# Max silence keepalives per second (send one every ~5s to avoid wasting bandwidth)
KEEPALIVE_INTERVAL = 5.0
MAX_RECONNECTS = 10


# ── STT streaming (v2 chirp) ─────────────────────────────────────────────────

async def _stream_stt_v2(audio_queue: asyncio.Queue, speaker: str,
                         websocket: WebSocket, credentials, session_id: str,
                         callbacks: EventCallbacks = None, language: str = "uk-UA"):
    """Stream audio to Google Speech v2 with chirp model."""
    from google.cloud.speech_v2 import SpeechAsyncClient
    from google.cloud.speech_v2.types import cloud_speech
    from google.api_core.client_options import ClientOptions

    client = SpeechAsyncClient(
        credentials=credentials,
        client_options=ClientOptions(
            api_endpoint=f"{GOOGLE_STT_LOCATION}-speech.googleapis.com"
        ),
    )
    recognizer = f"projects/{GOOGLE_PROJECT_ID}/locations/{GOOGLE_STT_LOCATION}/recognizers/_"

    recognition_config = cloud_speech.RecognitionConfig(
        explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
            encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            audio_channel_count=1,
        ),
        language_codes=[language, "en-US", "ru-RU"] if language != "en-US" else ["en-US", "uk-UA", "ru-RU"],
        model="chirp",
        features=cloud_speech.RecognitionFeatures(
            enable_automatic_punctuation=True,
        ),
    )
    streaming_config = cloud_speech.StreamingRecognitionConfig(
        config=recognition_config,
        streaming_features=cloud_speech.StreamingRecognitionFeatures(
            interim_results=False,  # chirp v2 uk-UA doesn't support interim
        ),
    )

    call_ended = False
    overlap_chunks: collections.deque = collections.deque(maxlen=256)
    overlap_size = 0
    is_first_session = True
    last_sent_text = ""
    last_final_text = ""
    reconnect_count = 0

    while not call_ended:
        need_reconnect = False
        session_start = asyncio.get_running_loop().time()
        audio_buffer = bytearray()
        overlap_snapshot = bytes().join(overlap_chunks) if not is_first_session else b""
        is_first_session = False

        async def audio_gen(buf=audio_buffer, t0=session_start, snap=overlap_snapshot):
            nonlocal call_ended, need_reconnect, overlap_size

            yield cloud_speech.StreamingRecognizeRequest(
                recognizer=recognizer,
                streaming_config=streaming_config,
            )

            if snap:
                logger.info(f"[{speaker}] replaying {len(snap)//32000:.1f}s overlap")
                for i in range(0, len(snap), BUFFER_TARGET):
                    chunk = snap[i:i + BUFFER_TARGET]
                    buf.extend(chunk)
                    if len(buf) >= BUFFER_TARGET:
                        yield cloud_speech.StreamingRecognizeRequest(audio=bytes(buf))
                        buf.clear()

            last_keepalive = asyncio.get_running_loop().time()

            while True:
                elapsed = asyncio.get_running_loop().time() - t0
                if elapsed >= RECONNECT_SECONDS:
                    need_reconnect = True
                    if buf:
                        yield cloud_speech.StreamingRecognizeRequest(audio=bytes(buf))
                        buf.clear()
                    return

                try:
                    chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.02)
                except asyncio.TimeoutError:
                    # Send silence keepalive to prevent Google audio timeout
                    now = asyncio.get_running_loop().time()
                    if now - last_keepalive >= KEEPALIVE_INTERVAL:
                        yield cloud_speech.StreamingRecognizeRequest(audio=SILENCE_FRAME)
                        last_keepalive = now
                    continue

                if chunk is None:
                    call_ended = True
                    if buf:
                        yield cloud_speech.StreamingRecognizeRequest(audio=bytes(buf))
                    return

                overlap_chunks.append(chunk)
                overlap_size += len(chunk)
                while overlap_size > OVERLAP_MAX_BYTES and overlap_chunks:
                    removed = overlap_chunks.popleft()
                    overlap_size -= len(removed)

                # Send immediately — no buffering (lowest latency)
                yield cloud_speech.StreamingRecognizeRequest(audio=chunk)
                last_keepalive = asyncio.get_running_loop().time()

        try:
            logger.info(f"[{session_id}][{speaker}] STT v2 chirp stream starting")
            metadata = [("x-goog-request-params", f"recognizer={recognizer}")]
            async for response in await client.streaming_recognize(
                requests=audio_gen(), metadata=metadata
            ):
                parts = []
                has_final = False
                for result in response.results:
                    if not result.alternatives:
                        continue
                    t = result.alternatives[0].transcript.strip()
                    if t:
                        parts.append(t)
                    if result.is_final:
                        has_final = True
                if not parts:
                    continue
                text = " ".join(parts)
                is_final = has_final
                if is_final and _is_filler_only(text):
                    logger.info(f"[{session_id}][{speaker}] suppressed filler: {text}")
                    continue
                if is_final:
                    # Overlap dedup: skip if this final is contained in last final
                    if last_final_text and text in last_final_text:
                        logger.info(f"[{session_id}][{speaker}] dedup: skipped (subset of previous)")
                        continue
                    # Overlap dedup: trim prefix that overlaps with end of last final
                    if last_final_text:
                        overlap = _find_overlap(last_final_text, text)
                        if overlap:
                            text = text[len(overlap):].strip()
                            logger.info(f"[{session_id}][{speaker}] dedup: trimmed overlap ({len(overlap)} chars)")
                            if not text:
                                continue
                    last_final_text = text
                if not is_final and text == last_sent_text:
                    continue
                last_sent_text = text
                logger.info(
                    f"[{session_id}][{speaker}] "
                    f"{'FINAL' if is_final else 'interim'}: {text[:200]}"
                )
                try:
                    await websocket.send_json({
                        "type": "transcript",
                        "speaker": speaker,
                        "text": text,
                        "interim": not is_final,
                    })
                except Exception:
                    call_ended = True
                    return
                if callbacks and callbacks.on_transcript:
                    callbacks.on_transcript(speaker, text, is_final)
                if is_final:
                    last_sent_text = ""

        except Exception as e:
            if call_ended:
                break
            err_str = str(e)
            if "Audio Timeout" in err_str or ("400" in err_str and "timeout" in err_str.lower()):
                logger.warning(f"[{session_id}][{speaker}] STT audio timeout, will reconnect")
                need_reconnect = True
            elif "400" in err_str:
                logger.error(f"[{session_id}][{speaker}] STT config error: {err_str}")
                call_ended = True
                break
            elif "Max duration" in err_str or "409" in err_str:
                logger.info(f"[{session_id}][{speaker}] STT reconnecting (duration limit)")
                need_reconnect = True
            elif "499" in err_str or "cancelled" in err_str.lower():
                logger.info(f"[{session_id}][{speaker}] STT stream cancelled")
                call_ended = True
                break
            else:
                logger.error(f"[{session_id}][{speaker}] STT error: {err_str}")
                need_reconnect = True
            await asyncio.sleep(0.3)

        if need_reconnect:
            reconnect_count += 1
            if reconnect_count > MAX_RECONNECTS:
                logger.error(f"[{session_id}][{speaker}] Max reconnects ({MAX_RECONNECTS}) exceeded, stopping")
                break
            backoff = min(0.5 * reconnect_count, 5.0)
            logger.info(f"[{session_id}][{speaker}] Starting new STT stream (reconnect #{reconnect_count}, backoff {backoff:.1f}s)")
            await asyncio.sleep(backoff)
        else:
            reconnect_count = 0  # Reset on successful session

    logger.info(f"[{session_id}][{speaker}] STT stream ended")


# ── STT streaming (v1 latest_long fallback) ───────────────────────────────────

async def _stream_stt_v1(audio_queue: asyncio.Queue, speaker: str,
                         websocket: WebSocket, credentials, session_id: str,
                         callbacks: EventCallbacks = None, language: str = "uk-UA"):
    """Stream audio to Google Speech v1 with latest_long model."""
    from google.cloud import speech as speech_v1

    client = speech_v1.SpeechAsyncClient(credentials=credentials)

    # Set primary and alternative languages based on session language
    if language == "en-US":
        primary_lang = "en-US"
        alt_langs = ["uk-UA", "ru-RU"]
    else:
        primary_lang = "uk-UA"
        alt_langs = ["en-US", "ru-RU"]

    streaming_config = speech_v1.StreamingRecognitionConfig(
        config=speech_v1.RecognitionConfig(
            encoding=speech_v1.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code=primary_lang,
            alternative_language_codes=alt_langs,
            model="latest_long",
            enable_automatic_punctuation=True,
        ),
        interim_results=True,
    )

    call_ended = False
    overlap_chunks: collections.deque = collections.deque(maxlen=256)
    overlap_size = 0
    is_first_session = True
    last_sent_text = ""
    last_final_text = ""
    reconnect_count = 0

    while not call_ended:
        need_reconnect = False
        session_start = asyncio.get_running_loop().time()
        audio_buffer = bytearray()
        overlap_snapshot = bytes().join(overlap_chunks) if not is_first_session else b""
        is_first_session = False

        async def audio_gen(buf=audio_buffer, t0=session_start, snap=overlap_snapshot):
            nonlocal call_ended, need_reconnect, overlap_size

            yield speech_v1.StreamingRecognizeRequest(
                streaming_config=streaming_config,
            )

            if snap:
                logger.info(f"[{speaker}] replaying {len(snap)//32000:.1f}s overlap")
                for i in range(0, len(snap), BUFFER_TARGET):
                    chunk = snap[i:i + BUFFER_TARGET]
                    buf.extend(chunk)
                    if len(buf) >= BUFFER_TARGET:
                        yield speech_v1.StreamingRecognizeRequest(audio_content=bytes(buf))
                        buf.clear()

            last_keepalive = asyncio.get_running_loop().time()

            while True:
                elapsed = asyncio.get_running_loop().time() - t0
                if elapsed >= RECONNECT_SECONDS:
                    need_reconnect = True
                    if buf:
                        yield speech_v1.StreamingRecognizeRequest(audio_content=bytes(buf))
                        buf.clear()
                    return

                try:
                    chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.02)
                except asyncio.TimeoutError:
                    # Send silence keepalive to prevent Google audio timeout
                    now = asyncio.get_running_loop().time()
                    if now - last_keepalive >= KEEPALIVE_INTERVAL:
                        yield speech_v1.StreamingRecognizeRequest(audio_content=SILENCE_FRAME)
                        last_keepalive = now
                    continue

                if chunk is None:
                    call_ended = True
                    if buf:
                        yield speech_v1.StreamingRecognizeRequest(audio_content=bytes(buf))
                    return

                overlap_chunks.append(chunk)
                overlap_size += len(chunk)
                while overlap_size > OVERLAP_MAX_BYTES and overlap_chunks:
                    removed = overlap_chunks.popleft()
                    overlap_size -= len(removed)

                # Send immediately — no buffering (lowest latency)
                yield speech_v1.StreamingRecognizeRequest(audio_content=chunk)
                last_keepalive = asyncio.get_running_loop().time()

        try:
            logger.info(f"[{session_id}][{speaker}] STT v1 latest_long stream starting")
            async for response in await client.streaming_recognize(requests=audio_gen()):
                parts = []
                has_final = False
                for result in response.results:
                    if not result.alternatives:
                        continue
                    t = result.alternatives[0].transcript.strip()
                    if t:
                        parts.append(t)
                    if result.is_final:
                        has_final = True
                if not parts:
                    continue
                text = " ".join(parts)
                is_final = has_final
                if is_final and _is_filler_only(text):
                    logger.info(f"[{session_id}][{speaker}] suppressed filler: {text}")
                    continue
                if is_final:
                    # Overlap dedup: skip if this final is contained in last final
                    if last_final_text and text in last_final_text:
                        logger.info(f"[{session_id}][{speaker}] dedup: skipped (subset of previous)")
                        continue
                    # Overlap dedup: trim prefix that overlaps with end of last final
                    if last_final_text:
                        overlap = _find_overlap(last_final_text, text)
                        if overlap:
                            text = text[len(overlap):].strip()
                            logger.info(f"[{session_id}][{speaker}] dedup: trimmed overlap ({len(overlap)} chars)")
                            if not text:
                                continue
                    last_final_text = text
                if not is_final and text == last_sent_text:
                    continue
                last_sent_text = text
                logger.info(
                    f"[{session_id}][{speaker}] "
                    f"{'FINAL' if is_final else 'interim'}: {text[:200]}"
                )
                try:
                    await websocket.send_json({
                        "type": "transcript",
                        "speaker": speaker,
                        "text": text,
                        "interim": not is_final,
                    })
                except Exception:
                    call_ended = True
                    return
                if callbacks and callbacks.on_transcript:
                    callbacks.on_transcript(speaker, text, is_final)
                if is_final:
                    last_sent_text = ""

        except Exception as e:
            if call_ended:
                break
            err_str = str(e)
            if "Audio Timeout" in err_str or ("400" in err_str and "timeout" in err_str.lower()):
                logger.warning(f"[{session_id}][{speaker}] STT audio timeout, will reconnect")
                need_reconnect = True
            elif "400" in err_str:
                logger.error(f"[{session_id}][{speaker}] STT config error: {err_str}")
                call_ended = True
                break
            elif "Max duration" in err_str or "409" in err_str:
                logger.info(f"[{session_id}][{speaker}] STT reconnecting (duration limit)")
                need_reconnect = True
            elif "499" in err_str or "cancelled" in err_str.lower():
                logger.info(f"[{session_id}][{speaker}] STT stream cancelled")
                call_ended = True
                break
            else:
                logger.error(f"[{session_id}][{speaker}] STT error: {err_str}")
                need_reconnect = True
            await asyncio.sleep(0.3)

        if need_reconnect:
            reconnect_count += 1
            if reconnect_count > MAX_RECONNECTS:
                logger.error(f"[{session_id}][{speaker}] Max reconnects ({MAX_RECONNECTS}) exceeded, stopping")
                break
            backoff = min(0.5 * reconnect_count, 5.0)
            logger.info(f"[{session_id}][{speaker}] Starting new STT stream (reconnect #{reconnect_count}, backoff {backoff:.1f}s)")
            await asyncio.sleep(backoff)
        else:
            reconnect_count = 0  # Reset on successful session

    logger.info(f"[{session_id}][{speaker}] STT stream ended")


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/audio")
async def audio_ws(websocket: WebSocket):
    from app.session import CallSession
    from app.ai_analysis import CallAnalyzer

    await websocket.accept()
    session_id = f"scriptius_{int(time.time())}_{id(websocket)}"
    logger.info(f"[{session_id}] WebSocket connected")

    # ── Session & callbacks ──────────────────────────────────────────────
    session = CallSession(session_id)
    analyzer = CallAnalyzer(session, websocket)

    def _on_transcript(speaker, text, is_final):
        if is_final:
            session.add_transcript(speaker, text)
            analyzer.on_new_transcript(speaker, text)

    def _on_vad_event(speaker, event):
        pass  # VAD events already logged by SpeechDetector

    def _on_call_start(sid):
        logger.info(f"[{sid}] Call session started")

    def _on_call_end(sid):
        logger.info(f"[{sid}] Call session ended, {len(session.conversation)} utterances")

    callbacks = EventCallbacks(
        on_transcript=_on_transcript,
        on_vad_event=_on_vad_event,
        on_call_start=_on_call_start,
        on_call_end=_on_call_end,
    )
    callbacks.on_call_start(session_id)

    try:
        credentials = _load_credentials()
    except Exception as e:
        logger.error(f"[{session_id}] Credentials error: {e}")
        await websocket.close(code=1011)
        return

    client_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    sales_queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    client_detector = SpeechDetector("client", session_id)
    sales_detector = SpeechDetector("sales", session_id)

    stream_fn = _stream_stt_v2 if STT_ENGINE == "chirp_v2" else _stream_stt_v1
    logger.info(f"[{session_id}] Using STT engine: {STT_ENGINE}")

    # Lazy-start STT streams — only when first audio arrives for that channel
    stt_tasks: dict[int, asyncio.Task] = {}  # track -> task
    queues = {0: client_queue, 1: sales_queue}
    speakers = {0: "client", 1: "sales"}

    def get_stt_language():
        """Get STT language code based on session forced language."""
        fl = session.forced_language
        if fl and "english" in fl.lower():
            return "en-US"
        return "uk-UA"

    def ensure_stt(track: int):
        if track not in stt_tasks:
            q = queues[track]
            sp = speakers[track]
            lang = get_stt_language()
            stt_tasks[track] = asyncio.create_task(
                stream_fn(q, sp, websocket, credentials, session_id, callbacks, language=lang)
            )
            logger.info(f"[{session_id}][{sp}] STT stream started (lang={lang}, first audio received)")

    try:
        while True:
            msg = await websocket.receive()

            if msg["type"] == "websocket.receive":
                if "bytes" in msg and msg["bytes"]:
                    raw = msg["bytes"]
                    if len(raw) < 2:
                        continue
                    track = raw[0]  # 0x00 = client, 0x01 = sales
                    pcm = raw[1:]

                    # Start STT on first audio for this channel
                    ensure_stt(track)

                    # All audio direct to STT (no gating)
                    target_queue = queues.get(track, sales_queue)
                    try:
                        target_queue.put_nowait(pcm)
                    except asyncio.QueueFull:
                        pass

                    # VAD in parallel — events only
                    detector = client_detector if track == 0 else sales_detector
                    detector.feed(pcm)
                    for ev in detector.events:
                        try:
                            await websocket.send_json({
                                "type": "vad_event",
                                "speaker": detector.speaker,
                                "event": ev,
                            })
                        except Exception:
                            pass
                        if callbacks.on_vad_event:
                            callbacks.on_vad_event(detector.speaker, ev)

                elif "text" in msg and msg["text"]:
                    try:
                        data = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    msg_type = data.get("type")
                    if msg_type == "start_call":
                        logger.info(f"[{session_id}] Call started, course_id={data.get('course_id')}")
                        # Check for HubSpot prefill data
                        from app.hubspot import get_pending_prefill
                        prefill = get_pending_prefill()
                        if prefill:
                            # Remove internal fields (prefixed with _)
                            client_fields = {k: v for k, v in prefill.items() if not k.startswith("_") and v}
                            if client_fields:
                                session.update_profile(client_fields)
                                logger.info(f"[{session_id}] HubSpot prefill applied: {list(client_fields.keys())}")
                                analyzer.update_qualification_questions()
                                try:
                                    await websocket.send_json({
                                        "type": "analysis",
                                        "data": {"clientProfile": session.client_profile},
                                    })
                                    # Also send prefill metadata (phone, deal name)
                                    meta = {k: v for k, v in prefill.items() if k.startswith("_") and v}
                                    if meta:
                                        await websocket.send_json({
                                            "type": "hubspotMeta",
                                            "data": meta,
                                        })
                                except Exception:
                                    pass
                                analyzer.trigger_fast()
                    elif msg_type == "end_call":
                        logger.info(f"[{session_id}] Call ended by client")
                        break
                    elif msg_type == "clientInfo":
                        client_data = data.get("data", {})
                        logger.info(f"[{session_id}] Client info received: {list(client_data.keys())}")
                        session.update_profile(client_data)
                        try:
                            await websocket.send_json({
                                "type": "analysis",
                                "data": {"clientProfile": session.client_profile},
                            })
                        except Exception:
                            pass
                        analyzer.trigger_fast()
                    elif msg_type == "setLanguage":
                        lang = data.get("language", "Ukrainian")
                        session.forced_language = lang
                        analyzer.update_qualification_questions()
                        logger.info(f"[{session_id}] Language set to: {lang}")
                    elif msg_type == "note":
                        note_text = data.get("text", "")
                        if note_text:
                            session.notes.append(note_text)
                            logger.info(f"[{session_id}] Note saved: \"{note_text[:100]}\"")

            elif msg["type"] == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] WebSocket disconnected")

    finally:
        analyzer.cancel()

        if callbacks.on_call_end:
            callbacks.on_call_end(session_id)

        # Signal STT tasks to stop and wait
        for track, task in stt_tasks.items():
            await queues[track].put(None)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        logger.info(f"[{session_id}] Session cleaned up")
