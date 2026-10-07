# Archived: PowerShell version

The original Windows PowerShell/WinForms VaultFlow Ingest, kept as a known-good
fallback now that the Python version in the repo root is the active one.
Tagged `powershell-final`.

- Run with `Run VaultFlow Ingest.bat` (or `powershell -File vaultflow_ingest.ps1`).
- Reads/writes the same `vaultflow_config.json` and `assets/` in the repo root.
- Auto-launch helpers are in `backend/` (not currently registered as a scheduled task).
- Not under active development; fixes land in the Python version.
