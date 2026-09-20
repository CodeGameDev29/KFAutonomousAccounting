<#
.SYNOPSIS
    Start Autonomous Accounting on this host.

.DESCRIPTION
    Starts PostgreSQL, applies pending migrations, starts the API, and - only if
    one is configured - runs an optional tunnel command that publishes the app
    beyond this host.

    Every path it uses comes from an environment variable with a local default:
    AA_HOME, AA_PG_BIN, AA_PG_DATA, AA_LOG_DIR, AA_PYTHON, PORT. Publishing is
    off unless AA_TUNNEL_CMD and PUBLIC_HOSTNAME are both set; what that command
    is, and how it authenticates, is entirely up to whoever sets it.

    It is idempotent - every step checks whether the thing is already running
    before starting it - so it is safe to run repeatedly, and safe to wire to a
    logon trigger (see register-startup-task.ps1).

    Order matters: migrations need PostgreSQL, the API needs the schema, and the
    tunnel should only start once the API answers, otherwise the first requests
    it forwards get a 502.

.PARAMETER SkipTunnel
    Start only the local stack, without running the tunnel command.
#>
param(
    [switch]$SkipTunnel
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
# Where the stack's own files live: the PostgreSQL binaries, its data directory,
# the log files and the backups. Override with the AA_HOME environment variable;
# the default keeps everything in one folder under the local app data directory.
$Base = if ($env:AA_HOME) { $env:AA_HOME } else { Join-Path $env:LOCALAPPDATA 'autonomous-accounting' }
$PgBin       = if ($env:AA_PG_BIN)  { $env:AA_PG_BIN }  else { "$Base\pgsql\bin" }
$PgData      = if ($env:AA_PG_DATA) { $env:AA_PG_DATA } else { "$Base\pgdata" }
$Logs        = if ($env:AA_LOG_DIR) { $env:AA_LOG_DIR } else { "$Base\logs" }
$Python      = if ($env:AA_PYTHON)  { $env:AA_PYTHON }  else { "$ProjectRoot\.venv\Scripts\python.exe" }
# Publishing the app is optional and generic: AA_TUNNEL_CMD is an executable
# that forwards PUBLIC_HOSTNAME to this port, AA_TUNNEL_ARGS is its argument
# string. Leave AA_TUNNEL_CMD unset and the stack stays on localhost.
$TunnelCmd   = $env:AA_TUNNEL_CMD
$TunnelArgs  = $env:AA_TUNNEL_ARGS
$PublicHost  = $env:PUBLIC_HOSTNAME
$Port        = if ($env:PORT) { [int]$env:PORT } else { 8080 }

New-Item -ItemType Directory -Force -Path $Logs | Out-Null

function Info($m) { Write-Host "[aa] $m" }

# --- 1. PostgreSQL ---------------------------------------------------
# pg_ctl status exits 3 when the server is not running; that is not an error.
$pgRunning = $false
try {
    & "$PgBin\pg_ctl.exe" -D $PgData status *> $null
    $pgRunning = ($LASTEXITCODE -eq 0)
} catch { $pgRunning = $false }

if ($pgRunning) {
    Info "PostgreSQL already running"
} else {
    Info "starting PostgreSQL..."
    # Two things here are deliberate:
    #
    #  * No -Wait. On Windows pg_ctl does not return once the postmaster is up
    #    the way it does on Unix, so waiting on it blocks this script forever.
    #    The pg_isready poll below is what establishes readiness.
    #  * Both streams redirected to files. Without that, pg_ctl inherits this
    #    console's stdout, the postmaster inherits it in turn, and the handle
    #    stays open for the life of the database - so this script's caller
    #    never sees the pipe close and appears to hang even after a clean start.
    Start-Process -FilePath "$PgBin\pg_ctl.exe" `
        -ArgumentList @('-D', $PgData, '-l', "$Logs\postgres.log", 'start') `
        -RedirectStandardOutput "$Logs\pg_ctl.out" `
        -RedirectStandardError  "$Logs\pg_ctl.err" `
        -WindowStyle Hidden | Out-Null
}

# Wait for the socket to actually accept connections before migrating.
$ready = $false
foreach ($i in 1..30) {
    & "$PgBin\pg_isready.exe" -h 127.0.0.1 -p 5432 *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw "PostgreSQL did not become ready - see $Logs\postgres.log" }
Info "PostgreSQL ready on 127.0.0.1:5432"

# --- 2. Schema -------------------------------------------------------
Info "applying pending migrations..."
$env:PYTHONPATH = $ProjectRoot
# Same UTF-8 reason as the API block below; db.migrate logs to migrate.log.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$migLog = "$Logs\migrate.log"
# Redirect to files rather than piping. Python logs to stderr, and in Windows
# PowerShell `native.exe 2>&1 | ...` wraps every stderr line in an ErrorRecord,
# which under $ErrorActionPreference='Stop' aborts this script on a perfectly
# successful migration run.
$mig = Start-Process -FilePath $Python `
    -ArgumentList @('-m', 'db.migrate') `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput "$migLog.out" `
    -RedirectStandardError  $migLog `
    -WindowStyle Hidden -PassThru -Wait

Get-Content $migLog -ErrorAction SilentlyContinue |
    Where-Object { $_ -match 'applied|up to date|bootstrap|ERROR' } |
    ForEach-Object { Info "  $_" }

if ($mig.ExitCode -ne 0) {
    throw "migrations failed (exit $($mig.ExitCode)) - refusing to start the API against a stale schema. See $migLog"
}

# --- 3. API ----------------------------------------------------------
$apiUp = $false
try {
    Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 5 | Out-Null
    $apiUp = $true
} catch { $apiUp = $false }

if ($apiUp) {
    Info "API already answering on :$Port"
} else {
    Info "starting API..."
    $env:PYTHONPATH = $ProjectRoot
    # Both of this child's streams are redirected into files below. On Windows a
    # redirected Python stream defaults to the ANSI codepage with
    # errors=backslashreplace, which mangles every non-ASCII character in a file
    # whose other lines are UTF-8. Force UTF-8 for the API process.
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    # --proxy-headers matters more than it looks. Behind a tunnel or a reverse
    # proxy every request arrives from 127.0.0.1, so without it the per-IP rate
    # limiter sees one client for every caller: the hourly cap on password reset
    # becomes a single shared bucket that any visitor can exhaust, locking out
    # the account owner. --forwarded-allow-ips is pinned to loopback because
    # loopback is the only thing that can reach this port, so the forwarded
    # header cannot be spoofed by an outside caller.
    Start-Process -FilePath $Python `
        -ArgumentList @('-m', 'uvicorn', 'server.app:app',
                        '--host', '127.0.0.1', '--port', "$Port",
                        '--proxy-headers', '--forwarded-allow-ips', '127.0.0.1',
                        '--timeout-keep-alive', '75',
                        '--timeout-graceful-shutdown', '30') `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput "$Logs\aa-server.log" `
        -RedirectStandardError  "$Logs\aa-server.err" `
        -WindowStyle Hidden | Out-Null

    $ok = $false
    foreach ($i in 1..40) {
        Start-Sleep -Seconds 2
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { }
    }
    if (-not $ok) { throw "API did not come up - see $Logs\aa-server.err" }
}
Info "API healthy on http://127.0.0.1:$Port"

# --- 4. Optional tunnel command --------------------------------------
# Whatever AA_TUNNEL_CMD points at is expected to forward PUBLIC_HOSTNAME to
# this host's port and to keep running while it does. This script only starts
# it, waits, and checks that https://PUBLIC_HOSTNAME/health answers.
if ($SkipTunnel) {
    Info "skipping the tunnel command (-SkipTunnel); the app is on http://127.0.0.1:$Port"
} elseif (-not $TunnelCmd -or -not $PublicHost) {
    Info "no tunnel configured (set AA_TUNNEL_CMD and PUBLIC_HOSTNAME to publish); the app is on http://127.0.0.1:$Port"
} else {
    $tunnelProc = [IO.Path]::GetFileNameWithoutExtension($TunnelCmd)
    if (Get-Process -Name $tunnelProc -ErrorAction SilentlyContinue) {
        Info "tunnel already running"
    } else {
        Info "starting the tunnel command..."
        $startArgs = @{
            FilePath               = $TunnelCmd
            RedirectStandardOutput = "$Logs\tunnel.log"
            RedirectStandardError  = "$Logs\tunnel.err"
            WindowStyle            = 'Hidden'
        }
        if ($TunnelArgs) { $startArgs['ArgumentList'] = $TunnelArgs }
        Start-Process @startArgs | Out-Null
        Start-Sleep -Seconds 12
    }

    $pub = "https://$PublicHost/health"
    $live = $false
    foreach ($i in 1..10) {
        try {
            $r = Invoke-WebRequest $pub -UseBasicParsing -TimeoutSec 20
            if ($r.StatusCode -eq 200) { $live = $true; break }
        } catch { Start-Sleep -Seconds 5 }
    }
    if ($live) {
        Info "published at https://$PublicHost"
    } else {
        Write-Warning "[aa] the tunnel command started but the public URL did not answer - see $Logs\tunnel.err"
    }
}

Info "done. Logs: $Logs"
