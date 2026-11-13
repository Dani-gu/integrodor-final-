param([switch]$RequireDrive)

$ErrorActionPreference = "Stop"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$localBackupDirectory = Join-Path $PSScriptRoot "backups"
New-Item -ItemType Directory -Path $localBackupDirectory -Force | Out-Null
$logPath = Join-Path $localBackupDirectory "backup.log"
trap {
    Add-Content -LiteralPath $logPath -Value "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) ERROR $($_.Exception.Message)" -Encoding UTF8 -ErrorAction SilentlyContinue
    throw
}

$envPath = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "No se encontró .env. Crea ese archivo con la configuración local de PostgreSQL antes de respaldar."
}

$databaseSettings = @{}
foreach ($line in Get-Content -LiteralPath $envPath) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $separator = $line.IndexOf('=')
    $key = $line.Substring(0, $separator).Trim()
    $value = $line.Substring($separator + 1).Trim()
    $databaseSettings[$key] = $value
}

foreach ($key in @("DB_NAME", "DB_USER", "DB_PASSWORD")) {
    if (-not $databaseSettings.ContainsKey($key) -or [string]::IsNullOrWhiteSpace($databaseSettings[$key])) {
        throw "Falta configurar $key en .env."
    }
}

$pgDump = (Get-Command "pg_dump.exe" -ErrorAction SilentlyContinue).Source
if (-not $pgDump) {
    $postgresRoot = Join-Path $env:ProgramFiles "PostgreSQL"
    if (Test-Path -LiteralPath $postgresRoot) {
        $pgDump = Get-ChildItem -LiteralPath $postgresRoot -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName "bin\pg_dump.exe" } |
            Where-Object { Test-Path -LiteralPath $_ } |
            Select-Object -First 1
    }
}
if (-not $pgDump) {
    throw "No se encontró pg_dump. Agrega PostgreSQL\18\bin al PATH o repara la instalación de PostgreSQL."
}

$configuredDriveDirectory = $databaseSettings["DRIVE_BACKUP_DIRECTORY"]
if ($null -ne $configuredDriveDirectory) {
    $configuredDriveDirectory = $configuredDriveDirectory.Trim().Trim('"')
}
if ($RequireDrive -and [string]::IsNullOrWhiteSpace($configuredDriveDirectory)) {
    throw "Configura DRIVE_BACKUP_DIRECTORY en .env con la carpeta de Google Drive para usar el respaldo diario."
}

$backupDirectory = if (-not [string]::IsNullOrWhiteSpace($configuredDriveDirectory)) {
    $configuredDriveDirectory
} else {
    Join-Path $PSScriptRoot "backups"
}

if ($RequireDrive -and -not (Test-Path -LiteralPath (Split-Path -Parent $backupDirectory))) {
    throw "No se encuentra la carpeta principal de Google Drive: $(Split-Path -Parent $backupDirectory). Inicia Google Drive para escritorio y revisa la ruta en .env."
}
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
$logPath = Join-Path $backupDirectory "backup.log"
$backupPath = Join-Path $backupDirectory "matriplast_$timestamp.dump"
$databaseHost = if ([string]::IsNullOrWhiteSpace($databaseSettings["DB_HOST"])) { "127.0.0.1" } else { $databaseSettings["DB_HOST"] }
$databasePort = if ([string]::IsNullOrWhiteSpace($databaseSettings["DB_PORT"])) { "5432" } else { $databaseSettings["DB_PORT"] }
$previousPgPassword = $env:PGPASSWORD
$env:PGPASSWORD = $databaseSettings["DB_PASSWORD"]

try {
    & $pgDump `
        "--format=custom" `
        "--no-owner" `
        "--no-privileges" `
        "--host=$databaseHost" `
        "--port=$databasePort" `
        "--username=$($databaseSettings['DB_USER'])" `
        "--file=$backupPath" `
        $databaseSettings["DB_NAME"]

    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump terminó con código $LASTEXITCODE. No se pudo completar el respaldo."
    }

    if (-not (Test-Path -LiteralPath $backupPath) -or (Get-Item -LiteralPath $backupPath).Length -eq 0) {
        throw "El archivo de respaldo no existe o está vacío."
    }

    $pgRestore = Join-Path (Split-Path -Parent $pgDump) "pg_restore.exe"
    if (Test-Path -LiteralPath $pgRestore) {
        & $pgRestore --list $backupPath *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "pg_restore no pudo leer el respaldo; el archivo puede estar dañado."
        }
    }
}
finally {
    if ($null -eq $previousPgPassword) {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    }
    else {
        $env:PGPASSWORD = $previousPgPassword
    }
}

Write-Output "Respaldo creado: $backupPath"
Add-Content -LiteralPath $logPath -Value "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) OK $backupPath" -Encoding UTF8
if ($RequireDrive) {
    Write-Output "Google Drive sincronizará la carpeta configurada: $backupDirectory"
}
