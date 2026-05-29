param(
    [Parameter(Mandatory = $true)]
    [string]$DataFolder,

    [string]$OutputFolder = (Join-Path $DataFolder "dpt_analysis_out")
)

$Root = Split-Path -Parent $PSScriptRoot
& (Join-Path $Root "run_analysis.ps1") `
  -InputPath $DataFolder `
  -Output $OutputFolder `
  -Plot

