# Assistent — виртуальный Stack-chan «Марк»

Голосовой ассистент в телефоне, собранный на базе [stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp) (MIT) — без железа: роль робота играет эмулятор на ПК, роль тела — веб-страница в Safari на iPhone.

```
iPhone (Safari: микрофон/динамик/камера/морда)
        │  https + Tailscale
        ▼
virtual_stackchan.py  ── эмулятор HTTP API прошивки (порт 8090) + страница (8443)
        ▲
        │
phone_assistant.py ── запись → GigaAM (локальный ASR, :8765) → Claude (headless) → edge-tts → озвучка
        ▲
        │
Claude Code (MCP-сервер stackchan, .mcp.json) — тоже может говорить/смотреть через робота
```

## Что внутри

| Файл | Назначение |
|------|-----------|
| `stackchan-mcp/virtual_stackchan.py` | Эмулятор прошивки: device API + веб-страница для телефона |
| `stackchan-mcp/virtual_ui.html` | Страница телефона: запись, камера, лицо, кнопки тач-полосы |
| `stackchan-mcp/face_engine.js` | Программное лицо 320×172 для переноса в прошивку ESP32 |
| `stackchan-mcp/phone_assistant.py` | Фоновый голосовой цикл: запись → ASR → Claude → озвучка |
| `stackchan-mcp/asr_service.py` | Копия сервиса GigaAM — оригинал живёт в проекте `asr-tests` |
| `stackchan-mcp/start_virtual.ps1` | Запуск/остановка/статус всех фоновых процессов |
| `stackchan-mcp/.env` | Конфиг (локальный ASR, edge-tts, русский язык) |
| `.mcp.json` | Регистрация MCP-сервера stackchan для Claude Code |

Патчи поверх upstream: локальный ASR вместо Fish Audio (`STACKCHAN_LOCAL_ASR_URL`), русский голос edge-tts (`EDGE_VOICES["ru"]`), язык по умолчанию из `STACKCHAN_VOICE_LANG`, таймаут у edge-tts.

## Запуск

Требуется: Windows, Python 3.12, `uv`, `ffmpeg`, Tailscale, модели GigaAM в `C:\Users\Admin\Desktop\WhisperWork\asr-tests` (проект asr-tests).

```powershell
cd stackchan-mcp
uv sync
Copy-Item .env.virtual.example .env   # если .env ещё нет
.\start_virtual.ps1           # фоновые процессы
.\start_virtual.ps1 -Status   # проверить
.\start_virtual.ps1 -StopAll  # остановить
```

Страница для телефона: `https://<tailscale-ip>:8443/` (сертификат самоподписанный — принять один раз).

## Лицо (эмулятор будущей прошивки)

`face_engine.js` рисует лицо программно — без картинок, только примитивы, экран 320×172
(ESP32-S3-LCD-1.47B-M, ST7789, альбомная ориентация). Состояния: `IDLE`, `LISTENING`,
`THINKING`, `SPEAKING`, `HAPPY`, `SURPRISED`, `SLEEPING`, `ERROR`. Палитра: фон `#050608`,
основной `#FFB000`, яркий `#FFD25A`, подсветка `#FF7A00`.

Автоматика: моргание каждые 2–7 с, случайные взгляды в IDLE, рот по уровню звука в SPEAKING
(Web Audio Analyser; на ESP32 — амплитуда I2S от MAX98357), сон после 3 минут тишины,
кадр камеры показывается поверх лица 6 секунд (как в прошивке — JPEG на дисплее).

Перенос на ESP32: `r(x,y,w,h,c)` → `display.fillRect(...)`, `tick/render` → задача ~30 FPS
с отрисовкой кадра в Sprite и одним выводом на экран (двойной буфер в PSRAM 320×172×2 байта),
`setGaze()` ← события `move()` и гироскоп QMI8658, `setAudioLevel()` ← уровень аудио.
Карта событий MCP → состояния: запись → `LISTENING`, ожидание ответа → `THINKING`,
воспроизведение → `SPEAKING`, `nod`/`pet` → `HAPPY`, `shake` → `SURPRISED`, нет связи → `ERROR`.

## Ограничения

- Экран **телефона** должен быть включён: iOS глушит микрофон/звук веб-страниц при блокировке.
- Блокировка экрана **ПК** работе не мешает; сон ПК — мешает.
- Сервоприводы и датчики окружения — заглушки (реального железа нет).

## Лицензия

Код на базе stackchan-mcp (MIT, см. `stackchan-mcp/LICENSE`). Модели GigaAM — из проекта sherpa-onnx (см. лицензии в `asr-tests/models`).
