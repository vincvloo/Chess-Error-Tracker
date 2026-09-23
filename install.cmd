@echo off
rem Double-click this on Windows to install and start Chess Error Tracker.
rem (Runs install.ps1 without needing to change PowerShell's execution policy.)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 pause
