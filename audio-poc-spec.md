# Scriptius Audio Capture PoC — Extension Spec

## Мета

Мінімальний Chrome Extension, який:
1. Захоплює аудіо з вкладки HubSpot (де працює Unitalk Web Dialer) через `chrome.tabCapture`
2. Паралельно захоплює мікрофон сейлза через `getUserMedia`
3. Логує RMS-рівень обох потоків в console кожні 500мс
4. Показує live індикатори рівня на popup

Це proof-of-concept для валідації чи tabCapture захоплює голос клієнта з Unitalk, і чи getUserMedia не конфліктує з Unitalk за мікрофон.

---

## Що валідуємо

| # | Гіпотеза | Як перевірити | Очікуваний результат |
|---|----------|---------------|---------------------|
| 1 | tabCapture захоплює аудіо Unitalk | Під час дзвінка дивитись RMS tab-потоку | RMS > 0 коли клієнт говорить |
| 2 | getUserMedia працює паралельно з Unitalk | Увімкнути mic capture під час дзвінка | RMS mic > 0 коли сейлз говорить, Unitalk не ламається |
| 3 | Tab audio = голос клієнта (remote stream) | Порівняти: коли говорить клієнт — tab RMS високий, mic RMS низький | Потоки розділені по спікерах |
| 4 | tabCapture працює при перемиканні вкладок | Перемкнути на іншу вкладку під час дзвінка | RMS tab продовжує логуватись > 0 |

---

## Файлова структура

```
scriptius-audio-poc/
├── manifest.json
├── popup.html           # UI з кнопками Start/Stop та індикаторами рівня
├── popup.js             # Popup логіка: кнопки, відображення рівнів
├── offscreen.html       # Порожній HTML для Offscreen Document
├── offscreen.js         # Audio processing: AudioContext, AnalyserNode, RMS
├── background.js        # Service Worker: координація, message routing
└── icons/
    ├── icon16.png
    ├── icon48.png
    └── icon128.png
```

---

## manifest.json

```json
{
  "manifest_version": 3,
  "name": "Scriptius Audio PoC",
  "version": "0.1.0",
  "description": "Validates tab audio capture for Unitalk integration",
  "permissions": [
    "tabCapture",
    "offscreen"
  ],
  "action": {
    "default_popup": "popup.html",
    "default_icon": {
      "16": "icons/icon16.png",
      "48": "icons/icon48.png",
      "128": "icons/icon128.png"
    }
  },
  "background": {
    "service_worker": "background.js"
  },
  "icons": {
    "16": "icons/icon16.png",
    "48": "icons/icon48.png",
    "128": "icons/icon128.png"
  }
}
```

### Примітки до manifest

- **Не потрібен `host_permissions`** — tabCapture працює без host permissions, достатньо permission `tabCapture`.
- **Не потрібен `content_scripts`** — PoC не інжектить нічого в сторінку. Все керується з popup.
- **`offscreen`** permission — для створення Offscreen Document де живе AudioContext.
- **getUserMedia** не потребує окремого permission в manifest — він запитується в Offscreen Document (який має повний доступ до Web API).

---

## background.js — Service Worker

### Відповідальності

1. Створює / видаляє Offscreen Document
2. Отримує `streamId` від `chrome.tabCapture.getMediaStreamId()` і передає в offscreen
3. Маршрутизує повідомлення між popup і offscreen
4. Тримає стан: `{ capturing: false, tabId: null }`

### API та потік

```
Popup натискає "Start Tab Capture"
  → background отримує message { type: "start_tab_capture" }
  → background створює offscreen document (якщо не існує)
  → background викликає chrome.tabCapture.getMediaStreamId({ targetTabId })
  → background відправляє streamId в offscreen: { type: "start_tab_capture", streamId }
  
Offscreen починає capture і шле рівні:
  → { type: "audio_levels", tab_rms: 0.05, mic_rms: 0.02 }
  → background проксить в popup

Popup натискає "Stop"
  → background шле offscreen { type: "stop_capture" }
  → offscreen зупиняє всі MediaStream tracks
  → background видаляє offscreen document
```

