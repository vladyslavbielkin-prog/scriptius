// Scriptius Web App — AI Sales Assistant
// Two WebSocket connections:
//   ws://localhost:9001 — Desktop Agent (system audio capture)
//   ws://[server]/audio — STT Backend (audio + transcripts + AI analysis)

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

// ── State ───────────────────────────────────────────────────
let capturing = false;
let agentWs = null;
let backendWs = null;
let audioContext = null;
let micStream = null;
let micAnalyser = null;
let pcmWorkletNode = null;
let levelInterval = null;
let callStartTime = null;

// Transcript UI state
let replyCount = 0;
let utteranceEntries = {};
const SPEAKER_LABELS = { client: 'Client', sales: 'Sales Rep' };

// Backend transcript uid tracking
let backendUtteranceCounters = { client: 0, sales: 0 };
let backendActiveUids = {};
let finalizeTimers = {};
const FINALIZE_DELAY = 500;

// VAD state: true = speaker is currently talking
let vadState = { client: false, sales: false };

// Connection error flags
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
    try { msg = JSON.parse(event.data); } catch (e) { return; }

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
        for (let i = 0; i < binaryStr.length; i++) bytes[i] = binaryStr.charCodeAt(i);
        const sent = sendAudioFrame(0x00, bytes.buffer);
        if (sent) sysAudioForwarded++;
        if (sysAudioReceived % 50 === 1) {
          console.log(`[Audio] system_audio: received=${sysAudioReceived}, forwarded=${sysAudioForwarded}, bytes=${binaryStr.length}`);
        }
      } catch (e) {
        console.error('[Audio] Error forwarding system audio:', e);
      }
    }

    if (msg.type === 'capture_started') console.log('[Agent] Capture started, mode:', msg.mode);
    if (msg.type === 'error') {
      console.error('[Agent] Error:', msg.message);
      errorMsg.textContent = '[Agent] ' + msg.message;
    }
  };

  agentWs.onclose = () => {
    console.log('[Agent] Disconnected');
    agentWs = null;
    if (!agentErrored) agentDot.className = 'conn-dot';
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
    if (typeof event.data !== 'string') return;

    let msg;
    try { msg = JSON.parse(event.data); } catch (e) { return; }
    console.log('[Backend-Msg]', JSON.stringify(msg).substring(0, 300));

    // ── VAD events ──
    if (msg.type === 'vad_event') {
      const speaker = msg.speaker;
      if (msg.event === 'speech_start') vadState[speaker] = true;
      if (msg.event === 'speech_end') vadState[speaker] = false;

      if (msg.event === 'speech_start' && !backendActiveUids[speaker]) {
        backendActiveUids[speaker] = 'be_' + speaker + '_' + (backendUtteranceCounters[speaker] || 0);
        handleTranscript(speaker, '...', true, backendActiveUids[speaker]);

        // Barge-in: force-finalize other speaker's active card
        const otherSpeaker = speaker === 'client' ? 'sales' : 'client';
        if (backendActiveUids[otherSpeaker]) {
          if (finalizeTimers[otherSpeaker]) {
            clearTimeout(finalizeTimers[otherSpeaker]);
            finalizeTimers[otherSpeaker] = null;
          }
          finalizeActiveCard(otherSpeaker);
        }
      }

      if (msg.event === 'speech_end' && backendActiveUids[speaker]) {
        const uid = backendActiveUids[speaker];
        const entry = utteranceEntries[String(uid)];
        if (entry) {
          entry.classList.remove('interim');
          entry.classList.add('processing');
        }
        scheduleFinalize(speaker);
      }
    }

    // ── Transcript ──
    if (msg.type === 'transcript') {
      const speaker = msg.speaker;
      if (!backendActiveUids[speaker]) {
        backendActiveUids[speaker] = 'be_' + speaker + '_' + (backendUtteranceCounters[speaker] || 0);
      }
      const uid = backendActiveUids[speaker];

      if (!utteranceEntries[String(uid)] && !msg.interim) {
        const estimatedDuration = (msg.text.length / 15) * 1000;
        if (!window._chirpEstimatedStart) window._chirpEstimatedStart = {};
        window._chirpEstimatedStart[String(uid)] = Date.now() - estimatedDuration;
      }

      // Update card text — keep mutable (don't reset uid)
      handleTranscript(speaker, msg.text, true, uid);

      // Chirp FINAL → set finalizing state + schedule delayed finalization
      if (!msg.interim) {
        const entry = utteranceEntries[String(uid)];
        if (entry) {
          entry.classList.remove('interim', 'processing');
          entry.classList.add('finalizing');
        }
        scheduleFinalize(speaker);
      }
    }

    // ── AI Analysis ──
    if (msg.type === 'analysis') {
      handleAnalysis(msg.data);
    }

    // ── Value Questions ──
    if (msg.type === 'valueQuestions') {
      handleValueQuestions(msg);
    }
  };

  backendWs.onclose = () => {
    console.log('[Backend] Disconnected');
    backendWs = null;
    if (!backendErrored) backendDot.className = 'conn-dot';
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

    pcmWorkletNode = new AudioWorkletNode(audioContext, 'pcm-processor');
    micSource.connect(pcmWorkletNode);
    pcmWorkletNode.port.onmessage = (e) => {
      sendAudioFrame(0x01, e.data);
    };
  } catch (err) {
    console.error('[Mic] Capture failed:', err.message);
    errorMsg.textContent = 'Mic capture failed: ' + err.message;
    return;
  }

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

