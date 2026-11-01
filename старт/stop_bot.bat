@echo off
setlocal

rem The BAT is only a convenient button. The safe process selection is in PS1.
for %%I in ("%~dp0..") do set "AVITO_BOT_ROOT=%%~fI"
cd /d "%AVITO_BOT_ROOT%"

echo Stopping Avito bot...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\stop_bot.ps1"

if errorlevel 1 (
    echo ERROR: The bot could not be stopped completely.
    pause
    exit /b 1
)

endlocal
exit /b 0
