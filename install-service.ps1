[CmdletBinding()]
param(
    [string]$TaskName = "Recolecta-Service",
    [int]$Port = 8091
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Este modo requiere PowerShell ejecutado como administrador."
}

$installDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$executable = Join-Path $installDir "Recolecta.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "No se encontró $executable. Ejecute este script desde el bundle extraído."
}

$action = New-ScheduledTaskAction `
    -Execute $executable `
    -Argument "--service" `
    -WorkingDirectory $installDir
$trigger = New-ScheduledTaskTrigger -AtStartup
# Contract: RestartCount 999; MultipleInstances IgnoreNew;
# ExecutionTimeLimit 0; StartWhenAvailable; WakeToRun; battery execution allowed.
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$systemPrincipal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $systemPrincipal

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$healthUrl = "http://127.0.0.1:$Port/healthz"
$healthy = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            $healthy = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $healthy) {
    throw "La tarea SYSTEM se registró, pero $healthUrl no respondió en 20 segundos. Revise logs\app.log."
}

Write-Host "Recolecta quedó instalado como SYSTEM al arrancar."
Write-Host "Dashboard: http://127.0.0.1:$Port"
