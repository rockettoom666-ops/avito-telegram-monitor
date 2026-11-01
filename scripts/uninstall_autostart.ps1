$ErrorActionPreference = "Stop"
$taskName = "Avito Telegram Monitor"

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Задача автозапуска удалена: $taskName"
} else {
    Write-Host "Задача автозапуска не найдена."
}
