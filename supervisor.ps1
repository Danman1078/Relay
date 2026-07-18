# Keeps relay.py running: if it exits for any reason (crash, killed, TrueNAS
# connection lost and unrecovered), relaunch it after a short pause. This is
# the actual restart guarantee -- Task Scheduler's own RestartCount/RestartInterval
# setting does not reliably fire for a long-running process that gets killed
# while in the "Running" state, only for tasks that fail immediately on start.
#
# Self-locating: uses its own folder for relay.py and finds pythonw.exe via
# PATH, so this works unchanged after a reinstall/move as long as
# setup-task.ps1 was re-run to (re)point the scheduled task at this file.

$scriptPath = Join-Path $PSScriptRoot "relay.py"
$pythonwCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $pythonwCmd) {
    throw "pythonw.exe not found on PATH. Install Python and ensure 'Add to PATH' was checked, then re-run setup-task.ps1."
}
$pythonExe = $pythonwCmd.Source

while ($true) {
    Start-Process -FilePath $pythonExe -ArgumentList "`"$scriptPath`"" -WindowStyle Hidden -Wait
    Start-Sleep -Seconds 3
}
