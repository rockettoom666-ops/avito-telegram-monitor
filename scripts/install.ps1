$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    # Выбираем Python независимо от имени пользователя и каталога установки.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv (Join-Path $projectRoot ".venv")
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv (Join-Path $projectRoot ".venv")
    } else {
        throw "Установите Python 3.12 с python.org и включите Add Python to PATH."
    }
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать окружение Python 3.12." }
}

& $venvPython -c "import sys; assert (3, 12) <= sys.version_info[:2] < (3, 14), 'Required Python 3.12 or 3.13'"
if ($LASTEXITCODE -ne 0) { throw "Нужен Python 3.12 или 3.13." }
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Не удалось обновить pip." }
& $venvPython -m pip install -e "$projectRoot[dev]"
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости." }
& $venvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить Chromium." }

Write-Host ""
Write-Host "Установка завершена."
Write-Host "Скопируйте .env.example в .env и укажите TELEGRAM_BOT_TOKEN."
Write-Host "Запуск: .venv\Scripts\python.exe -m avito_bot"
