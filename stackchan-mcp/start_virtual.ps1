# Виртуальный Stack-chan "Марк": ASR-сервис + эмулятор + голосовой ассистент.
#   .\start_virtual.ps1            — запустить всё в фоне (окно можно закрыть)
#   .\start_virtual.ps1 -Status    — проверить, что слушает какие порты
#   .\start_virtual.ps1 -StopAll   — остановить всё
#   .\start_virtual.ps1 -Foreground — запустить эмулятор в текущем окне (для отладки)
param(
    [switch]$StopAll,
    [switch]$Status,
    [switch]$Foreground,
    [switch]$NoAsr
)

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$asrDir = "C:\Users\Admin\Desktop\WhisperWork\asr-tests"
$asrPython = Join-Path $asrDir ".venv\Scripts\python.exe"
$venvPython = Join-Path $repo ".venv\Scripts\python.exe"
$logDir = Join-Path $env:TEMP "virtual_stackchan\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-Port([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Stop-PortProcess([int]$Port, [string]$Name) {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($conn in $conns) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "Останавливаю $Name (PID $($proc.Id))"
            Stop-Process -Id $proc.Id -Force
        }
    }
}

function Start-Hidden([string]$File, [string[]]$Arguments, [string]$WorkDir, [string]$Name) {
    $out = Join-Path $logDir "$Name.out.log"
    $err = Join-Path $logDir "$Name.err.log"
    Start-Process -FilePath $File -ArgumentList $Arguments -WorkingDirectory $WorkDir `
        -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
    Write-Host "Запущен $Name (лог: $out)"
}

if ($StopAll) {
    Stop-PortProcess 8443 "эмулятор (веб)"
    Stop-PortProcess 8090 "эмулятор (device API)"
    Stop-PortProcess 8765 "ASR-сервис"
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -like "*phone_assistant.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "Остановлен ассистент (PID $($_.ProcessId))" }
    exit 0
}

if ($Status) {
    foreach ($pair in @(@(8765, "ASR-сервис (GigaAM)"), @(8090, "эмулятор: device API"), @(8443, "эмулятор: страница для телефона"))) {
        $state = if (Test-Port $pair[0]) { "работает" } else { "НЕ работает" }
        Write-Host ("{0,-34} порт {1}: {2}" -f $pair[1], $pair[0], $state)
    }
    $assistant = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*phone_assistant.py*" }
    $astate = if ($assistant) { "работает (PID $($assistant[0].ProcessId))" } else { "НЕ работает" }
    Write-Host ("{0,-34} {1}" -f "голосовой ассистент", $astate)
    exit 0
}

if (-not (Test-Port 8765) -and -not $NoAsr) {
    Write-Host "Запускаю ASR-сервис (GigaAM v2-ctc)…"
    Start-Hidden $asrPython @("-u", "asr_service.py") $asrDir "asr"
    Start-Sleep -Seconds 8
} elseif (Test-Port 8765) {
    Write-Host "ASR-сервис уже слушает :8765"
}

if (-not (Test-Port 8090)) {
    Start-Hidden $venvPython @("-u", "virtual_stackchan.py") $repo "emulator"
    Start-Sleep -Seconds 3
} else {
    Write-Host "Эмулятор уже слушает :8090"
}

$assistant = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*phone_assistant.py*" }
if (-not $assistant) {
    Start-Hidden $venvPython @("-u", "phone_assistant.py") $repo "assistant"
    Start-Sleep -Seconds 2
} else {
    Write-Host "Ассистент уже работает (PID $($assistant[0].ProcessId))"
}

$tailscale = Get-Command tailscale -ErrorAction SilentlyContinue
$tsIp = ""
if ($tailscale) {
    $tsIp = (& tailscale ip -4 2>$null | Select-Object -First 1)
}
Write-Host ""
Write-Host "Готово. Страница для телефона: https://$(if ($tsIp) { $tsIp } else { '<tailscale-ip>' }):8443/"
Write-Host "Логи: $logDir"
Write-Host "Проверка: .\start_virtual.ps1 -Status    Остановка: .\start_virtual.ps1 -StopAll"

if ($Foreground) {
    Set-Location $repo
    & $venvPython "virtual_stackchan.py"
}

