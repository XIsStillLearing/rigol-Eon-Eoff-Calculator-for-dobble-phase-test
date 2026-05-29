[CmdletBinding()]
param(
    [switch]$User
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Requirements = Join-Path $Root "requirements.txt"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Cannot find python in PATH. Install Python 3.10+ first."
}

$args = @("-m", "pip", "install", "-r", $Requirements)
if ($User) {
    $args += "--user"
}

& python @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Dependencies installed."

