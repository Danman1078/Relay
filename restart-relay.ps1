# restart-relay.ps1
# Force-restarts the TrueNAS-Widget-Relay scheduled task, killing any
# lingering pythonw.exe first. Useful when a plain schtasks /End didn't
# actually clear the old process (e.g. config.json edits not taking effect).
#
# Run from a normal PowerShell window: .\restart-relay.ps1

$taskName = "TrueNAS-Widget-Relay"

Write-Output "Stopping task '$taskName'..."
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

Write-Output "Killing any lingering pythonw.exe..."
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force

Start-Sleep -Seconds 1

Write-Output "Starting task '$taskName'..."
Start-ScheduledTask -TaskName $taskName

Start-Sleep -Seconds 1

Write-Output ""
Write-Output "Done. Verify with:"
Write-Output "  curl http://127.0.0.1:8787/qbit-stats"
Write-Output ""
Write-Output "Current pythonw.exe process(es):"
Get-Process pythonw -ErrorAction SilentlyContinue | Format-Table Id, StartTime -AutoSize
