@echo off
rem ============================================================
rem  KSCC Proxy Launcher (Windows)
rem  Usage:  start.bat                    (default config)
rem           start.bat --port 9000       (extra args passed through)
rem
rem  If kscc_proxy.json is missing or kscc_token / kscc_base_url
rem  are empty (and no KSCC_AUTH_TOKEN env var), the program will
rem  interactively prompt and write the answers back to the file.
rem  First run, install deps first:
rem      pip install -r kscc_proxy\requirements.txt
rem ============================================================

rem UTF-8 codepage so the program's Chinese prompts print correctly
chcp 65001 >nul

rem Go to the project root (d:\Project, two levels up from scripts\) so python -m kscc_proxy finds the package
cd /d "%~dp0..\.."

rem Pick Python: prefer plain python, fall back to the py launcher
where python >nul 2>nul
if errorlevel 1 (
    set "PY=py -3"
) else (
    set "PY=python"
)

%PY% -m kscc_proxy --config "kscc_proxy\config\kscc_proxy.json" %*

echo.
echo If you saw "No module named ...", run:  pip install -r kscc_proxy\requirements.txt
echo [kscc_proxy exited] press any key to close...
pause >nul
