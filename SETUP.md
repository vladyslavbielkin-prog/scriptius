# Scriptius — Інструкція з встановлення

Scriptius транскрибує твої дзвінки в реальному часі: окремо голос клієнта та твій голос, з AI-аналізом розмови.

---

## Вимоги

- **macOS 14.2 або новіше** (Sonoma) — обов'язково
- Інтернет-з'єднання

---

## Крок 1 — Встановити Xcode Command Line Tools

Відкрий Terminal і виконай:

```bash
xcode-select --install
```

З'явиться діалог — натисни **Install**. Дочекайся завершення (~5 хв).

---

## Крок 2 — Клонувати репозиторій

```bash
git clone https://github.com/vladyslavbielkin-prog/scriptius.git
cd scriptius
```

---

## Крок 3 — Зібрати Desktop Agent

```bash
cd scriptius-native
swift build -c release
```

Перша збірка займає ~1-2 хвилини. Ти побачиш `Build complete!` в кінці.

---

## Крок 4 — Дозволити запис екрану

> ⚠️ Цей крок обов'язковий — без нього системний аудіо (голос клієнта) не буде захоплюватись.

1. Відкрий **System Settings** → **Privacy & Security** → **Screen Recording**
2. Знайди **Terminal** у списку та увімкни перемикач
3. Якщо Terminal там немає — натисни `+` і додай його вручну (`/Applications/Utilities/Terminal.app`)

---

## Крок 5 — Запустити Desktop Agent

Кожного разу перед початком дзвінка виконай в Terminal:

```bash
# (якщо ти вже всередині папки scriptius/scriptius-native)
.build/release/ScriptiusAudio --server
```

Або з будь-якого місця:

```bash
/path/to/scriptius/scriptius-native/.build/release/ScriptiusAudio --server
```

Ти побачиш: `[Server] Listening on ws://localhost:9001` — агент готовий.

Залиш Terminal відкритим під час дзвінка. Зупинити: **Ctrl+C**.

---

## Крок 6 — Відкрити Scriptius

Відкрий у браузері: **https://scriptius.fly.dev**

- Натисни **Start** — браузер запитає доступ до мікрофона, дозволь
- Транскрипція почнеться автоматично

---

## Troubleshooting

| Проблема | Рішення |
|----------|---------|
| Немає голосу клієнта (тільки твій мікрофон) | Перевір Screen Recording permission (Крок 4), перезапусти агент |
| "Agent disconnected" або немає підключення агента | Агент не запущений — виконай Крок 5 |
| `xcrun: error` при збірці | Повтори `xcode-select --install` |
| macOS 13 або старіший | Потрібно оновити систему до macOS 14 (Sonoma) |
| Браузер не запитав мікрофон | Відкрий Safari/Chrome вручну, дозволь мікрофон у налаштуваннях браузера |
