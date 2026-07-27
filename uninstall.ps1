[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$installDir = [System.IO.Path]::GetFullPath(
    (Split-Path -Parent $MyInvocation.MyCommand.Path)
).TrimEnd('\')
$taskNames = @("FileHarvester", "FileHarvester-Service")

foreach ($taskName in $taskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Tarea eliminada: $taskName"
    }
}

$processes = Get-CimInstance Win32_Process -Filter "Name = 'FileHarvester.exe'"
foreach ($process in $processes) {
    if (-not $process.ExecutablePath) {
        continue
    }
    $processDir = [System.IO.Path]::GetFullPath(
        (Split-Path -Parent $process.ExecutablePath)
    ).TrimEnd('\')
    if ($processDir.Equals($installDir, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Process -Id $process.ProcessId -Force
        Write-Host "Proceso detenido: $($process.ProcessId)"
    }
}

Write-Host "FileHarvester quedó desregistrado."
Write-Host "Se conservaron la aplicación, data\, logs\, exports\ y todos los archivos descargados."