### Ключові деталі реалізації

```javascript
// Створення offscreen document
await chrome.offscreen.createDocument({
  url: 'offscreen.html',
  reasons: ['USER_MEDIA'],     // дозволяє getUserMedia
  justification: 'Audio capture for sales call analysis'
});
```

**Reason `USER_MEDIA`** — це єдиний reason який дозволяє і tabCapture playback, і getUserMedia в Offscreen Document.

```javascript
// Отримання streamId для tabCapture
// ВАЖЛИВО: targetTabId — це ID активної вкладки з HubSpot
const streamId = await chrome.tabCapture.getMediaStreamId({
  targetTabId: tabId
});
```

**`chrome.tabCapture.getMediaStreamId()`** — це Manifest V3 спосіб. Він повертає streamId (string), який потім передається в offscreen, де через `navigator.mediaDevices.getUserMedia({ audio: { mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId } } })` отримується MediaStream.

**Отримання tabId активної вкладки:**
```javascript
const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
const tabId = tab.id;
```

---

## offscreen.js — Audio Processing

### Відповідальності

1. Отримує streamId від background → створює tab MediaStream
2. Створює mic MediaStream через getUserMedia
3. Для кожного потоку: AudioContext → MediaStreamSource → AnalyserNode
4. Кожні 500мс рахує RMS і відправляє в background

### Tab Capture

```javascript
// Отримуємо MediaStream з tab audio через streamId
const tabStream = await navigator.mediaDevices.getUserMedia({
  audio: {
    mandatory: {
      chromeMediaSource: 'tab',
      chromeMediaSourceId: streamId   // від background.js
    }
  }
});
```

### Mic Capture

```javascript
// Стандартний getUserMedia для мікрофона
const micStream = await navigator.mediaDevices.getUserMedia({
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    sampleRate: 16000
  }
});
```

### Audio Analysis Pipeline

Для КОЖНОГО потоку (tab і mic) створюється окремий ланцюг:

```
MediaStream
  → audioContext.createMediaStreamSource(stream)
  → analyserNode (fftSize: 2048)
  → [не підключаємо до destination — тільки аналіз]
```

**RMS калькуляція:**

```javascript
function calculateRMS(analyser) {
  const data = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(data);
  let sum = 0;
  for (let i = 0; i < data.length; i++) {
    sum += data[i] * data[i];
  }
  return Math.sqrt(sum / data.length);
}
```

**Інтервал логування:**

```javascript
setInterval(() => {
  const tabRMS = calculateRMS(tabAnalyser);
  const micRMS = calculateRMS(micAnalyser);
  
  // Логування в console offscreen document (видно в chrome://extensions → Inspect)
  console.log(`[Audio] Tab RMS: ${tabRMS.toFixed(4)} | Mic RMS: ${micRMS.toFixed(4)}`);
  
  // Відправка в background → popup
  chrome.runtime.sendMessage({
    type: 'audio_levels',
    tab_rms: tabRMS,
    mic_rms: micRMS
  });
}, 500);
```

### Повна послідовність в offscreen.js

```
1. Слухає message від background
2. При "start_tab_capture" з streamId:
   a. Створює AudioContext (sampleRate: 48000 — дефолт браузера)
   b. Отримує tab MediaStream через streamId
   c. Створює tabSource → tabAnalyser
   d. Отримує mic MediaStream через getUserMedia
   e. Створює micSource → micAnalyser
   f. Запускає setInterval для RMS логування
   g. Відправляє { type: "capture_started" }
3. При "stop_capture":
   a. clearInterval
   b. Зупиняє всі tracks обох MediaStream
   c. Закриває AudioContext
   d. Відправляє { type: "capture_stopped" }
```

### Обробка помилок

