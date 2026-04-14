# Scriptius — Локальний запуск

Scriptius транскрибує дзвінки в реальному часі: окремо голос клієнта (системне аудіо) та голос продавця (мікрофон), з AI-аналізом розмови.

Ця інструкція описує як підняти весь продукт локально на своєму Mac.

---

## Вимоги

- **macOS 14.2+** (Sonoma) — обов'язково для захоплення системного аудіо
- **Python 3.12+** — для бекенд-сервера
- **Xcode Command Line Tools** — для збірки Desktop Agent (Swift)
- **Google Cloud акаунт** — для транскрипції (Speech-to-Text) та AI-аналізу (Gemini)

---

## Крок 1 — Клонувати репозиторій

```bash
git clone https://github.com/vladyslavbielkin-prog/scriptius.git
cd scriptius
```

---

## Крок 2 — Встановити Xcode Command Line Tools

```bash
xcode-select --install
```

З'явиться діалог — натисни **Install**. Дочекайся завершення (~5 хв).

Якщо вже встановлено — команда скаже `already installed`, це ОК.

---

## Крок 3 — Зібрати Desktop Agent

```bash
cd scriptius-native
swift build -c release
cd ..
```

Перша збірка займає ~1-2 хвилини. В кінці побачиш `Build complete!`.

---

## Крок 4 — Дозволити запис екрану

> Без цього кроку системний аудіо (голос клієнта) не буде захоплюватись.

1. Відкрий **System Settings** → **Privacy & Security** → **Screen Recording**
2. Знайди **Terminal** у списку та увімкни перемикач
3. Якщо Terminal там немає — натисни `+` і додай вручну (`/Applications/Utilities/Terminal.app`)

---

## Крок 5 — Налаштувати Python-середовище

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r server/requirements.txt
```

> Щоразу коли відкриваєш новий Terminal для сервера — активуй venv:
> `source venv/bin/activate`

---

## Крок 6 — Налаштувати змінні середовища

```bash
cp server/.env.example server/.env
```

Відкрий файл `server/.env` і заповни значення. Детальна інструкція по отриманню ключів — у розділі [Отримання Google Cloud ключів](#отримання-google-cloud-ключів) нижче.

---

## Крок 7 — Запустити (потрібно 3 Terminal вікна)

Для роботи потрібні 3 Terminal вікна (або вкладки):

| Terminal | Процес | Що робить |
|----------|--------|-----------|
| 1 | Сервер | Бекенд + роздає UI |
| 2 | Desktop Agent | Захоплює системне аудіо |
| 3 | Вільний | Git, редагування, копіювання файлів |

### Terminal 1 — Сервер

```bash
cd scriptius/server
source ../venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
```

Побачиш: `Uvicorn running on http://0.0.0.0:8000` — сервер працює. Залиш відкритим.

### Terminal 2 — Desktop Agent

```bash
cd scriptius/scriptius-native
.build/release/ScriptiusAudio --server
```

Побачиш: `[Server] Listening on ws://localhost:9001` — агент готовий. Залиш відкритим.

### Terminal 3 — Робочий

Використовуй для git, редагування файлів, копіювання webapp → server/public тощо.

---

## Крок 8 — Відкрити Scriptius

Відкрий у браузері: **http://localhost:8000**

1. Браузер запитає доступ до мікрофона — дозволь
2. Натисни **Start**
3. Транскрипція почнеться автоматично

Для роботи потрібні обидва процеси: сервер (Terminal 1) і агент (Terminal 2).

---

## Отримання Google Cloud ключів

### Google Cloud Speech-to-Text (транскрипція)

1. Зайди на [Google Cloud Console](https://console.cloud.google.com/)
2. Створи новий проект (або використай існуючий)
3. Запам'ятай **Project ID** — це значення для `GOOGLE_PROJECT_ID`
4. Увімкни **Cloud Speech-to-Text API**:
   - Меню → APIs & Services → Library
   - Знайди "Cloud Speech-to-Text API" → Enable
5. Створи Service Account:
   - Меню → IAM & Admin → Service Accounts
   - Create Service Account
   - Роль: `Cloud Speech Client` (або `Cloud Speech-to-Text User`)
   - Створи ключ: Keys → Add Key → Create new key → JSON
6. Завантажений JSON-файл — це твій ключ. Відкрий його та **скопіюй весь вміст** (одним рядком) у `GOOGLE_CREDENTIALS_JSON` у файлі `.env`

### Gemini API (AI-аналіз)

1. Зайди на [Google AI Studio](https://aistudio.google.com/)
2. Натисни **Get API Key** → Create API Key
3. Скопіюй ключ у `GEMINI_API_KEY` у файлі `.env`

---

## Що буде працювати без ключів

Якщо поки не маєш Google Cloud ключів:

- UI завантажиться і відкриється
- Мікрофон і системне аудіо будуть захоплюватись
- **Не працюватиме**: транскрипція (STT) та AI-аналіз

Це корисно для роботи над інтерфейсом без налаштування хмарних сервісів.

---

## Структура проекту для розробки

```
scriptius/
├── webapp/            ← UI (HTML, JS, CSS) — редагуй тут
│   ├── app.js         ← основна логіка фронтенду
│   ├── index.html     ← розмітка
│   └── style.css      ← стилі
├── server/
│   ├── main.py        ← точка входу сервера
│   ├── audio_ws.py    ← WebSocket, VAD, STT логіка
│   ├── app/
│   │   ├── ai_analysis.py  ← Gemini AI аналіз
│   │   └── session.py      ← стан дзвінка в пам'яті
│   └── public/        ← копія webapp/ (сервер роздає звідси)
└── scriptius-native/  ← Desktop Agent (Swift, macOS)
```

> **Важливо**: сервер роздає файли з `server/public/`. Після зміни файлів у `webapp/` — скопіюй їх у `server/public/`:
> ```bash
> cp webapp/* server/public/
> ```

---

## Troubleshooting

| Проблема | Рішення |
|----------|---------|
| Немає голосу клієнта (тільки мікрофон) | Перевір Screen Recording permission (Крок 4), перезапусти агент |
| "Agent disconnected" | Агент не запущений — виконай Крок 8 |
| `xcrun: error` при збірці | Повтори `xcode-select --install` |
| macOS 13 або старіший | Потрібно оновити до macOS 14.2+ (Sonoma) |
| Браузер не запитав мікрофон | Відкрий в Chrome/Safari, дозволь мікрофон у налаштуваннях браузера |
| `ModuleNotFoundError` при запуску сервера | Активуй venv: `source venv/bin/activate` і повтори `pip install -r server/requirements.txt` |
| `No module named 'dotenv'` | `pip install python-dotenv` (або перевір що venv активований) |
| Сервер запустився, але сторінка порожня | Перевір що `server/public/index.html` існує: `ls server/public/` |
| `Address already in use` (порт 8000) | Інший процес займає порт. Зміни порт: `uvicorn main:app --port 8001` |
| Транскрипція не з'являється | Перевір `server/.env` — чи заповнені `GOOGLE_PROJECT_ID`, `GOOGLE_CREDENTIALS_JSON` |
| `google.auth.exceptions.DefaultCredentialsError` | `GOOGLE_CREDENTIALS_JSON` у `.env` має містити повний JSON сервіс-акаунта, не шлях до файлу |
| `fatal: destination path 'scriptius' already exists` | `rm -rf scriptius` і повтори `git clone` |
