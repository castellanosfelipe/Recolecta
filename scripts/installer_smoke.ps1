[CmdletBinding()]
param(
    [string]$SetupPath = "",
    [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $SetupPath) {
    $SetupPath = Join-Path $root "dist\Recolecta-Setup.exe"
}
if (-not $ReportPath) {
    $ReportPath = Join-Path $root "dist\installer-smoke.json"
}
$SetupPath = [System.IO.Path]::GetFullPath($SetupPath)
$ReportPath = [System.IO.Path]::GetFullPath($ReportPath)
if (-not (Test-Path -LiteralPath $SetupPath -PathType Leaf)) {
    throw "No existe el instalador $SetupPath."
}

$manifestPath = Join-Path (Split-Path -Parent $SetupPath) "SHA256SUMS.txt"
$setupLine = Get-Content -LiteralPath $manifestPath |
    Where-Object { $_ -match '\*Recolecta-Setup\.exe$' } |
    Select-Object -First 1
if ($setupLine -notmatch '^([0-9a-fA-F]{64}) \*Recolecta-Setup\.exe$') {
    throw "El manifiesto no contiene un hash válido para el instalador."
}
$expectedHash = $Matches[1]
$actualHash = (Get-FileHash -LiteralPath $SetupPath -Algorithm SHA256).Hash
if ($actualHash -ne $expectedHash) {
    throw "El SHA-256 del instalador no coincide con el manifiesto."
}

$workRoot = [System.IO.Path]::GetFullPath((Join-Path $root "work"))
New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
$smokeRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $workRoot ("installer-smoke-" + [guid]::NewGuid().ToString("N")))
)
if (-not $smokeRoot.StartsWith(
    $workRoot + [System.IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Ruta temporal insegura: $smokeRoot"
}
New-Item -ItemType Directory -Path $smokeRoot | Out-Null

try {
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $setup = Start-Process `
        -FilePath $SetupPath `
        -ArgumentList @(
            "--install-dir",
            "`"$smokeRoot`"",
            "--extract-only"
        ) `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    $stopwatch.Stop()
    if ($setup.ExitCode -ne 0) {
        throw "Recolecta-Setup terminó con código $($setup.ExitCode)."
    }

    $executable = Join-Path $smokeRoot "Recolecta.exe"
    $installReport = Join-Path $smokeRoot "install-report.json"
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf) -or
        -not (Test-Path -LiteralPath $installReport -PathType Leaf)) {
        throw "El instalador no extrajo el ejecutable y su evidencia."
    }
    $selfTestReport = Join-Path $smokeRoot "frozen-self-test.txt"
    $selfTest = Start-Process `
        -FilePath $executable `
        -ArgumentList @("--self-test", "--self-test-report", $selfTestReport) `
        -WorkingDirectory $smokeRoot `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($selfTest.ExitCode -ne 0) {
        throw "El ejecutable instalado falló el autodiagnóstico."
    }

    $report = [ordered]@{
        checked_at_utc = [DateTime]::UtcNow.ToString("o")
        setup_sha256 = $actualHash.ToLowerInvariant()
        extraction_seconds = [math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
        executable = "Recolecta.exe"
        scheduled_task_changed = $false
        frozen_self_test = "passed"
        result = "passed"
    }
    [System.IO.Directory]::CreateDirectory(
        (Split-Path -Parent $ReportPath)
    ) | Out-Null
    [System.IO.File]::WriteAllText(
        $ReportPath,
        ($report | ConvertTo-Json) + "`r`n"
    )
    Write-Host "Smoke del instalador aprobado en $($report.extraction_seconds) s."
    Write-Host "Evidencia: $ReportPath"
} finally {
    if (Test-Path -LiteralPath $smokeRoot) {
        Remove-Item -LiteralPath $smokeRoot -Recurse -Force
    }
}
