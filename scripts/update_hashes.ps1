[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Write-HashManifest {
    param(
        [Parameter(Mandatory)]
        [string]$Directory,
        [Parameter(Mandatory)]
        [string[]]$Patterns
    )

    $items = foreach ($pattern in $Patterns) {
        Get-ChildItem -LiteralPath $Directory -Filter $pattern -File
    }
    $lines = $items |
        Sort-Object -Property Name -Unique |
        ForEach-Object {
            $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            "$hash *$($_.Name)"
        }
    $manifest = Join-Path $Directory "SHA256SUMS.txt"
    [System.IO.File]::WriteAllLines($manifest, [string[]]$lines)
    Write-Host "Actualizado $manifest ($(@($lines).Count) archivos)."
}

Write-HashManifest -Directory (Join-Path $root "wheelhouse") -Patterns @("*.whl")
Write-HashManifest -Directory (Join-Path $root "vendor") -Patterns @("*.exe")
