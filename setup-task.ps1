# Registers (or re-registers) the "TrueNAS-Widget-Relay" scheduled task that
# keeps relay.py running via supervisor.ps1 -- at login, hidden, auto-restart
# on crash, survives reboots. Safe to re-run any time (e.g. after moving this
# folder, or after a fresh Windows install/restore).
#
# Prerequisites this script does NOT install for you:
#   - Python 3.x on PATH (with pip install -r requirements.txt already run)
#   - config.json filled in with your TrueNAS host/username/api_key
#
# Run this from an elevated or normal PowerShell window (elevation not
# required -- AtLogOn tasks for the current user don't need admin rights).

$ErrorActionPreference = "Stop"

$supervisorPath = Join-Path $PSScriptRoot "supervisor.ps1"
if (-not (Test-Path $supervisorPath)) {
    throw "supervisor.ps1 not found next to this script at: $supervisorPath"
}

if (-not (Get-Command pythonw.exe -ErrorAction SilentlyContinue)) {
    throw "pythonw.exe not found on PATH. Install Python (checking 'Add python.exe to PATH' during install), then re-run this script."
}

$taskName = "TrueNAS-Widget-Relay"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -NoProfile -File `"$supervisorPath`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Runs a supervisor loop that keeps relay.py alive for the iCUE TrueNAS widget, restarting it if it ever exits." `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName

Write-Output "Scheduled task '$taskName' registered and started."
Write-Output "Supervisor: $supervisorPath"
Write-Output "It will now also start automatically at every login."
Write-Output ""
Write-Output "Verify with: Invoke-WebRequest http://127.0.0.1:8787/stats -UseBasicParsing"
