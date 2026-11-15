@echo off
cd /d "%~dp0.."
echo FinFlux BC - downloading symbols offline...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0download-global-symbols.ps1"
pause