// ── Finalize Helpers ─────────────────────────────────────────

function scheduleFinalize(speaker) {
  if (finalizeTimers[speaker]) clearTimeout(finalizeTimers[speaker]);
  finalizeTimers[speaker] = setTimeout(() => {
    finalizeTimers[speaker] = null;
    finalizeActiveCard(speaker);
  }, FINALIZE_DELAY);
}

function finalizeActiveCard(speaker) {
  const uid = backendActiveUids[speaker];
  if (!uid) return;

  const entry = utteranceEntries[String(uid)];
  if (entry) {
    const text = entry.querySelector('.transcript-text').textContent.trim();
    if (text && text !== '...') {
      removeDuplicateCards(speaker, text);
      entry.classList.remove('interim', 'processing', 'finalizing');
      delete utteranceEntries[String(uid)];
      replyCount++;
      transcriptCount.textContent = `${replyCount} replies`;
    } else {
      entry.remove();
      delete utteranceEntries[String(uid)];
    }
  }
  backendUtteranceCounters[speaker] = (backendUtteranceCounters[speaker] || 0) + 1;
  backendActiveUids[speaker] = null;
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

function findOverlappingCard(speaker, newText) {
  const newLower = newText.toLowerCase();
  const cards = transcriptList.querySelectorAll(`.transcript-entry.${speaker}:not(.interim):not(.processing):not(.finalizing)`);
  const recent = Array.from(cards).slice(-5);
  for (const card of recent) {
    const cardText = card.querySelector('.transcript-text').textContent.trim().toLowerCase();
    if (!cardText || cardText === '...') continue;
    if (cardText === newLower) return { action: 'skip' };
    if (newLower.includes(cardText)) return { action: 'replace', card };
    if (cardText.includes(newLower)) return { action: 'skip' };
  }
  return { action: 'create' };
}

function removeDuplicateCards(speaker, text) {
  const textLower = text.toLowerCase();
  const cards = transcriptList.querySelectorAll(`.transcript-entry.${speaker}:not(.interim):not(.processing):not(.finalizing)`);
  const recent = Array.from(cards).slice(-5);
  for (const card of recent) {
    const cardText = card.querySelector('.transcript-text').textContent.trim().toLowerCase();
    if (!cardText || cardText === '...') continue;
    if (textLower.includes(cardText)) {
      card.remove();
      replyCount--;
    }
  }
}

function handleTranscript(speaker, text, interim, uid) {
  if (transcriptEmpty) transcriptEmpty.style.display = 'none';

  if (uid === undefined || uid === null) {
    uid = 'ws_' + speaker + '_' + Date.now();
  }

  const key = String(uid);
  let existing = utteranceEntries[key];

  // Immutability guard: if entry lost all mutable classes, it's finalized — don't mutate
  if (existing && !existing.classList.contains('interim') && !existing.classList.contains('processing') && !existing.classList.contains('finalizing')) {
    delete utteranceEntries[key];
    existing = undefined;
  }

  if (existing) {
    existing.querySelector('.transcript-text').textContent = text;
    if (!interim) {
      existing.classList.remove('interim', 'processing');
      delete utteranceEntries[key];
      replyCount++;
    }
  } else {
    // Dedup: check if new text overlaps with recent finalized cards
    if (text && text !== '...') {
      const overlap = findOverlappingCard(speaker, text);
      if (overlap.action === 'skip') return;
      if (overlap.action === 'replace') {
        overlap.card.remove();
        replyCount--;
      }
    }

    const entry = createTranscriptEntry(speaker, text, interim);
    const estimatedStart = window._chirpEstimatedStart?.[key];
    entry.dataset.speechStartTime = String(estimatedStart || Date.now());
    if (estimatedStart) delete window._chirpEstimatedStart[key];

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

  transcriptCount.textContent = `${replyCount} replies`;
  transcriptList.scrollTop = transcriptList.scrollHeight;
}

function clearTranscript() {
  Object.keys(finalizeTimers).forEach(k => { clearTimeout(finalizeTimers[k]); finalizeTimers[k] = null; });
  transcriptList.innerHTML = '';
  if (transcriptEmpty) {
    transcriptList.appendChild(transcriptEmpty);
    transcriptEmpty.style.display = '';
  }
  replyCount = 0;
  utteranceEntries = {};
  transcriptCount.textContent = '0 replies';
}

// ══════════════════════════════════════════════════════════════
// ── UI Rendering (Analysis, Profile, Questions, Summary) ────
// ══════════════════════════════════════════════════════════════

// ── Analysis Handler ────────────────────────────────────────

function handleAnalysis(data) {
  if (!data) return;
  if (data.qualificationStatus) handleQualificationStatus(data.qualificationStatus);
  if (data.valueStatus) handleValueStatus(data.valueStatus);
  if (data.clientProfile) updateClientProfile(data.clientProfile);
  if (data.summary) renderSummary(data.summary);
  if (data.sentiment) renderSentiment(data.sentiment);
  if (data.objectionHandling) renderObjection(data.objectionHandling);
  if (data.recommendedOffer) renderOffer(data.recommendedOffer);
}

// ── Summary Rendering ───────────────────────────────────────

function renderSummary(summary) {
  const el = document.getElementById('summaryList');
  const items = Array.isArray(summary) ? summary : [summary];
  el.innerHTML = items.map(s => `<li>${s}</li>`).join('');
}

// ── Sentiment Rendering ─────────────────────────────────────

function renderSentiment(sentiment) {
  const badge = document.getElementById('sentimentBadge');
  const row = document.getElementById('sentimentRow');

  const text = typeof sentiment === 'string'
    ? sentiment
    : `${sentiment.label || sentiment.value || 'Neutral'} \u2014 ${sentiment.reason || ''}`;

  badge.textContent = text;
  badge.className = 'sentiment-badge';

  const lower = text.toLowerCase();
  if (lower.includes('positive') || lower.includes('\u043F\u043E\u0437\u0438\u0442\u0438\u0432')) badge.classList.add('positive');
  else if (lower.includes('negative') || lower.includes('\u043D\u0435\u0433\u0430\u0442\u0438\u0432')) badge.classList.add('negative');
  else if (lower.includes('skepti') || lower.includes('\u0441\u043A\u0435\u043F\u0442\u0438')) badge.classList.add('skeptical');
  else badge.classList.add('neutral');

  row.style.display = 'flex';
  updateReadiness(lower);
}

// ── Objection Rendering ─────────────────────────────────────

function renderObjection(text) {
  document.getElementById('objectionText').textContent = text;
  document.getElementById('objectionRow').style.display = 'flex';
}

// ── Offer Rendering ─────────────────────────────────────────

function renderOffer(offerData) {
  const offerEl = document.getElementById('offerText');
  const priceRow = document.getElementById('priceRow');

  if (typeof offerData === 'object' && offerData.items) {
    const items = Array.isArray(offerData.items) ? offerData.items : [];
    offerEl.className = 'offer-text';
    offerEl.innerHTML = '<ul>' + items.map(item =>
      `<li>${item}</li>`
    ).join('') + '</ul>';

    if (offerData.price) {
      priceRow.style.display = 'block';
      document.getElementById('priceCurrent').textContent = offerData.price;
    }
  } else {
    offerEl.className = 'offer-text';
    offerEl.textContent = typeof offerData === 'string' ? offerData : JSON.stringify(offerData);
  }
}

// ── Readiness Bar ───────────────────────────────────────────

function updateReadiness(sentimentText) {
  const segments = document.querySelectorAll('.readiness-segment');
  const valueEl = document.getElementById('readinessValue');
  let filled = 1;
  let label = 'Low';

  if (sentimentText.includes('positive') || sentimentText.includes('\u043F\u043E\u0437\u0438\u0442\u0438\u0432')) {
    filled = 5; label = 'Ready';
  } else if (sentimentText.includes('neutral') || sentimentText.includes('\u043D\u0435\u0439\u0442\u0440\u0430\u043B')) {
    filled = 3; label = 'Neutral';
  } else if (sentimentText.includes('skepti') || sentimentText.includes('\u0441\u043A\u0435\u043F\u0442\u0438')) {
    filled = 2; label = 'Skeptical';
  } else if (sentimentText.includes('negative') || sentimentText.includes('\u043D\u0435\u0433\u0430\u0442\u0438\u0432')) {
    filled = 1; label = 'Low';
  }

  const colors = { 1: '#8B1A1A', 2: '#E8837C', 3: '#E8C547', 4: '#6BBF6A', 5: '#2D7A2D' };
  valueEl.textContent = label;
  valueEl.style.color = colors[filled] || '#fff';

  segments.forEach((seg, i) => {
    seg.classList.toggle('filled', i < filled);
    if (i < filled) seg.style.background = colors[filled];
    else seg.style.background = '';
  });
}

// ── Client Profile ──────────────────────────────────────────

function updateClientProfile(profile) {
  if (!profile || typeof profile !== 'object') return;

  if (profile.name) {
    document.getElementById('clientName').textContent = profile.name;
    const parts = profile.name.trim().split(/\s+/);
    const avatarEl = document.getElementById('avatar');
    avatarEl.textContent = parts.map(p => p[0]).join('').toUpperCase().slice(0, 2);
    avatarEl.style.background = '#6B7D5E';
  }

  if (profile.role || profile.company) {
    const pieces = [profile.role, profile.company].filter(Boolean);
    document.getElementById('clientRole').textContent = pieces.join(', ');
  }

  if (profile.industry) document.getElementById('statIndustry').textContent = profile.industry;
  if (profile.experience) document.getElementById('statExperience').textContent = profile.experience;
  if (profile.company) document.getElementById('statCompany').textContent = profile.company;
  if (profile.role) document.getElementById('fieldPosition').textContent = profile.role;
  if (profile.painPoints) document.getElementById('fieldPainPoints').textContent = profile.painPoints;
  if (profile.goal) document.getElementById('fieldGoal').textContent = profile.goal;

  if (profile.course) {
    const sel = document.getElementById('courseSelect');
    const opts = Array.from(sel.options);
    const exact = opts.find(o => o.value === profile.course);
    const partial = opts.find(o => o.value && profile.course.toLowerCase().includes(o.value.toLowerCase()));
    if (exact) sel.value = exact.value;
    else if (partial) sel.value = partial.value;
    if (sel.value) document.getElementById('priceCurrent').textContent = '$500';
  }
}

// ── Qualification Status ────────────────────────────────────

function handleQualificationStatus(statuses) {
  if (!Array.isArray(statuses)) return;

  statuses.forEach(({ id, status }) => {
    const item = document.querySelector(`.question-item[data-qid="${id}"]`);
    if (!item) return;

    const checkbox = item.querySelector('.question-check');

    if (status === 'asked') {
      if (!checkbox.checked) {
        checkbox.checked = true;
        item.classList.remove('dismissed');
        item.classList.add('auto-checked');
        updateSectionStates();
      }
    } else if (status === 'answered') {
      if (!checkbox.checked && !item.classList.contains('dismissed')) {
        item.classList.add('dismissed');
      }
    }
  });
}

// ── Value Questions ─────────────────────────────────────────

function handleValueQuestions(msg) {
  const { questions, batch } = msg;
  if (!Array.isArray(questions) || questions.length === 0) return;

  const body = document.getElementById('valueBody');

  if (batch === 1) {
    body.innerHTML = '';
  }

  const group = document.createElement('div');
  group.className = 'value-group';
  group.dataset.group = String(batch);
  group.innerHTML = `<div class="value-group-label">Round ${batch}</div>`;

  questions.forEach(q => {
    const item = document.createElement('div');
    item.className = 'question-item';
    item.dataset.qid = q.id;
    item.innerHTML = `
      <input type="checkbox" class="question-check" />
      <span class="question-text">${q.text}</span>
      <span class="question-badge"></span>
    `;
    group.appendChild(item);

    item.querySelector('.question-check').addEventListener('change', () => {
      updateSectionStates();
    });
  });

  body.appendChild(group);

  // Flash animation
  const section = document.getElementById('sectionValue');
  section.classList.add('questions-loaded');
  setTimeout(() => section.classList.remove('questions-loaded'), 1500);
}

// ── Value Status ────────────────────────────────────────────

function handleValueStatus(statuses) {
  if (!Array.isArray(statuses)) return;

  statuses.forEach(({ id, status }) => {
    const item = document.querySelector(`.question-item[data-qid="${id}"]`);
    if (!item) return;

    const checkbox = item.querySelector('.question-check');

    if (status === 'asked') {
      if (!checkbox.checked) {
        checkbox.checked = true;
        item.classList.remove('dismissed');
        item.classList.add('auto-checked');
        updateSectionStates();
      }
    } else if (status === 'answered') {
      if (!checkbox.checked && !item.classList.contains('dismissed')) {
        item.classList.add('dismissed');
      }
    }
  });
}

// ── Section State Management ────────────────────────────────

function updateSectionStates() {
  const sections = [
    document.getElementById('sectionQualification'),
    document.getElementById('sectionValue'),
  ];

  let firstUncovered = null;

  sections.forEach(section => {
    const checks = section.querySelectorAll('.question-check');
    if (checks.length === 0) return;

    const checked = Array.from(checks).filter(c => c.checked).length;
    const allChecked = checked === checks.length;

    const dot = section.querySelector('.section-dot');
    const badge = section.querySelector('.section-badge');

    if (allChecked) {
      section.classList.add('covered');
      section.classList.remove('active');
      dot.className = 'section-dot done';
      badge.className = 'section-badge covered';
      badge.textContent = 'Covered';
    } else {
      section.classList.remove('covered');

      if (!firstUncovered) {
        firstUncovered = section;
        section.classList.add('active');
        dot.className = 'section-dot active';
        badge.className = 'section-badge ask-now';
        badge.textContent = 'Ask Now';
      } else {
        section.classList.remove('active');
        dot.className = 'section-dot';
        badge.className = 'section-badge up-next';
        badge.textContent = 'Up Next';
      }
    }
  });
}

function initCheckboxListeners() {
  document.querySelectorAll('.question-check').forEach(checkbox => {
    checkbox.addEventListener('change', () => {
      const item = checkbox.closest('.question-item');
      if (item) item.classList.remove('dismissed');
      updateSectionStates();
    });
  });
}

// ── Call Summary ────────────────────────────────────────────

function showCallSummary() {
  const duration = callStartTime ? Math.round((Date.now() - callStartTime) / 60000) : 0;

  const now = new Date();
  const dateStr = now.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  document.getElementById('summaryMeta').textContent = `${dateStr} \u00B7 ${duration} min`;

  const sections = [
    { id: 'sectionQualification', detailId: 'scoreQualDetail' },
    { id: 'sectionValue', detailId: 'scoreValueDetail' },
  ];

  let totalChecked = 0;
  let totalQuestions = 0;

  sections.forEach(({ id, detailId }) => {
    const el = document.getElementById(id);
    const checks = el.querySelectorAll('.question-check');
    const checked = Array.from(checks).filter(c => c.checked).length;
    totalChecked += checked;
    totalQuestions += checks.length;
    document.getElementById(detailId).textContent = `${checked} of ${checks.length} questions covered`;
  });

  document.getElementById('totalQuestions').textContent = `${totalChecked} of ${totalQuestions}`;

  // Copy notes
  const notesBody = document.getElementById('notesBody');
  const summaryNotesBody = document.getElementById('summaryNotesBody');
  summaryNotesBody.innerHTML = notesBody.innerHTML;

  navigateTo('call-summary');
}

// ── Routing ─────────────────────────────────────────────────

function navigateTo(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  const target = document.getElementById(`page-${page}`);
  if (target) target.classList.add('active');

  document.querySelectorAll('.nav-link').forEach(l => {
    l.classList.toggle('active', l.dataset.page === page);
  });
}

function initRouter() {
  document.querySelectorAll('.nav-link').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const page = link.dataset.page;
      window.location.hash = page;
      navigateTo(page);
    });
  });

  window.addEventListener('hashchange', () => {
    const page = window.location.hash.slice(1) || 'calls';
    navigateTo(page);
  });

  const initial = window.location.hash.slice(1) || 'calls';
  navigateTo(initial);
}