```javascript
// Tab capture може не спрацювати
try {
  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId } }
  });
} catch (err) {
  chrome.runtime.sendMessage({
    type: 'error',
    source: 'tab_capture',
    message: err.message
  });
  // Продовжуємо з mic-only якщо tab не працює
}

// Mic capture може конфліктувати з Unitalk
try {
  const micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
} catch (err) {
  chrome.runtime.sendMessage({
    type: 'error',
    source: 'mic_capture',
    message: err.message
  });
  // Продовжуємо з tab-only якщо mic не працює
}
```

---

## popup.html + popup.js — UI

### Макет

Мінімальний popup 320×400px:

```
┌──────────────────────────────┐
│  Scriptius Audio PoC         │
│                              │
│  Tab ID: 1234 (hubspot.com)  │
│                              │
│  [▶ Start Tab Capture]       │
│  [🎤 Start Mic Capture]      │
│  [⏹ Stop All]                │
│                              │
│  ── Audio Levels ──────────  │
│                              │
│  Tab (client):               │
│  ████████░░░░░░░  0.042      │
│                              │
│  Mic (sales):                │
│  ██░░░░░░░░░░░░░  0.008      │
│                              │
│  ── Log ───────────────────  │
│  12:03:05 Tab:0.042 Mic:0.008│
│  12:03:05 Tab:0.038 Mic:0.005│
│  12:03:04 Tab:0.051 Mic:0.003│
│  ...                         │
│                              │
│  Status: ● Capturing         │
└──────────────────────────────┘
```

### Кнопки та їх дії

| Кнопка | Дія | Message до background |
|--------|-----|----------------------|
| Start Tab Capture | Запускає tab + mic capture одночасно | `{ type: "start_tab_capture" }` |
| Stop All | Зупиняє capture | `{ type: "stop_capture" }` |

**Спрощення для PoC**: одна кнопка Start запускає і tab, і mic одночасно. Якщо один з них фейлиться — показуємо помилку але продовжуємо з іншим.

### Level Bars

Прості CSS progress bars оновлювані кожні 500мс через message від background:

```javascript
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'audio_levels') {
    // RMS зазвичай 0.0 - 0.3 для мови, нормалізуємо до 0-100%
    const tabPercent = Math.min(msg.tab_rms * 500, 100);
    const micPercent = Math.min(msg.mic_rms * 500, 100);
    tabBar.style.width = tabPercent + '%';
    micBar.style.width = micPercent + '%';
    tabValue.textContent = msg.tab_rms.toFixed(4);
    micValue.textContent = msg.mic_rms.toFixed(4);
    addLogEntry(msg.tab_rms, msg.mic_rms);
  }
});
```

### Log Panel

Scrollable div з останніми 50 записами (timestamp + RMS values). Для аналізу після дзвінка.

---

## Іконки

Для PoC достатньо згенерувати прості placeholder-іконки. Можна використати canvas для генерації або створити мінімальні PNG. Найпростіший варіант — однокольорові квадрати з літерою "S":

- `icon16.png` — 16×16px
- `icon48.png` — 48×48px  
- `icon128.png` — 128×128px

Колір: `#4CAF50` (green accent з design-spec Scriptius). Літера "S" білим.

---

## Як тестувати

### Підготовка

1. Відкрити `chrome://extensions/`
2. Увімкнути "Developer mode"
3. "Load unpacked" → обрати папку `scriptius-audio-poc/`
4. Відкрити HubSpot у вкладці, переконатись що Unitalk Web Dialer активний

### Тест 1: Tab Capture при дзвінку

1. Почати дзвінок через Unitalk
2. Клікнути іконку Extension → "Start Tab Capture"
3. Спостерігати Tab RMS рівень:
   - **Коли клієнт говорить** → Tab RMS повинен бути > 0.01
   - **Коли тиша** → Tab RMS ≈ 0.0001-0.001
   - **Якщо Tab RMS завжди 0** → tabCapture НЕ захоплює Unitalk аудіо (потрібен альтернативний підхід)

### Тест 2: Mic Capture паралельно

