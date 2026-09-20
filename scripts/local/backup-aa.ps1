<#
.SYNOPSIS
    Take a full, restorable backup of Autonomous Accounting.

.DESCRIPTION
    There is no managed point-in-time recovery here and no object-store
    redundancy: there is one copy of everything, on one disk. This script is the
    mitigation.

    A backup is a timestamped directory containing:
      * database.dump  - pg_dump custom format, restorable with pg_restore
      * storage.zip    - every receipt, statement and export under STORAGE_ROOT
      * env.txt        - a copy of .env, because a restored database is useless
                         without AUTH_JWT_SECRET (sessions), STORAGE_URL_SECRET
                         (download links), CREDENTIAL_ENCRYPTION_SECRET (stored
                         integration credentials) and the DATABASE_URL password
      * MANIFEST.txt   - what this is and exactly how to restore it

    env.txt is written in clear text, on purpose: a backup you cannot restore
    from is not a backup. It does mean the backup directory is as sensitive as
    .env itself: restrict it, and do not sync it anywhere you would not put
    .env.

    Old backups are pruned to -KeepDays.

.PARAMETER Destination
    Where to write. Defaults to $env:AA_HOME\backups (or a folder under LOCALAPPDATA).

    A backup on the same physical disk as the data survives a mistake but not a
    disk failure. Point this at an external drive or a synced folder and that
    risk goes away. The script warns when the destination shares a drive with
    the data.

.PARAMETER KeepDays
    Delete backups older than this many days. Default 30.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\local\backup-aa.ps1
    powershell -ExecutionPolicy Bypass -File .\scripts\local\backup-aa.ps1 -Destination D:\aa-backups
#>
param(
    [string]$Destination = '',
    [int]$KeepDays = 30
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
# Where the stack's own files live: the PostgreSQL binaries, its data directory,
# the log files and the backups. Override with the AA_HOME environment variable;
# the default keeps everything in one folder under the local app data directory.
$Base = if ($env:AA_HOME) { $env:AA_HOME } else { Join-Path $env:LOCALAPPDATA 'autonomous-accounting' }
if (-not $Destination) { $Destination = Join-Path $Base 'backups' }
$PgBin       = if ($env:AA_PG_BIN) { $env:AA_PG_BIN } else { "$Base\pgsql\bin" }
$EnvFile     = Join-Path $ProjectRoot '.env'

function Info($m) { Write-Host "[backup] $m" }

if (-not (Test-Path $EnvFile)) { throw ".env not found at $EnvFile" }

# Parse just the two values this script needs out of .env rather than importing
# the whole file.
$envMap = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') { $envMap[$matches[1]] = $matches[2].Trim() }
}
$dbUrl       = $envMap['DATABASE_URL']
$storageRoot = $envMap['STORAGE_ROOT']
if (-not $dbUrl)       { throw 'DATABASE_URL missing from .env' }
# Must match core/file_storage.py's _DEFAULT_ROOT (<repo>/data/storage), which is
# where the app writes when STORAGE_ROOT is unset. A fallback that points
# anywhere else produces a backup with no documents in it.
if (-not $storageRoot) { $storageRoot = Join-Path $ProjectRoot 'data\storage' }

# postgresql://user:pass@host:port/dbname
if ($dbUrl -notmatch '^postgres(ql)?://([^:]+):([^@]+)@([^:/]+):(\d+)/(.+)$') {
    throw "DATABASE_URL is not in the expected postgresql://user:pass@host:port/db form"
}
$pgUser = $matches[2]; $pgPass = $matches[3]
$pgHost = $matches[4]; $pgPort = $matches[5]; $pgName = $matches[6]

$stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$outDir = Join-Path $Destination $stamp
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

if ((Split-Path -Qualifier $Destination) -eq (Split-Path -Qualifier $storageRoot)) {
    Write-Warning "[backup] Destination is on the same drive as the data ($(Split-Path -Qualifier $Destination)). This survives mistakes but NOT a disk failure. Pass -Destination on an external or synced drive."
}

