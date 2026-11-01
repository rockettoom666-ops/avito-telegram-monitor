@echo off
setlocal

rem The BAT files are inside the "start" folder, one level below the project.
for %%I in ("%~dp0..") do set "AVITO_BOT_ROOT=%%~fI"
cd /d "%AVITO_BOT_ROOT%"

if not exist ".env" (
    echo ERROR: .env was not found.
    echo Copy .env.example to .env and add TELEGRAM_BOT_TOKEN.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Python environment was not found.
    echo Run: powershell -ExecutionPolicy Bypass -File scripts\install.ps1
    pause
    exit /b 1
)

rem Prefer the scheduled task: it prevents starting a duplicate bot process.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$task = Get-ScheduledTask -TaskName 'Avito Telegram Monitor' -ErrorAction SilentlyContinue; if (-not $task) { exit 10 }; if ($task.State -eq 'Running') { Write-Host 'Avito bot is already running.' } else { Start-ScheduledTask -TaskName 'Avito Telegram Monitor'; Write-Host 'Avito bot started through Windows Task Scheduler.' }; exit 0"

if %errorlevel% equ 0 (
    exit /b 0
)

rem If Task Scheduler is not configured, keep the bot in this console window.
echo Starting Avito bot in this window...
".venv\Scripts\python.exe" -m avito_bot

if errorlevel 1 (
    echo.
    echo Bot stopped with an error. Check logs\bot.log.
    pause
    exit /b 1
)

endlocal
exit /b 0
