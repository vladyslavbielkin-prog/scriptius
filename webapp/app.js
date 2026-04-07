// Scriptius Web App — Live Call Transcript
// Replaces Chrome Extension: all logic in one file.
//
// Two WebSocket connections:
//   ws://localhost:9001 — Desktop Agent (system audio capture)
//   ws://localhost:8000 — STT Backend (audio → transcripts)

// ── DOM ─────────────────────────────────────────────────────
const btnStart = document.getElementById('btnStart');
const btnStop = document.getElementById('btnStop');
const systemBar = document.getElementById('systemBar');
const micBar = document.getElementById('micBar');
const systemValue = document.getElementById('systemValue');
const micValue = document.getElementById('micValue');
const statusDot = document.getElementById('statusDot');
const statusText = document.getElementById('statusText');
const errorMsg = document.getElementById('errorMsg');
const transcriptList = document.getElementById('transcriptList');
const transcriptCount = document.getElementById('transcriptCount');
const transcriptEmpty = document.getElementById('transcriptEmpty');
const agentDot = document.getElementById('agentDot');
const backendDot = document.getElementById('backendDot');

// ── Config ──────────────────────────────────────────────────
const AGENT_WS_URL = 'ws://localhost:9001';
const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
const BACKEND_WS_URL = `${wsProtocol}//${location.host}/audio`;
const SILENCE_TIMEOUT = 1000;
const MAX_UTTERANCE_CHARS = 120;
const STT_LANG = 'uk-UA';

// ── State ───────────────────────────────────────────────────
let capturing = false;
let agentWs = null;
let backendWs = null;
let audioContext = null;
let micStream = null;
let micAnalyser = null;
let pcmWorkletNode = null;
let levelInterval = null;

// STT state
let recognition = null;
let sttActive = false;
let silenceTimer = null;
let lastInterimText = '';
let utteranceId = 0;

// Transcript UI state
let replyCount = 0;
let utteranceEntries = {};
const SPEAKER_LABELS = { client: 'Клієнт', sales: 'Сейлз' };

// Backend transcript uid tracking (stable id per utterance per speaker)
let backendUtteranceCounters = { client: 0, sales: 0 };
let backendActiveUids = {};

// DOM ref of BSR-finalized sales card, kept for Chirp v2 to update text
let salesChirpPendingEntry = null;

// VAD state: true = speaker is currently talking (gates BSR to prevent echo)
let vadState = { client: false, sales: false };

// Connection error flags (prevent onclose from overriding onerror red dot)
let agentErrored = false;
let backendErrored = false;

// Diagnostic counters
let sysAudioReceived = 0;
let sysAudioForwarded = 0;
let backendMsgsReceived = 0;

// ── Audio Levels ────────────────────────────────────────────

function calculateRMS(analyser) {
  const data = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(data);
  let sum = 0;
  for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
  return Math.sqrt(sum / data.length);
}

// ── Desktop Agent WebSocket ─────────────────────────────────

function connectAgent() {
  agentWs = new WebSocket(AGENT_WS_URL);

  agentWs.onopen = () => {
    console.log('[Agent] Connected');
    agentDot.className = 'conn-dot connected';
  };

  agentWs.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      return; // Not JSON
    }

    if (msg.type === 'system_rms') {
      const pct = Math.min(msg.rms * 500, 100);
      systemBar.style.width = pct + '%';
      systemValue.textContent = msg.rms.toFixed(4);
    }

    if (msg.type === 'system_audio' && msg.audio) {
      sysAudioReceived++;
      try {
        const binaryStr = atob(msg.audio);
        const bytes = new Uint8Array(binaryStr.length);
        for (let i = 0; i < binaryStr.length; i++) {
          bytes[i] = binaryStr.charCodeAt(i);
        }
        const sent = sendAudioFrame(0x00, bytes.buffer); // 0x00 = client/system audio
        if (sent) sysAudioForwarded++;
        if (sysAudioReceived % 50 === 1) {
          console.log(`[Audio] system_audio: received=${sysAudioReceived}, forwarded=${sysAudioForwarded}, bytes=${binaryStr.length}, backendOpen=${backendWs?.readyState === WebSocket.OPEN}`);
        }
      } catch (e) {
        console.error('[Audio] Error forwarding system audio:', e);
      }
    }

    if (msg.type === 'capture_started') {
      console.log('[Agent] Capture started, mode:', msg.mode);
    }

    if (msg.type === 'error') {
      console.error('[Agent] Error:', msg.message);
      errorMsg.textContent = '[Agent] ' + msg.message;
    }
  };

  agentWs.onclose = () => {
    console.log('[Agent] Disconnected');
    agentWs = null;
    if (!agentErrored) agentDot.className = 'conn-dot';
    // if errored: keep red dot so user knows the service is not running
  };

  agentWs.onerror = () => {
    agentErrored = true;
    agentDot.className = 'conn-dot error';
  };
}

