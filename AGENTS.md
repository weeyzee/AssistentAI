# AGENTS.md — брифинг для ИИ-агента

Проект: **«Марк»** — виртуальный Stack-chan (робот-ассистент без железа). Эмулятор на ПК притворяется прошивкой, телефон в Safari играет роль тела (микрофон, динамик, камера, лицо). Мозг — Claude (удалённо, через Claude Code CLI), речь — локальное распознавание GigaAM + облачный синтез edge-tts. Полное описание — в `README.md`.

## Как поднять/остановить

```powershell
cd stackchan-mcp
uv sync                       # один раз (или после обновления зависимостей)
uv tool install edge-tts      # один раз на машину (синтез речи)
.\start_virtual.ps1           # запустить всё в фоне (ASR → эмулятор → ассистент)
.\start_virtual.ps1 -Status   # проверить: 4 строки «работает»
.\start_virtual.ps1 -StopAll  # остановить всё
```

Адрес страницы печатает сам скрипт: `https://<tailscale-ip>:8443/` (сертификат самоподписанный, в Safari принять один раз).

## Что где

| Файл | Роль |
|------|------|
| `stackchan-mcp/virtual_stackchan.py` | Эмулятор прошивки: device API :8090 (localhost) + https-страница :8443 (LAN/Tailscale) |
| `stackchan-mcp/virtual_ui.html` | Страница телефона: запись, камера, лицо, кнопки; маппинг событий MCP → состояния лица |
| `stackchan-mcp/face_engine.js` | Программное лицо 320×172 (эталон для прошивки ESP32). Отладка: `window.__face.setState('SPEAKING')` |
| `stackchan-mcp/phone_assistant.py` | Голосовой цикл: `/audio/status` → GigaAM → тёплый Claude → edge-tts → `/play` |
| `stackchan-mcp/asr_service.py` | HTTP-обёртка GigaAM (sherpa-onnx). **Рабочая копия — в проекте `asr-tests`** (см. ниже), в репо лежит та же версия для справки |
| `stackchan-mcp/nettop_status.py` | Read-only диагностика домашнего сервера по ssh (фиксированный набор команд) |
| `stackchan-mcp/start_virtual.ps1` | Запуск/остановка/статус процессов |
| `stackchan-mcp/.env` / `.env.virtual.example` | Локальный конфиг (в гит не коммитится) |
| `.mcp.json` | MCP-сервер `stackchan` для Claude Code; `.codex/config.toml` — для Codex |

Порты: **8090** device API (только 127.0.0.1), **8443** страница телефона, **8765** ASR-сервис, **5099** отдача WAV.

ASR живёт отдельно: `C:\Users\Admin\Desktop\WhisperWork\asr-tests` — там модели (`models/gigaam-v2-ctc` и др.), `.venv` с sherpa-onnx и сам `asr_service.py`, который запускает `start_virtual.ps1`. Модели скачиваются их `fetch_models.py`.

## Правила (важно)

- **Никакого произвольного shell у LLM.** Ассистенту разрешены только точные команды (`--allowedTools` без wildcard: read-only обёртка неттопа + `mcp__stackchan__stackchan_see`), `--permission-mode dontAsk`. Не расширять без явной просьбы.
- **`.env` не коммитить** (там могут появиться ключи). Шаблон — `.env.virtual.example`.
- **`.ps1` писать в UTF-8 с BOM** (PowerShell 5.1 иначе ломает кириллицу).
- Device API эмулятора держать на `127.0.0.1`; `/play` принимает только локальный аудио-сервер (защита от SSRF); редиректы запрещены.
- Пароли/токены в логи не писать.

## Грабли (проверено на практике)

- **edge-tts требует интернет.** При сбое DNS создаёт пустой файл — Марк молчит. `~/.local/bin/edge-tts.exe` (шим uv) зависает из-под MCP — используем exe из `%APPDATA%\uv\tools\edge-tts\Scripts\` + `timeout`/`stdin=DEVNULL`.
- Kludge-история: `transcribe_audio` в `mcp_server/audio_processing.py` переключён на локальный ASR (`STACKCHAN_LOCAL_ASR_URL`); `EDGE_VOICES["ru"]`; язык по умолчанию из `STACKCHAN_VOICE_LANG`; в `listening.py` локальный ASR работает и без Fish-ключа.
- Тёплый процесс Claude: `--effort low` даёт ~3 с на повторный ответ; ротация сессии по `STACKCHAN_ASSISTANT_MAX_TURNS` (80) / `_MAX_AGE` (3 ч) / `_SESSION_TTL` (2 ч простоя) — иначе контекст растёт и ответы замедляются.
- Сон ПК останавливает всё; блокировка экрана — нет. Экран телефона должен быть активен.

## Быстрые проверки

```powershell
.\start_virtual.ps1 -Status                                    # порты + ассистент
curl.exe -s http://127.0.0.1:8090/audio/status                 # готовность записи
curl.exe -sk https://127.0.0.1:8443/web/poll?since=0           # long-poll событий (ждёт до 20 с)
Get-Content "$env:TEMP\virtual_stackchan\logs\assistant.out.log" -Tail 10   # что услышал/ответил
```

Проверка сквозного цикла без телефона: сгенерировать вопрос `edge-tts --voice ru-RU-SvetlanaNeural --text "..." --write-media q.mp3`, отправить `curl -sk -X POST --data-binary @q.mp3 https://127.0.0.1:8443/web/mic`, подождать ~20 с, посмотреть лог ассистента.

## Сделано / дальше

Сделано: MCP-мост + эмулятор, русский ASR (GigaAM) и TTS (edge-tts), тёплый Claude, камера по голосу (`stackchan_see`), read-only неттоп, программное лицо (8 состояний), надёжность цикла (очереди, epoch, ротация).

Дальше (обсуждалось): голосовые заметки в Obsidian (MVP: «запиши мысль»/«найди в заметках»/«добавь задачу»/«запомни, где лежит»/«отмени последнее»), реакция на поглаживание, прошивка на ESP32-S3-LCD-1.47B-M (у платы нет микрофона/усилителя — внешние модули; wake-word «Марк» готового русского нет), таймеры/напоминания.
