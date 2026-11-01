$ErrorActionPreference = "Stop"

$taskName = "Avito Telegram Monitor"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$envFile = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "Виртуальное окружение не найдено. Сначала запустите scripts\install.ps1"
}
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Файл .env не найден. Скопируйте .env.example и добавьте токен."
}

$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "-m avito_bot" `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Telegram-бот для новых объявлений Avito" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Host "Автозапуск установлен, задача запущена: $taskName"
