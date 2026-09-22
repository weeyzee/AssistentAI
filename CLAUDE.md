# CLAUDE.md

Проект «Марк» — виртуальный Stack-chan: эмулятор прошивки + телефон как тело, русская речь (локальный GigaAM + edge-tts), мозг — Claude через MCP-сервер `stackchan`.

**Перед работой прочитай `AGENTS.md`** — там карта файлов, порты, команды запуска/остановки, логи, правила безопасности и грабли. Кратко:

```powershell
cd stackchan-mcp
.\start_virtual.ps1           # поднять всё (ASR → эмулятор → ассистент)
.\start_virtual.ps1 -Status   # проверить
.\start_virtual.ps1 -StopAll  # остановить
```

Логи: `%TEMP%\virtual_stackchan\logs\`. Телефон: `https://<tailscale-ip>:8443/`.
