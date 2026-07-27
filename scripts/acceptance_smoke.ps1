[CmdletBinding()]
param(
    [string]$ZipPath = "",
    [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $ZipPath) {
    $ZipPath = Join-Path $root "dist\Recolecta-win64.zip"
}
if (-not $ReportPath) {
    $ReportPath = Join-Path $root "dist\acceptance-smoke.json"
}
$ZipPath = [System.IO.Path]::GetFullPath($ZipPath)
$ReportPath = [System.IO.Path]::GetFullPath($ReportPath)
if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
    throw "No existe el paquete $ZipPath."
}

$manifestPath = Join-Path (Split-Path -Parent $ZipPath) "SHA256SUMS.txt"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "No existe el manifiesto $manifestPath."
}
$manifestLine = (Get-Content -LiteralPath $manifestPath | Select-Object -First 1)
if ($manifestLine -notmatch '^([0-9a-fA-F]{64}) \*Recolecta-win64\.zip$') {
    throw "El manifiesto de release tiene un formato inválido."
}
$expectedHash = $Matches[1]
$actualHash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash
if ($actualHash -ne $expectedHash) {
    throw "El SHA-256 del ZIP no coincide con el manifiesto."
}

$workRoot = [System.IO.Path]::GetFullPath((Join-Path $root "work"))
New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
$smokeRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $workRoot ("acceptance-smoke-" + [guid]::NewGuid().ToString("N")))
)
if (-not $smokeRoot.StartsWith(
    $workRoot + [System.IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Ruta temporal insegura: $smokeRoot"
}
New-Item -ItemType Directory -Path $smokeRoot | Out-Null

$listener = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback,
    0
)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$oldDataDir = $env:RECOLECTA_DATA_DIR
$oldPort = $env:RECOLECTA_PORT
$process = $null
try {
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $smokeRoot
    $bundle = Join-Path $smokeRoot "Recolecta"
    $executable = Join-Path $bundle "Recolecta.exe"
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "El ZIP no contiene Recolecta\Recolecta.exe."
    }
    if (Get-ChildItem -LiteralPath $bundle -Filter "python.exe" -Recurse -File) {
        throw "El paquete no debe depender de un python.exe externo."
    }

    $selfTestReport = Join-Path $smokeRoot "frozen-self-test.txt"
    $selfTest = Start-Process `
        -FilePath $executable `
        -ArgumentList @(
            "--self-test",
            "--self-test-report",
            $selfTestReport
        ) `
        -WorkingDirectory $bundle `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($selfTest.ExitCode -ne 0) {
        if (Test-Path -LiteralPath $selfTestReport) {
            Get-Content -LiteralPath $selfTestReport | Write-Host
        }
        throw "El autodiagnóstico extraído falló con código $($selfTest.ExitCode)."
    }

    $stateDir = Join-Path $smokeRoot "state"
    $env:RECOLECTA_DATA_DIR = $stateDir
    $env:RECOLECTA_PORT = [string]$port
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process `
        -FilePath $executable `
        -WorkingDirectory $bundle `
        -PassThru `
        -WindowStyle Hidden

    $healthUrl = "http://127.0.0.1:$port/healthz"
    $health = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($process.HasExited) {
            throw "El ejecutable terminó con código $($process.ExitCode)."
        }
        try {
            $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 1
            break
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    $stopwatch.Stop()
    if ($null -eq $health) {
        throw "$healthUrl no respondió."
    }
    if ($stopwatch.Elapsed.TotalSeconds -ge 5) {
        throw "El dashboard tardó $($stopwatch.Elapsed.TotalSeconds) s; excede 5 s."
    }
    if ($health.status -ne "ok" -or $health.app -ne "Recolecta") {
        throw "La respuesta de healthz es inválida."
    }
    $dashboard = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$port/" `
        -UseBasicParsing `
        -TimeoutSec 2
    $javascript = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$port/static/app.js" `
        -UseBasicParsing `
        -TimeoutSec 2
    if ($dashboard.StatusCode -ne 200 -or
        $dashboard.Content -notmatch "Recolecta" -or
        $javascript.StatusCode -ne 200) {
        throw "El dashboard o sus recursos estáticos no están disponibles."
    }

    $report = [ordered]@{
        checked_at_utc = [DateTime]::UtcNow.ToString("o")
        zip_sha256 = $actualHash.ToLowerInvariant()
        start_seconds = [math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
        health_status = $health.status
        app = $health.app
        version = $health.version
        dashboard_status = $dashboard.StatusCode
        static_status = $javascript.StatusCode
        frozen_self_test = "passed"
        external_python_required = $false
        result = "passed"
    }
    $json = $report | ConvertTo-Json
    [System.IO.Directory]::CreateDirectory(
        (Split-Path -Parent $ReportPath)
    ) | Out-Null
    [System.IO.File]::WriteAllText($ReportPath, $json + "`r`n")
    Write-Host "Smoke test aprobado en $($report.start_seconds) s."
    Write-Host "Evidencia: $ReportPath"
} catch {
    $appLog = Join-Path $smokeRoot "state\logs\app.log"
    if (Test-Path -LiteralPath $appLog -PathType Leaf) {
        Write-Host "Últimas líneas de app.log:"
        Get-Content -LiteralPath $appLog -Tail 50 | Write-Host
    }
    throw
} finally {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit()
    }
    $env:RECOLECTA_DATA_DIR = $oldDataDir
    $env:RECOLECTA_PORT = $oldPort
    if (Test-Path -LiteralPath $smokeRoot) {
        Remove-Item -LiteralPath $smokeRoot -Recurse -Force
    }
}
