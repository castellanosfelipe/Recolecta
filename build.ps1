[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root ".venv-build"
$wheelhouse = Join-Path $root "wheelhouse"
$vendorInstaller = Join-Path $root "vendor\python-3.12.10-amd64.exe"
$distRoot = Join-Path $root "dist"
$bundle = Join-Path $distRoot "Recolecta"
$zipPath = Join-Path $distRoot "Recolecta-win64.zip"
$installerPath = Join-Path $distRoot "Recolecta-Setup.exe"

function Invoke-Checked {
    param(
        [Parameter(Mandatory)]
        [string]$Label,
        [Parameter(Mandatory)]
        [scriptblock]$Action
    )
    Write-Host "`n==> $Label"
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Label falló con código $LASTEXITCODE."
    }
}

function Test-HashManifest {
    param([Parameter(Mandatory)][string]$Directory)

    $manifest = Join-Path $Directory "SHA256SUMS.txt"
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw "Falta el manifiesto $manifest."
    }
    foreach ($line in Get-Content -LiteralPath $manifest) {
        if ($line -notmatch '^([0-9a-fA-F]{64}) \*(.+)$') {
            throw "Línea inválida en $manifest`: $line"
        }
        $expected = $Matches[1]
        $file = Join-Path $Directory $Matches[2]
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            throw "Falta el archivo inventariado $file."
        }
        $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
        if ($actual -ne $expected) {
            throw "SHA-256 inválido para $file."
        }
    }
}

function Find-Python312 {
    $probe = "import platform,struct,sys;prefix=(sys.base_prefix+' '+sys.prefix+' '+sys.version).lower();valid=sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 and platform.python_implementation()=='CPython' and 'conda' not in prefix and 'anaconda' not in prefix;raise SystemExit(0 if valid else 1)"
    $privatePython = Join-Path $root ".python-build"
    $privateExe = Join-Path $privatePython "python.exe"
    if (Test-Path -LiteralPath $privateExe -PathType Leaf) {
        & $privateExe -c $probe 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ Command = $privateExe; Arguments = @() }
        }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -c $probe 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ Command = "py"; Arguments = @("-3.12") }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c $probe 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ Command = "python"; Arguments = @() }
        }
    }

    if (-not (Test-Path -LiteralPath $vendorInstaller -PathType Leaf)) {
        throw "No se encontró CPython 3.12 x64 ni el instalador offline $vendorInstaller."
    }
    Write-Host "Instalando CPython 3.12.10 privado desde vendor/..."
    $installerArgs = @(
        "/quiet",
        "InstallAllUsers=0",
        "Include_launcher=0",
        "Include_test=0",
        "PrependPath=0",
        "TargetDir=$privatePython"
    )
    $process = Start-Process -FilePath $vendorInstaller -ArgumentList $installerArgs -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "El bootstrap de Python falló con código $($process.ExitCode)."
    }
    if (-not (Test-Path -LiteralPath $privateExe -PathType Leaf)) {
        throw "El bootstrap terminó sin crear $privateExe."
    }
    return @{ Command = $privateExe; Arguments = @() }
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "La compilación requiere Windows x64."
}
Test-HashManifest -Directory $wheelhouse
Test-HashManifest -Directory (Join-Path $root "vendor")

$python = Find-Python312
$venvResolved = [System.IO.Path]::GetFullPath($venv)
$rootResolved = [System.IO.Path]::GetFullPath($root)
if (-not $venvResolved.StartsWith($rootResolved, [StringComparison]::OrdinalIgnoreCase) -or
    (Split-Path -Leaf $venvResolved) -ne ".venv-build") {
    throw "Ruta de entorno de compilación insegura: $venvResolved"
}
if (Test-Path -LiteralPath $venvResolved) {
    Remove-Item -LiteralPath $venvResolved -Recurse -Force
}

Invoke-Checked "Crear entorno de compilación CPython 3.12 x64" {
    & $python.Command @($python.Arguments) -m venv $venvResolved
}
$buildPython = Join-Path $venvResolved "Scripts\python.exe"

$oldNoIndex = $env:PIP_NO_INDEX
$oldFindLinks = $env:PIP_FIND_LINKS
try {
    $env:PIP_NO_INDEX = "1"
    $env:PIP_FIND_LINKS = $wheelhouse
    Invoke-Checked "Instalar dependencias únicamente desde wheelhouse/" {
        & $buildPython -m pip install --no-index --find-links $wheelhouse -r (Join-Path $root "requirements-dev.txt")
    }
} finally {
    $env:PIP_NO_INDEX = $oldNoIndex
    $env:PIP_FIND_LINKS = $oldFindLinks
}

Invoke-Checked "Ejecutar pruebas" {
    & $buildPython -m pytest
}
Invoke-Checked "Ejecutar autodiagnóstico en código fuente" {
    & $buildPython (Join-Path $root "launcher.py") --self-test
}

