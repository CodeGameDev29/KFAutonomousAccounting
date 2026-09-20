<#
.SYNOPSIS
    Make Autonomous Accounting come back by itself after a reboot.

.DESCRIPTION
    The host is the whole deployment: if it restarts and nothing brings the
    stack back up, the app stays down until somebody notices. This registers
    the launcher as a Scheduled Task that fires at logon.

    Deliberately a per-user logon task, not a Windows service:
      * it needs no administrator rights to register;
      * the PostgreSQL data directory, and any credential the optional tunnel
        command needs, live under the user's profile, so a task running as
        SYSTEM would not find them.

    The trade-off is that the stack comes back when that user logs in, not at
    the boot prompt. On a host that logs in automatically those are the same
    moment; on one that does not, the app is down until someone signs in.

    Idempotent: re-running replaces the existing task.
#>

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Launcher    = Join-Path $PSScriptRoot 'start-aa.ps1'
$TaskName    = 'AutonomousAccounting-Local'

if (-not (Test-Path $Launcher)) { throw "launcher not found at $Launcher" }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`"" `
    -WorkingDirectory $ProjectRoot

# Logon trigger, with a delay: at logon the network stack is often not ready,
# and a tunnel command that cannot resolve its endpoint on the first attempt
# tends to exit rather than retry.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT45S'

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)   # 0 = never kill it

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Starts local PostgreSQL, the Autonomous Accounting API, and (when configured) the optional tunnel command that publishes it.' `
    | Out-Null

Write-Host "[aa] registered scheduled task '$TaskName' (at logon, +45s delay)"

# --- Daily backup ----------------------------------------------------
# There is no managed point-in-time recovery here: one copy, on one disk.
# Register the backup now rather than leaving it as a thing to remember.
$BackupScript = Join-Path $PSScriptRoot 'backup-aa.ps1'
$BackupTask   = 'AutonomousAccounting-Backup'

if (Test-Path $BackupScript) {
    $bAction = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$BackupScript`"" `
        -WorkingDirectory $ProjectRoot

    # 03:00 daily. StartWhenAvailable catches the machine having been asleep.
    $bTrigger  = New-ScheduledTaskTrigger -Daily -At 3am
    $bSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)

    Unregister-ScheduledTask -TaskName $BackupTask -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask `
        -TaskName $BackupTask `
        -Action $bAction `
        -Trigger $bTrigger `
        -Settings $bSettings `
        -Description 'Nightly pg_dump + storage archive of Autonomous Accounting to the backups folder under AA_HOME.' `
        | Out-Null

    Write-Host "[aa] registered scheduled task '$BackupTask' (daily 03:00)"
    Write-Host "[aa] NOTE: backups default to a folder under AA_HOME - the same disk as the data."
    Write-Host "[aa]       That survives a mistake but not a disk failure. Point the task at an"
    Write-Host "[aa]       external or synced drive with -Destination to actually close that risk."
}

Write-Host ""
Write-Host "[aa] verify with:  Get-ScheduledTask -TaskName $TaskName,$BackupTask"
Write-Host "[aa] run it now:   Start-ScheduledTask -TaskName $TaskName"
Write-Host "[aa] remove with:  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
