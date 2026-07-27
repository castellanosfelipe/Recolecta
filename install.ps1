[CmdletBinding()]
param(
    [string]$TaskName = "Recolecta",
    [int]$Port = 8091
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$installDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$executable = Join-Path $installDir "Recolecta.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "No se encontró $executable. Ejecute este script desde el bundle extraído."
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $executable -WorkingDirectory $installDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
# Contract: RestartCount 999; MultipleInstances IgnoreNew;
# ExecutionTimeLimit 0; StartWhenAvailable; battery execution allowed.
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings

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
    throw "La tarea se registró, pero $healthUrl no respondió en 20 segundos. Revise logs\app.log."
}

Write-Host "Recolecta quedó instalado para $identity."
Write-Host "Dashboard: http://127.0.0.1:$Port"
