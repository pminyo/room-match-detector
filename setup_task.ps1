param(
    [string]$TaskName = "RoomMatchMonitor",
    [int]$IntervalMinutes = 5,
    [string]$PythonExe = "",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Tarea '$TaskName' eliminada."
    exit 0
}

if (-not $PythonExe) {
    $candidate = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $candidate) { $candidate = Get-Command python.exe -ErrorAction SilentlyContinue }
    if (-not $candidate) { throw "No encuentro Python. Pasa -PythonExe 'C:\ruta\pythonw.exe'." }
    $PythonExe = $candidate.Source
}

$action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument ('"' + (Join-Path $dir "monitor.py") + '"') -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Avisa de nuevos anuncios en RoomMatch" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Tarea '$TaskName' creada: cada $IntervalMinutes min con $PythonExe"
