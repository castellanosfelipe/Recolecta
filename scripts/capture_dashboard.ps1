[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$shotData = Join-Path $root "work\screenshot-data"
$imageDir = Join-Path $root "docs\images"
$edgeProfile = Join-Path $root "work\edge-profile"
New-Item -ItemType Directory -Path $shotData, $imageDir, $edgeProfile -Force |
    Out-Null

$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path -LiteralPath $edge -PathType Leaf)) {
    throw "Microsoft Edge no está disponible en $edge."
}
$python = Join-Path $root ".venv-build\Scripts\python.exe"
$screenshot = Join-Path $imageDir "dashboard.png"
$oldData = $env:HARVESTER_DATA_DIR
$appProcess = $null
try {
    $env:HARVESTER_DATA_DIR = $shotData
    $appProcess = Start-Process `
        -FilePath $python `
        -ArgumentList @((Join-Path $root "launcher.py"), "--service") `
        -WorkingDirectory $root `
        -PassThru `
        -WindowStyle Hidden

    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $response = Invoke-WebRequest `
                -Uri "http://127.0.0.1:8091/healthz" `
                -UseBasicParsing `
                -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $ready) {
        throw "El dashboard no respondió para capturar la imagen."
    }

    $edgeArgs = @(
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--user-data-dir=$edgeProfile",
        "--window-size=1440,1000",
        "--screenshot=$screenshot",
        "http://127.0.0.1:8091/"
    )
    $edgeProcess = Start-Process `
        -FilePath $edge `
        -ArgumentList $edgeArgs `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($edgeProcess.ExitCode -ne 0 -or
        -not (Test-Path -LiteralPath $screenshot -PathType Leaf)) {
        throw "Edge no pudo capturar el dashboard."
    }
    Write-Host "Captura actualizada: $screenshot"
} finally {
    if ($null -ne $appProcess -and -not $appProcess.HasExited) {
        Stop-Process -Id $appProcess.Id -Force
    }
    $env:HARVESTER_DATA_DIR = $oldData
}
