@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_analysis.ps1" %*
exit /b %ERRORLEVEL%