// ── Transcript Panel Toggle ─────────────────────────────────

function toggleTranscript() {
  const isOpen = document.body.classList.toggle('transcript-open');
  document.getElementById('transcriptPanel').setAttribute('aria-hidden', isOpen ? 'false' : 'true');
}

// ── Start / Stop ────────────────────────────────────────────

async function startCapture() {
  capturing = true;
  callStartTime = Date.now();
  errorMsg.textContent = '';
  clearTranscript();

  // Reset state
  agentErrored = false;
  backendErrored = false;
  backendUtteranceCounters = { client: 0, sales: 0 };
  backendActiveUids = {};
  vadState = { client: false, sales: false };
  sysAudioReceived = 0;
  sysAudioForwarded = 0;
  backendMsgsReceived = 0;

  // Navigate to calls page
  navigateTo('calls');

  // 1. Connect to Desktop Agent
  connectAgent();
  const waitForAgent = new Promise((resolve) => {
    const check = setInterval(() => {
      if (agentWs && agentWs.readyState === WebSocket.OPEN) {
        agentWs.send(JSON.stringify({ command: 'start' }));
        clearInterval(check);
        resolve();
      }
    }, 100);
    setTimeout(() => { clearInterval(check); resolve(); }, 3000);
  });

  // 2. Connect to STT backend
  connectBackend();

  // 3. Start mic capture
  await startMicCapture();

  await waitForAgent;

  // Warnings
  const warnings = [];
  if (!agentWs || agentWs.readyState !== WebSocket.OPEN) {
    warnings.push('Desktop Agent offline \u2014 no system audio');
  }
  if (!backendWs || backendWs.readyState !== WebSocket.OPEN) {
    warnings.push('STT Backend offline \u2014 no transcription');
  }
  if (warnings.length) {
    errorMsg.textContent = '\u26A0 ' + warnings.join('; ');
  }

  setCapturingUI();
}

