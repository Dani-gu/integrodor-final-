$ErrorActionPreference = "Stop"

$envPath = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "No se encontró .env en la carpeta del proyecto."
}

$driveDirectory = $null
foreach ($line in Get-Content -LiteralPath $envPath) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $separator = $line.IndexOf('=')
    $key = $line.Substring(0, $separator).Trim()
    if ($key -eq "DRIVE_BACKUP_DIRECTORY") {
        $driveDirectory = $line.Substring($separator + 1).Trim().Trim('"')
        break
    }
}

if ([string]::IsNullOrWhiteSpace($driveDirectory)) {
    throw "Primero configura DRIVE_BACKUP_DIRECTORY en .env con la carpeta MATRIPLAST dentro de Google Drive para escritorio."
}

$driveParent = Split-Path -Parent $driveDirectory
if (-not (Test-Path -LiteralPath $driveParent)) {
    throw "No existe la carpeta de Google Drive: $driveParent. Inicia Google Drive para escritorio y verifica la ruta configurada."
}

$taskName = "MATRIPLAST - Respaldo diario PostgreSQL"
$powerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$backupScript = Join-Path $PSScriptRoot "backup_postgres.ps1"
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$backupScript`" -RequireDrive"
$action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At "8:00 PM"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Crea y valida un respaldo diario de PostgreSQL y lo deja en la carpeta sincronizada de Google Drive." -Force | Out-Null
Write-Output "Tarea configurada: $taskName"
Write-Output "Frecuencia: diaria a las 8:00 p. m. (cuando tu usuario haya iniciado sesión)."
Write-Output "Destino: $driveDirectory"
Write-Output "Para probarla ahora: Start-ScheduledTask -TaskName `"$taskName`""