// ── STT Backend WebSocket ───────────────────────────────────

function connectBackend() {
  backendWs = new WebSocket(BACKEND_WS_URL);
  backendWs.binaryType = 'arraybuffer';

  backendWs.onopen = () => {
    console.log('[Backend] Connected');
    backendDot.className = 'conn-dot connected';
    backendWs.send(JSON.stringify({ type: 'start_call' }));
  };

  backendWs.onmessage = (event) => {
    backendMsgsReceived++;
    // Log ALL backend messages to diagnose what's coming back
    if (typeof event.data === 'string') {
      try {
        const msg = JSON.parse(event.data);
        console.log('[Backend-Msg]', JSON.stringify(msg).substring(0, 200));
        // VAD events — early real-time trigger (speaking indicator)
        if (msg.type === 'vad_event') {
          const speaker = msg.speaker;
          if (msg.event === 'speech_start') vadState[speaker] = true;
          if (msg.event === 'speech_end') vadState[speaker] = false;
          if (msg.event === 'speech_start' && !backendActiveUids[speaker]) {
            if (speaker === 'sales') salesChirpPendingEntry = null;
            backendActiveUids[speaker] = 'be_' + speaker + '_' + (backendUtteranceCounters[speaker] || 0);
            handleTranscript(speaker, '...', true, backendActiveUids[speaker]);

            // Barge-in: force-finalize open backend card of the other speaker
            const otherSpeaker = speaker === 'client' ? 'sales' : 'client';
            const otherUid = backendActiveUids[otherSpeaker];
            if (otherUid) {
              const otherEntry = utteranceEntries[String(otherUid)];
              if (otherEntry) {
                const currentText = otherEntry.querySelector('.transcript-text').textContent.trim();
                if (currentText === '...' || currentText === '') {
                  // Keep card alive — Chirp v2 final will update it later (preserves chronological order)
                  otherEntry.classList.remove('interim');
                  otherEntry.classList.add('processing');
                } else {
                  otherEntry.classList.remove('interim', 'processing');
                  delete utteranceEntries[String(otherUid)];
                  backendActiveUids[otherSpeaker] = null;
                }
              } else {
                backendActiveUids[otherSpeaker] = null;
              }
            }

            // Barge-in: force-finalize active Browser SR interim for sales
            if (speaker === 'client' && lastInterimText) {
              const su = backendActiveUids['sales'];
              const sh = su !== undefined && su !== null;
              const au = sh ? su : utteranceId;
              bsrSoftFinalize(lastInterimText, au, sh);
              if (!sh) utteranceId++;
              lastInterimText = '';
              clearTimeout(silenceTimer);
              silenceTimer = null;
              if (recognition && sttActive) recognition.abort();
            }
          }
          if (msg.event === 'speech_end' && backendActiveUids[speaker]) {
            const uid = backendActiveUids[speaker];
            const entry = utteranceEntries[String(uid)];
            if (entry) {
              entry.classList.remove('interim');
              entry.classList.add('processing');
              // Do NOT overwrite text — BSR may have already set real text here
            }
          }
        }

        // Final transcript — authoritative text from chirp v2
        if (msg.type === 'transcript') {
          const speaker = msg.speaker;
          if (!backendActiveUids[speaker]) {
            backendActiveUids[speaker] = 'be_' + speaker + '_' + (backendUtteranceCounters[speaker] || 0);
          }
          const uid = backendActiveUids[speaker];

          // Estimate start time for late finals with no prior speech_start card
          if (!utteranceEntries[String(uid)] && !msg.interim) {
            const estimatedDuration = (msg.text.length / 15) * 1000;
            if (!window._chirpEstimatedStart) window._chirpEstimatedStart = {};
            window._chirpEstimatedStart[String(uid)] = Date.now() - estimatedDuration;
          }

          if (!msg.interim) {
            backendUtteranceCounters[speaker] = (backendUtteranceCounters[speaker] || 0) + 1;
            backendActiveUids[speaker] = null;

            // Remove BSR cards that are substrings of this Chirp result
            if (speaker === 'sales') removeDuplicateBsrCards(msg.text);

            // If BSR already soft-finalized this utterance, update its card with Chirp's accurate text
            if (speaker === 'sales' && salesChirpPendingEntry) {
              salesChirpPendingEntry.querySelector('.transcript-text').textContent = msg.text;
              salesChirpPendingEntry = null;
              transcriptList.scrollTop = transcriptList.scrollHeight;
              return;
            }
          }
          handleTranscript(speaker, msg.text, msg.interim, uid);
        }
      } catch (e) {
        console.log('[Backend-Msg] non-JSON text:', event.data.substring(0, 100));
      }
    } else {
      console.log('[Backend-Msg] binary message, size:', event.data.byteLength);
    }
  };

  backendWs.onclose = () => {
    console.log('[Backend] Disconnected');
    backendWs = null;
    if (!backendErrored) backendDot.className = 'conn-dot';
    // if errored: keep red dot so user knows the service is not running
  };

  backendWs.onerror = () => {
    backendErrored = true;
    backendDot.className = 'conn-dot error';
  };
}

