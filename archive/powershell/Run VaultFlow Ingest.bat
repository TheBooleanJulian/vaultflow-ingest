@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0vaultflow_ingest.ps1"
if errorlevel 1 pause