function stopCapture() {
  capturing = false;

  Object.keys(finalizeTimers).forEach(k => { clearTimeout(finalizeTimers[k]); finalizeTimers[k] = null; });
  stopMicCapture();

  // Send notes before closing
  const notesText = document.getElementById('notesBody').textContent.trim();
  if (notesText && backendWs && backendWs.readyState === WebSocket.OPEN) {
    backendWs.send(JSON.stringify({ type: 'note', text: notesText }));
  }

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
  showCallSummary();
}

// ── UI Helpers ──────────────────────────────────────────────

function setCapturingUI() {
  btnStart.disabled = true;
  btnStop.disabled = false;
  statusDot.className = 'status-dot active';
  statusText.textContent = 'Live';
  document.getElementById('centerEmpty').classList.add('hidden');
  document.getElementById('sectionWrap').classList.remove('hidden');
}

function setIdleUI() {
  btnStart.disabled = false;
  btnStop.disabled = true;
  statusDot.className = 'status-dot';
  statusText.textContent = 'Idle';
  micBar.style.width = '0%';
  document.getElementById('centerEmpty').classList.remove('hidden');
  document.getElementById('sectionWrap').classList.add('hidden');
}

// ── Course Selector ─────────────────────────────────────────

function initCourseSelector() {
  document.getElementById('courseSelect').addEventListener('change', (e) => {
    const course = e.target.value;
    const priceRow = document.getElementById('priceRow');
    const priceEl = document.getElementById('priceCurrent');

    e.target.classList.toggle('has-value', !!course);

    if (course) {
      priceRow.style.display = 'block';
      priceEl.textContent = '$500';
    } else {
      priceRow.style.display = 'none';
      priceEl.textContent = '';
    }

    if (course && backendWs && backendWs.readyState === WebSocket.OPEN) {
      backendWs.send(JSON.stringify({ type: 'clientInfo', data: { course } }));
    }
  });
}

// ── Init ────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initRouter();
  initCheckboxListeners();
  initCourseSelector();
  updateSectionStates();

  // Transcript panel toggle
  document.getElementById('transcriptToggle').addEventListener('click', toggleTranscript);
  document.getElementById('transcriptClose').addEventListener('click', toggleTranscript);

  // Start/Stop
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
});