function sendAudioFrame(trackByte, pcmData) {
  if (!backendWs || backendWs.readyState !== WebSocket.OPEN) return false;
  if (backendWs.bufferedAmount > 65536) return false;

  const pcmBytes = new Uint8Array(pcmData);
  const frame = new Uint8Array(1 + pcmBytes.length);
  frame[0] = trackByte;
  frame.set(pcmBytes, 1);
  backendWs.send(frame.buffer);
  return true;
}

// ── Mic Capture ─────────────────────────────────────────────

async function startMicCapture() {
  audioContext = new AudioContext({ sampleRate: 16000 });
  if (audioContext.state === 'suspended') await audioContext.resume();

  await audioContext.audioWorklet.addModule('pcm-processor.js');

  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000 }
    });
    const micSource = audioContext.createMediaStreamSource(micStream);

    micAnalyser = audioContext.createAnalyser();
    micAnalyser.fftSize = 2048;
    micSource.connect(micAnalyser);

    // PCM worklet sends mic audio to backend as track 0x01 (sales)
    pcmWorkletNode = new AudioWorkletNode(audioContext, 'pcm-processor');
    micSource.connect(pcmWorkletNode);
    pcmWorkletNode.port.onmessage = (e) => {
      sendAudioFrame(0x01, e.data);  // 0x01 = sales/mic
    };
  } catch (err) {
    console.error('[Mic] Capture failed:', err.message);
    errorMsg.textContent = 'Mic capture failed: ' + err.message;
    return;
  }

  // RMS interval for UI display only
  levelInterval = setInterval(() => {
    const micRMS = micAnalyser ? calculateRMS(micAnalyser) : 0;
    const micPct = Math.min(micRMS * 500, 100);
    micBar.style.width = micPct + '%';
    micValue.textContent = micRMS.toFixed(4);
  }, 500);
}

function stopMicCapture() {
  if (levelInterval) { clearInterval(levelInterval); levelInterval = null; }
  if (pcmWorkletNode) { pcmWorkletNode.disconnect(); pcmWorkletNode = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  micAnalyser = null;
  if (audioContext) { audioContext.close(); audioContext = null; }
}

// ── SpeechRecognition (browser-native STT for mic) ──────────

function startSpeechRecognition() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    console.warn('[STT] SpeechRecognition not available');
    return;
  }

  sttActive = true;
  utteranceId = 0;
  lastInterimText = '';

  recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = STT_LANG;

  recognition.onresult = (event) => {
    if (!vadState.sales) return; // mic picks up echo from speakers — ignore when sales VAD inactive
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      const text = result[0].transcript.trim();
      if (!text) continue;

      // SpeechRecognition listens to mic ONLY → always 'sales'
      const speaker = 'sales';
      // Share backend's active uid when available; fall back to own utteranceId
      const sharedUid = backendActiveUids[speaker];
      const isShared = sharedUid !== undefined && sharedUid !== null;
      const activeUid = isShared ? sharedUid : utteranceId;

      if (result.isFinal) {
        clearTimeout(silenceTimer);
        silenceTimer = null;
        lastInterimText = '';
        console.log('[STT-Sales] FINAL', `uid=${activeUid}`, `text="${text}"`);
        bsrSoftFinalize(text, activeUid, isShared);
        if (!isShared) utteranceId++;
        if (recognition && sttActive) { recognition.abort(); return; }
      } else {
        lastInterimText = text;

        if (text.length > MAX_UTTERANCE_CHARS) {
          clearTimeout(silenceTimer);
          silenceTimer = null;
          console.log('[STT-Sales] FINAL (max chars)', `uid=${activeUid}`, `text="${text}"`);
          bsrSoftFinalize(text, activeUid, isShared);
          lastInterimText = '';
          if (!isShared) utteranceId++;
          if (recognition && sttActive) recognition.abort();
          return;
        }

        handleTranscript(speaker, text, true, activeUid);

        clearTimeout(silenceTimer);
        silenceTimer = setTimeout(() => {
          if (lastInterimText) {
            // Re-read uid at fire time — it may have changed if Chirp closed the segment
            const su = backendActiveUids[speaker];
            const sh = su !== undefined && su !== null;
            const au = sh ? su : activeUid;
            console.log('[STT-Sales] FINAL (silence)', `uid=${au}`, `text="${lastInterimText}"`);
            bsrSoftFinalize(lastInterimText, au, sh);
            lastInterimText = '';
            if (!isShared) utteranceId++;
            if (recognition && sttActive) recognition.abort();
          }
        }, SILENCE_TIMEOUT);
      }
    }
  };

  recognition.onerror = (e) => {
    if (e.error !== 'no-speech') {
      console.error('[STT] Error:', e.error);
    }
  };

  recognition.onend = () => {
    if (sttActive) recognition.start();
  };

  recognition.start();
  console.log('[STT] SpeechRecognition started');
}

