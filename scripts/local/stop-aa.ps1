<#
.SYNOPSIS
    Stop Autonomous Accounting on this host.

.DESCRIPTION
    Reverse of start-aa.ps1, in the order that keeps the published site honest:
    stop the optional tunnel command first so requests stop being routed to an
    origin that is about to disappear, then the API, then PostgreSQL with a
    clean shutdown.

.PARAMETER KeepDatabase
    Leave PostgreSQL running. Useful when restarting only the app.
#>
param(
    [switch]$KeepDatabase
)

$ErrorActionPreference = 'Continue'

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
# Where the stack's own files live: the PostgreSQL binaries, its data directory,
# the log files and the backups. Override with the AA_HOME environment variable;
# the default keeps everything in one folder under the local app data directory.
$Base = if ($env:AA_HOME) { $env:AA_HOME } else { Join-Path $env:LOCALAPPDATA 'autonomous-accounting' }
$PgBin  = if ($env:AA_PG_BIN)  { $env:AA_PG_BIN }  else { "$Base\pgsql\bin" }
$PgData = if ($env:AA_PG_DATA) { $env:AA_PG_DATA } else { "$Base\pgdata" }

function Info($m) { Write-Host "[aa] $m" }

# Only the process AA_TUNNEL_CMD names is stopped; with no tunnel configured
# there is nothing to stop.
if ($env:AA_TUNNEL_CMD) {
    $tunnelProc = [IO.Path]::GetFileNameWithoutExtension($env:AA_TUNNEL_CMD)
    Info "stopping the tunnel command ($tunnelProc)..."
    Get-Process -Name $tunnelProc -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

Info "stopping API..."
# Match only the interpreter from this checkout's virtualenv, so an unrelated
# Python process on this host is never killed.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith("$ProjectRoot\.venv") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if ($KeepDatabase) {
    Info "leaving PostgreSQL running (-KeepDatabase)"
} else {
    Info "stopping PostgreSQL..."
    # -m fast rolls back open transactions and checkpoints before exiting;
    # never use -m immediate here, which skips the checkpoint and forces
    # recovery on next start.
    Start-Process -FilePath "$PgBin\pg_ctl.exe" `
        -ArgumentList @('-D', $PgData, '-m', 'fast', 'stop') `
        -WindowStyle Hidden -Wait
}

Info "stopped."
