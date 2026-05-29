param(
    [string]$TaskName = "Crypto Magic Nine Telegram Scan",
    [string[]]$At = @("09:00", "21:00")
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunScript = Join-Path $ProjectRoot "run_scanner.ps1"

if (-not (Test-Path $RunScript)) {
    throw "Cannot find run_scanner.ps1 at $RunScript"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`"" `
    -WorkingDirectory $ProjectRoot

$Triggers = @()
foreach ($Time in $At) {
    $Triggers += New-ScheduledTaskTrigger -Daily -At $Time
}
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Triggers `
    -Settings $Settings `
    -Description "Scan crypto market-cap top 200 for Magic Nine signals and push to Telegram." `
    -Force

Write-Host "Scheduled task registered: $TaskName"
Write-Host "Daily triggers: $($At -join ', ')"
