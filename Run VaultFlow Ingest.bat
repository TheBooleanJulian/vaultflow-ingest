@echo off
rem pythonw = no console window; falls back to the py launcher if pythonw isn't on PATH.
where pythonw >nul 2>nul && (start "" pythonw "%~dp0vaultflow_ingest.py" & exit /b 0)
start "" pyw -3 "%~dp0vaultflow_ingest.py"