# --- Database ---
Info "dumping database '$pgName'..."
$env:PGPASSWORD = $pgPass
$dumpPath = Join-Path $outDir 'database.dump'
& "$PgBin\pg_dump.exe" -h $pgHost -p $pgPort -U $pgUser -d $pgName `
    --format=custom --file=$dumpPath 2>&1 | Out-Null
$dumpExit = $LASTEXITCODE
$env:PGPASSWORD = $null
if ($dumpExit -ne 0 -or -not (Test-Path $dumpPath)) {
    throw "pg_dump failed (exit $dumpExit) - backup aborted rather than left half-written"
}
Info "  database.dump $([math]::Round((Get-Item $dumpPath).Length/1MB,2)) MB"

# --- Files ---
if (Test-Path $storageRoot) {
    Info "archiving storage from $storageRoot ..."
    $zipPath = Join-Path $outDir 'storage.zip'
    # -Force so an empty store still produces an archive; the absence of a
    # storage.zip should mean "the backup failed", never "there was nothing".
    Compress-Archive -Path (Join-Path $storageRoot '*') -DestinationPath $zipPath -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $zipPath)) {
        # Compress-Archive refuses an empty source; record that honestly.
        Set-Content -Path (Join-Path $outDir 'storage-EMPTY.txt') -Value 'STORAGE_ROOT contained no files at backup time.' -Encoding utf8
        Info "  storage was empty"
    } else {
        Info "  storage.zip $([math]::Round((Get-Item $zipPath).Length/1MB,2)) MB"
    }
} else {
    Info "  STORAGE_ROOT $storageRoot does not exist yet - skipping"
}

# --- Secrets ---
# Without these the dump is undecryptable in the ways that matter: stored
# integration credentials (Wise, Gmail, PayPal) are Fernet-encrypted with a key
# derived from CREDENTIAL_ENCRYPTION_SECRET - which takes precedence over
# AUTH_JWT_SECRET for that and falls back to it when unset - and every signed
# download URL is HMAC'd with STORAGE_URL_SECRET. So .env is copied in, and the
# manifest says plainly that the whole folder is secret.
Copy-Item -Path $EnvFile -Destination (Join-Path $outDir 'env.txt') -Force
Info "  env.txt written - treat this whole folder as secret"

# --- Manifest ---
@"
Autonomous Accounting backup
Taken:    $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')
Database: $pgName @ ${pgHost}:${pgPort}
Storage:  $storageRoot

CONTENTS
  database.dump  pg_dump custom format
  storage.zip    contents of STORAGE_ROOT (absent if it was empty)
  env.txt        copy of .env  <-- CONTAINS SECRETS, treat this whole folder as secret

RESTORE
  1. Start PostgreSQL:   <AA_HOME>\pgsql\bin\pg_ctl.exe -D <AA_HOME>\pgdata start  (AA_HOME = $Base)
  2. Recreate the database:
       dropdb   -h 127.0.0.1 -U postgres $pgName      # only if replacing
       createdb -h 127.0.0.1 -U postgres $pgName
  3. Restore schema + data:
       pg_restore -h 127.0.0.1 -U postgres -d $pgName --clean --if-exists database.dump
  4. Re-apply role grants (roles live in the cluster, not the dump):
       psql -h 127.0.0.1 -U postgres -d $pgName -f db\local_auth_schema.sql
       psql -h 127.0.0.1 -U postgres -d $pgName -f db\local_grants.sql
  5. Unzip storage.zip into STORAGE_ROOT.
  6. Copy env.txt back to the checkout as .env.
     Keep CREDENTIAL_ENCRYPTION_SECRET, AUTH_JWT_SECRET and STORAGE_URL_SECRET
     byte-identical - changing them invalidates stored integration credentials
     and every signed download link. CREDENTIAL_ENCRYPTION_SECRET is the one
     that keys stored integration credentials (Wise, Gmail, PayPal) and takes
     precedence over AUTH_JWT_SECRET for that; AUTH_JWT_SECRET is only the
     fallback when it is unset.
  7. .\scripts\local\start-aa.ps1
"@ | Set-Content -Path (Join-Path $outDir 'MANIFEST.txt') -Encoding utf8

# --- Prune ---
$cutoff = (Get-Date).AddDays(-$KeepDays)
$pruned = 0
Get-ChildItem $Destination -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^\d{8}-\d{6}$' -and $_.CreationTime -lt $cutoff } |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force; $pruned++ }
if ($pruned) { Info "pruned $pruned backup(s) older than $KeepDays days" }

Info "done -> $outDir"