1. Під час активного дзвінка дивитись Mic RMS:
   - **Коли сейлз говорить** → Mic RMS > 0.01
   - **Unitalk продовжує працювати нормально** → getUserMedia не конфліктує
   - **Якщо Unitalk ламається** (немає звуку, дзвінок обривається) → конфлікт за мікрофон

### Тест 3: Speaker Separation

1. Під час дзвінка спостерігати обидва рівні одночасно:
   - **Клієнт говорить**: Tab RMS високий, Mic RMS низький → ✅ потоки розділені
   - **Сейлз говорить**: Tab RMS низький, Mic RMS високий → ✅ потоки розділені
   - **Обидва рівні високі завжди** → tab audio містить обох спікерів (echo/mix) → потрібна diarization

### Тест 4: Переключення вкладок

1. Під час активного capture перемкнутися на іншу вкладку
2. Повернутись на popup Extension і дивитись RMS:
   - **RMS продовжує оновлюватись** → tabCapture стабільний
   - **RMS став 0** → capture зупинився при switch

### Де дивитись console.log

- **Offscreen Document console**: `chrome://extensions/` → знайти Extension → "Inspect views: offscreen.html" → Console
- **Service Worker console**: `chrome://extensions/` → знайти Extension → "Inspect views: service worker" → Console
- **Popup console**: правий клік на popup → Inspect → Console

---

## Критерії успіху / провалу

| Результат | Що це означає | Наступний крок |
|-----------|---------------|----------------|
| Tab RMS > 0 при мові клієнта, Mic RMS > 0 при мові сейлза, обидва потоки незалежні | **Повний успіх** — dual-stream працює | Переходити до Етапу 1 міграції (PCM encoding + WebSocket до бекенду) |
| Tab RMS > 0 але містить обох спікерів | **Частковий успіх** — tab capture працює, але speaker separation потребує diarization | Тестувати Google STT diarization; або шукати спосіб ізолювати remote stream |
| Tab RMS завжди 0 | **Tab capture не працює** з Unitalk | Спробувати `chrome.desktopCapture.chooseDesktopMedia` як альтернативу; або перевірити чи Unitalk використовує окремий audio output |
| Mic capture ламає Unitalk | **Конфлікт за мікрофон** | Не використовувати getUserMedia; покластися на diarization в single tab stream |

---

## Можливі проблеми та рішення

### "Offscreen document already exists"

При повторному натисканні Start. Рішення:
```javascript
// background.js
async function ensureOffscreen() {
  const existing = await chrome.offscreen.hasDocument();
  if (!existing) {
    await chrome.offscreen.createDocument({ ... });
  }
}
```

### "Tab capture requires user gesture"

`chrome.tabCapture.getMediaStreamId()` потребує user gesture. Popup click — це user gesture, тому працює. Але якщо викликати programmatically — не спрацює.

### getUserMedia permission dialog

Перший раз браузер покаже діалог "Allow microphone". Це нормально. Після allow — запам'ятовується для Extension.

### AudioContext suspended

Браузер може створити AudioContext в suspended стані. Рішення:
```javascript
const audioContext = new AudioContext();
if (audioContext.state === 'suspended') {
  await audioContext.resume();
}
```

### Popup закривається при кліку поза ним

Це нормальна поведінка Chrome popup. Audio capture продовжує працювати в offscreen document. При повторному відкритті popup — запитати поточний стан у background і відновити UI.

---

## Порядок імплементації

1. **manifest.json** — скопіювати як є з цього документа
2. **Іконки** — створити placeholder PNG (зелений квадрат з "S")
3. **background.js** — message routing, offscreen lifecycle, tabCapture.getMediaStreamId
4. **offscreen.html** — порожній HTML з `<script src="offscreen.js">`
5. **offscreen.js** — tab capture + mic capture + RMS analysis + message sending
6. **popup.html** — UI layout з кнопками та level bars
7. **popup.js** — кнопки, message listeners, UI updates

Кожен файл тестується окремо:
- Після 1-3: Extension завантажується без помилок в chrome://extensions
- Після 4-5: "Start" створює offscreen, console.log показує RMS
- Після 6-7: Popup показує live рівні
