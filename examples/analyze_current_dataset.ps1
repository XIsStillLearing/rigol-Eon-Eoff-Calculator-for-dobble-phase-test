$Root = Split-Path -Parent $PSScriptRoot
& (Join-Path $Root "run_analysis.ps1") `
  -InputPath "D:\Codex_workspace_1\双脉冲波形" `
  -Output "D:\Codex_workspace_1\双脉冲波形\program_analysis_packaged" `
  -SaveWaveforms `
  -Plot

