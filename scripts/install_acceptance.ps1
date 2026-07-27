[CmdletBinding()]
param(
    [string]$SetupPath = "",
    [string]$EvidencePath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $SetupPath) {
    $SetupPath = Join-Path $root "dist\Recolecta-Setup.exe"
}
if (-not $EvidencePath) {
    $EvidencePath = Join-Path $root "dist\install-acceptance.json"
}
$workRoot = [System.IO.Path]::GetFullPath((Join-Path $root "work")).TrimEnd("\")
$target = Join-Path $workRoot (
    "one-command-install-" + [guid]::NewGuid().ToString("N")
)
$target = [System.IO.Path]::GetFullPath($target)
$setup = [System.IO.Path]::GetFullPath($SetupPath)
$evidence = [System.IO.Path]::GetFullPath($EvidencePath)

if (-not $target.StartsWith(
    $workRoot + "\",
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "El destino temporal quedó fuera de work."
}
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
    throw "No se encontró el instalador: $setup"
}
if (Get-ScheduledTask -TaskName "Recolecta" -ErrorAction SilentlyContinue) {
    throw "Ya existe una tarea Recolecta; la prueba no la sobrescribirá."
}

$listener = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback,
    0
)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$started = Get-Date
$result = [ordered]@{
    app = "Recolecta"
    command = "Recolecta-Setup.exe --install-dir <temporal> --port $port"
    port = $port
    installed = $false
    task_registered = $false
    health_status = $null
    dashboard_http = $null
    javascript_http = $null
    uninstalled = $false
    task_removed = $false
    process_stopped = $false
    port_closed = $false
    error = $null
    retained_target = $null
}
$failure = $null

try {
    $installerProcess = Start-Process `
        -FilePath $setup `
        -ArgumentList @(
            "--install-dir",
            "`"$target`"",
            "--port",
            $port
        ) `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($installerProcess.ExitCode -ne 0) {
        throw "El instalador terminó con código $($installerProcess.ExitCode)."
    }
    $result.installed = $true

    $task = Get-ScheduledTask -TaskName "Recolecta" -ErrorAction Stop
    $action = @($task.Actions)[0]
    $expectedExecutable = Join-Path $target "Recolecta.exe"
    if (-not $action.Execute.Equals(
        $expectedExecutable,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "La tarea apunta a un ejecutable inesperado: $($action.Execute)"
    }
    if ($action.Arguments -notmatch "--port\s+$port(?:\s|$)") {
        throw "La tarea no recibió el puerto ${port}: $($action.Arguments)"
    }
    $result.task_registered = $true

    $health = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$port/healthz" `
        -TimeoutSec 5
    if ($health.status -ne "ok" -or $health.app -ne "Recolecta") {
        throw "La respuesta de /healthz no fue válida."
    }
    $result.health_status = $health.status

    $dashboard = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$port/" `
        -UseBasicParsing `
        -TimeoutSec 5
    $javascript = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$port/static/app.js" `
        -UseBasicParsing `
        -TimeoutSec 5
    if ($dashboard.StatusCode -ne 200 -or $javascript.StatusCode -ne 200) {
        throw "El dashboard o su JavaScript no respondió HTTP 200."
    }
    $result.dashboard_http = $dashboard.StatusCode
    $result.javascript_http = $javascript.StatusCode

    $reportPath = Join-Path $target "install-report.json"
    $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if ($report.result -ne "installed" -or [int]$report.port -ne $port) {
        throw "install-report.json no refleja la instalación solicitada."
    }
} catch {
    $failure = $_
    $result.error = $_.Exception.Message
} finally {
    $uninstaller = Join-Path $target "uninstall.ps1"
    if (Test-Path -LiteralPath $uninstaller -PathType Leaf) {
        & powershell.exe `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $uninstaller |
            Out-Host
    } else {
        $task = Get-ScheduledTask `
            -TaskName "Recolecta" `
            -ErrorAction SilentlyContinue
        if ($null -ne $task) {
            Stop-ScheduledTask `
                -TaskName "Recolecta" `
                -ErrorAction SilentlyContinue
            Unregister-ScheduledTask `
                -TaskName "Recolecta" `
                -Confirm:$false
        }
    }

    Start-Sleep -Milliseconds 800
    $result.task_removed = -not [bool](
        Get-ScheduledTask `
            -TaskName "Recolecta" `
            -ErrorAction SilentlyContinue
    )
    $targetProcesses = @(
        Get-CimInstance Win32_Process -Filter "Name = 'Recolecta.exe'" |
            Where-Object {
                $_.ExecutablePath -and
                $_.ExecutablePath.StartsWith(
                    $target + "\",
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    $result.process_stopped = $targetProcesses.Count -eq 0

    try {
        $probe = [System.Net.Sockets.TcpClient]::new()
        $connection = $probe.ConnectAsync("127.0.0.1", $port)
        if (-not $connection.Wait(1000)) {
            $result.port_closed = $true
        } else {
            $result.port_closed = -not $probe.Connected
        }
        $probe.Dispose()
    } catch {
        $result.port_closed = $true
    }

    $result.uninstalled = (
        $result.task_removed -and
        $result.process_stopped -and
        $result.port_closed
    )
    if (
        $null -eq $failure -and
        $result.uninstalled -and
        (Test-Path -LiteralPath $target)
    ) {
        Remove-Item -LiteralPath $target -Recurse -Force
    } elseif (Test-Path -LiteralPath $target) {
        $result.retained_target = $target
    }
}

$result.elapsed_seconds = [math]::Round(
    ((Get-Date) - $started).TotalSeconds,
    3
)
$result.result = if ($null -eq $failure -and $result.uninstalled) {
    "passed"
} else {
    "failed"
}
$result | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath $evidence -Encoding UTF8

if ($null -ne $failure) {
    Write-Error $failure.Exception.Message
    exit 1
}
if (-not $result.uninstalled) {
    throw "La instalación funcionó, pero la limpieza posterior quedó incompleta."
}

Write-Host "Instalación real aprobada en $($result.elapsed_seconds) s."
Write-Host "Evidencia: $evidence"
