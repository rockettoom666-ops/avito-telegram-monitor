$ErrorActionPreference = "Stop"

# Останавливаем задачу первой, иначе Планировщик может снова поднять процесс.
$taskName = "Avito Telegram Monitor"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$taskWasRunning = $null -ne $task -and $task.State -eq "Running"
if ($taskWasRunning) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 700
}

# Ищем только команду "-m avito_bot", запущенную из каталога этого проекта.
# Дочерние процессы — это настоящий Python и Chromium, открытый Playwright.
$projectRoot = [IO.Path]::GetFullPath(
    (Split-Path -Parent $PSScriptRoot)
).TrimEnd("\")
$allProcesses = @(Get-CimInstance Win32_Process)
$rootProcessIds = @(
    $allProcesses |
        Where-Object {
            $_.CommandLine `
                -and $_.CommandLine -match "(?i)(?:^|\s)-m\s+avito_bot(?:\s|$)" `
                -and $_.CommandLine.IndexOf(
                    $projectRoot,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0
        } |
        ForEach-Object { [int]$_.ProcessId }
)

$targetProcessIds = [Collections.Generic.HashSet[int]]::new()
foreach ($processId in $rootProcessIds) {
    [void]$targetProcessIds.Add($processId)
}

# Добавляем всё дерево потомков, но не затрагиваем другие процессы Python.
do {
    $added = $false
    foreach ($process in $allProcesses) {
        $parentId = [int]$process.ParentProcessId
        $processId = [int]$process.ProcessId
        if (
            $targetProcessIds.Contains($parentId) `
                -and $targetProcessIds.Add($processId)
        ) {
            $added = $true
        }
    }
} while ($added)

foreach ($processId in @($targetProcessIds)) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}

if ($taskWasRunning -or $targetProcessIds.Count -gt 0) {
    Write-Host "Avito bot stopped."
} else {
    Write-Host "Avito bot was already stopped."
}