$pyinstallerArgs = @(
    "--name", "Recolecta",
    "--onedir",
    "--noconsole",
    "--noconfirm",
    "--clean",
    "--distpath", $distRoot,
    "--workpath", (Join-Path $root "build"),
    "--specpath", (Join-Path $root "build"),
    "--add-data", "$root\static;static",
    "--add-data", "$root\templates;templates",
    "--hidden-import", "win32crypt",
    "--hidden-import", "winotify",
    "--hidden-import", "winsound",
    "--hidden-import", "pystray",
    "--hidden-import", "pystray._win32",
    "--hidden-import", "PIL.Image",
    "--hidden-import", "PIL.ImageDraw",
    # platform.py cae de forma segura a sys.getwindowsversion() sin _wmi.
    "--exclude-module", "_wmi",
    "--hidden-import", "cryptography.hazmat.primitives",
    "--hidden-import", "cryptography.hazmat.primitives.ciphers",
    "--hidden-import", "cryptography.hazmat.primitives.kdf.pbkdf2",
    "--collect-submodules", "apscheduler",
    "--collect-submodules", "cryptography",
    "--collect-submodules", "tzdata",
    (Join-Path $root "launcher.py")
)

$distResolved = [System.IO.Path]::GetFullPath($distRoot)
$rootResolved = [System.IO.Path]::GetFullPath($root)
if (-not $distResolved.StartsWith(
    $rootResolved + [System.IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Ruta de distribución insegura: $distResolved"
}
if (Test-Path -LiteralPath $distResolved) {
    Remove-Item -LiteralPath $distResolved -Recurse -Force
}

Invoke-Checked "Congelar Recolecta con PyInstaller" {
    & $buildPython -m PyInstaller @pyinstallerArgs
}

Copy-Item -LiteralPath (Join-Path $root "install.ps1") -Destination $bundle -Force
Copy-Item -LiteralPath (Join-Path $root "install-service.ps1") -Destination $bundle -Force
Copy-Item -LiteralPath (Join-Path $root "uninstall.ps1") -Destination $bundle -Force
Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination $bundle -Force
Copy-Item -LiteralPath (Join-Path $root "LICENSE") -Destination $bundle -Force
Copy-Item -LiteralPath (Join-Path $root "docs") -Destination $bundle -Recurse -Force

$required = @(
    (Join-Path $bundle "Recolecta.exe"),
    (Join-Path $bundle "install.ps1"),
    (Join-Path $bundle "install-service.ps1"),
    (Join-Path $bundle "uninstall.ps1"),
    (Join-Path $bundle "_internal\static\app.js"),
    (Join-Path $bundle "_internal\templates\index.html")
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "El bundle no contiene $path."
    }
}
$selfTestReport = Join-Path $root "build\frozen-self-test.txt"
$frozenTest = Start-Process `
    -FilePath (Join-Path $bundle "Recolecta.exe") `
    -ArgumentList @("--self-test", "--self-test-report", $selfTestReport) `
    -WorkingDirectory $bundle `
    -Wait `
    -PassThru `
    -WindowStyle Hidden
if ($frozenTest.ExitCode -ne 0) {
    if (Test-Path -LiteralPath $selfTestReport) {
        Get-Content -LiteralPath $selfTestReport | Write-Host
    }
    throw "El autodiagnóstico congelado falló con código $($frozenTest.ExitCode)."
}
Get-Content -LiteralPath $selfTestReport | Write-Host

$bundleBytes = (Get-ChildItem -LiteralPath $bundle -Recurse -File | Measure-Object -Property Length -Sum).Sum
if ($bundleBytes -gt 120MB) {
    throw "El bundle mide $([math]::Round($bundleBytes / 1MB, 1)) MB; excede el límite de 120 MB."
}

$installerArgs = @(
    "--name", "Recolecta-Setup",
    "--onefile",
    "--noconsole",
    "--noconfirm",
    "--clean",
    "--distpath", $distRoot,
    "--workpath", (Join-Path $root "build\installer"),
    "--specpath", (Join-Path $root "build"),
    "--add-data", "$bundle;payload\Recolecta",
    (Join-Path $root "installer.py")
)
Invoke-Checked "Construir instalador offline Recolecta-Setup" {
    & $buildPython -m PyInstaller @installerArgs
}
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "PyInstaller no produjo $installerPath."
}
$installerBytes = (Get-Item -LiteralPath $installerPath).Length
if ($installerBytes -gt 120MB) {
    throw "El instalador mide $([math]::Round($installerBytes / 1MB, 1)) MB; excede el límite de 120 MB."
}

if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -LiteralPath $bundle -DestinationPath $zipPath -CompressionLevel Optimal
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$installerHash = (
    Get-FileHash -LiteralPath $installerPath -Algorithm SHA256
).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText(
    (Join-Path $distRoot "SHA256SUMS.txt"),
    "$zipHash *Recolecta-win64.zip`r`n" +
    "$installerHash *Recolecta-Setup.exe`r`n"
)

Write-Host "`nBundle listo: $bundle"
Write-Host "ZIP listo: $zipPath"
Write-Host "Instalador listo: $installerPath"
Write-Host "Tamaño del bundle: $([math]::Round($bundleBytes / 1MB, 1)) MB"
Write-Host "Tamaño del instalador: $([math]::Round($installerBytes / 1MB, 1)) MB"