function stopSpeechRecognition() {
  sttActive = false;
  clearTimeout(silenceTimer);
  silenceTimer = null;
  lastInterimText = '';
  if (recognition) {
    recognition.onend = null;
    recognition.stop();
    recognition = null;
  }
}

// ── Transcript Helpers ──────────────────────────────────────

function bsrSoftFinalize(text, activeUid, isShared) {
  const key = String(activeUid);
  const entry = utteranceEntries[key];
  if (entry) {
    entry.querySelector('.transcript-text').textContent = text;
    entry.classList.remove('interim', 'processing');
    delete utteranceEntries[key];
    if (isShared) salesChirpPendingEntry = entry;
    replyCount++;
    transcriptCount.textContent = `${replyCount} реплік`;
    transcriptList.scrollTop = transcriptList.scrollHeight;
  }
  // If no entry: Chirp already closed it, or VAD never fired → skip silently
}

function removeDuplicateBsrCards(chirpText) {
  const cards = transcriptList.querySelectorAll('.transcript-entry.sales');
  const chirpLower = chirpText.toLowerCase();
  const last5 = Array.from(cards).slice(-5);
  for (const card of last5) {
    const cardText = card.querySelector('.transcript-text').textContent.trim();
    if (!cardText || cardText === '...') continue;
    const cardLower = cardText.toLowerCase();
    if (chirpLower.includes(cardLower) && card !== salesChirpPendingEntry) {
      card.remove();
      for (const [key, entry] of Object.entries(utteranceEntries)) {
        if (entry === card) { delete utteranceEntries[key]; break; }
      }
    }
  }
}

// ── Transcript UI ───────────────────────────────────────────

function createTranscriptEntry(speaker, text, isInterim) {
  const entry = document.createElement('div');
  entry.className = `transcript-entry ${speaker}${isInterim ? ' interim' : ''}`;

  const ts = new Date().toLocaleTimeString('uk-UA', { hour12: false });

  const meta = document.createElement('div');
  meta.className = 'transcript-meta';

  const speakerEl = document.createElement('span');
  speakerEl.className = 'transcript-speaker';
  speakerEl.textContent = SPEAKER_LABELS[speaker] || speaker;

  const timeEl = document.createElement('span');
  timeEl.className = 'transcript-time';
  timeEl.textContent = ts;

  meta.appendChild(speakerEl);
  meta.appendChild(timeEl);

  const textEl = document.createElement('div');
  textEl.className = 'transcript-text';
  if (isInterim && text === '...') {
    textEl.innerHTML = '<span class="typing-dots"><span>.</span><span>.</span><span>.</span></span>';
  } else {
    textEl.textContent = text;
  }

  entry.appendChild(meta);
  entry.appendChild(textEl);
  return entry;
}

