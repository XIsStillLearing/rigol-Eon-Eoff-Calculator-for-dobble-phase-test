[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$InputPath,

    [string]$Output = (Join-Path (Get-Location) "dpt_analysis_out"),

    [int]$VdsTrace = 2,
    [int]$CurrentTrace = 3,

    [ValidateSet("q1", "q2")]
    [string]$VdsInput = "q1",

    [ValidateSet("direct", "ct-voltage")]
    [string]$CurrentMode = "direct",

    [ValidateSet("global", "local")]
    [string]$VbusMode = "global",

    [double]$CtZeroV = 1.65,
    [double]$CtVPerA = 0.00625,
    [double]$CurrentScale = 1.0,
    [Nullable[double]]$CurrentBaseline = $null,

    [double]$MinPulseUs = 0.5,
    [double]$DebounceNs = 20.0,
    [double]$CurrentWindowNs = 100.0,
    [double]$LevelWindowNs = 500.0,

    [switch]$SaveWaveforms,
    [switch]$Plot
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Analyzer = Join-Path $Root "src\rigol_dpt_analyzer.py"

if (-not (Test-Path -LiteralPath $Analyzer)) {
    throw "Analyzer script not found: $Analyzer"
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Cannot find python in PATH. Install Python 3.10+ first."
}

$pyArgs = @(
    $Analyzer
) + $InputPath + @(
    "-o", $Output,
    "--vds-trace", "$VdsTrace",
    "--current-trace", "$CurrentTrace",
    "--vds-input", $VdsInput,
    "--current-mode", $CurrentMode,
    "--vbus-mode", $VbusMode,
    "--ct-zero-v", "$CtZeroV",
    "--ct-v-per-a", "$CtVPerA",
    "--current-scale", "$CurrentScale",
    "--min-pulse-us", "$MinPulseUs",
    "--debounce-ns", "$DebounceNs",
    "--current-window-ns", "$CurrentWindowNs",
    "--level-window-ns", "$LevelWindowNs"
)

if ($null -ne $CurrentBaseline) {
    $pyArgs += @("--current-baseline", "$CurrentBaseline")
}
if ($SaveWaveforms) {
    $pyArgs += "--save-waveforms"
}
if ($Plot) {
    $pyArgs += "--plot"
}

& python @pyArgs
exit $LASTEXITCODE