function handleTranscript(speaker, text, interim, uid) {
  if (transcriptEmpty) transcriptEmpty.style.display = 'none';

  if (uid === undefined || uid === null) {
    uid = 'ws_' + speaker + '_' + Date.now();
  }

  const key = String(uid);
  const existing = utteranceEntries[key];

  if (existing) {
    existing.querySelector('.transcript-text').textContent = text;
    if (!interim) {
      existing.classList.remove('interim', 'processing');
      delete utteranceEntries[key];
      replyCount++;
    }
  } else {
    const entry = createTranscriptEntry(speaker, text, interim);
    // Use estimated start time from Chirp if available, otherwise now
    const estimatedStart = window._chirpEstimatedStart?.[key];
    entry.dataset.speechStartTime = String(estimatedStart || Date.now());
    if (estimatedStart) delete window._chirpEstimatedStart[key];

    // Chronological insert: find correct position by speechStartTime
    const allEntries = transcriptList.querySelectorAll('.transcript-entry');
    let insertBefore = null;
    for (let i = allEntries.length - 1; i >= 0; i--) {
      const entryTime = Number(allEntries[i].dataset.speechStartTime || 0);
      if (entryTime > Number(entry.dataset.speechStartTime)) {
        insertBefore = allEntries[i];
      } else {
        break;
      }
    }
    if (insertBefore) {
      transcriptList.insertBefore(entry, insertBefore);
    } else {
      transcriptList.appendChild(entry);
    }

    if (interim) {
      utteranceEntries[key] = entry;
    } else {
      replyCount++;
    }
  }

  transcriptCount.textContent = `${replyCount} реплік`;
  transcriptList.scrollTop = transcriptList.scrollHeight;
}

function clearTranscript() {
  transcriptList.innerHTML = '';
  if (transcriptEmpty) {
    transcriptList.appendChild(transcriptEmpty);
    transcriptEmpty.style.display = '';
  }
  replyCount = 0;
  utteranceEntries = {};
  salesChirpPendingEntry = null;
  transcriptCount.textContent = '0 реплік';
}

// ── Start / Stop ────────────────────────────────────────────

async function startCapture() {
  capturing = true;
  errorMsg.textContent = '';
  clearTranscript();

  // Reset connection error flags and backend uid state
  agentErrored = false;
  backendErrored = false;
  backendUtteranceCounters = { client: 0, sales: 0 };
  backendActiveUids = {};
  vadState = { client: false, sales: false };
  sysAudioReceived = 0;
  sysAudioForwarded = 0;
  backendMsgsReceived = 0;

  // 1. Connect to Desktop Agent
  connectAgent();
  // Send start command after connection
  const waitForAgent = new Promise((resolve) => {
    const check = setInterval(() => {
      if (agentWs && agentWs.readyState === WebSocket.OPEN) {
        agentWs.send(JSON.stringify({ command: 'start' }));
        clearInterval(check);
        resolve();
      }
    }, 100);
    // Timeout after 3s
    setTimeout(() => { clearInterval(check); resolve(); }, 3000);
  });

  // 2. Connect to STT backend
  connectBackend();

  // 3. Start mic capture
  await startMicCapture();

  // 4. Start Browser SR for sales interim (parallel to backend Chirp v2)
  startSpeechRecognition();

  await waitForAgent;

  // Warn if either service is not connected — client attribution won't work without both
  const warnings = [];
  if (!agentWs || agentWs.readyState !== WebSocket.OPEN) {
    warnings.push('Desktop Agent offline — немає системного аудіо');
  }
  if (!backendWs || backendWs.readyState !== WebSocket.OPEN) {
    warnings.push('STT Backend offline — немає транскрипції клієнта');
  }
  if (warnings.length) {
    errorMsg.textContent = '\u26A0 ' + warnings.join('; ');
  }

  setCapturingUI();
}

function stopCapture() {
  capturing = false;

  stopSpeechRecognition();
  stopMicCapture();

  // Stop Desktop Agent
  if (agentWs && agentWs.readyState === WebSocket.OPEN) {
    agentWs.send(JSON.stringify({ command: 'stop' }));
    agentWs.close();
    agentWs = null;
  }

  // Close backend
  if (backendWs && backendWs.readyState === WebSocket.OPEN) {
    backendWs.send(JSON.stringify({ type: 'end_call' }));
    backendWs.close();
    backendWs = null;
  }

  setIdleUI();
}

// ── UI Helpers ──────────────────────────────────────────────

function setCapturingUI() {
  btnStart.disabled = true;
  btnStop.disabled = false;
  statusDot.className = 'status-dot active';
  statusText.textContent = 'Capturing';
}

function setIdleUI() {
  btnStart.disabled = false;
  btnStop.disabled = true;
  statusDot.className = 'status-dot';
  statusText.textContent = 'Idle';
  systemBar.style.width = '0%';
  micBar.style.width = '0%';
  systemValue.textContent = '0.0000';
  micValue.textContent = '0.0000';
}

// ── Event Listeners ─────────────────────────────────────────

btnStart.addEventListener('click', async () => {
  btnStart.disabled = true;
  statusText.textContent = 'Starting...';
  try {
    await startCapture();
  } catch (err) {
    errorMsg.textContent = err.message;
    setIdleUI();
  }
});

btnStop.addEventListener('click', () => {
  btnStop.disabled = true;
  statusText.textContent = 'Stopping...';
  stopCapture();
});
