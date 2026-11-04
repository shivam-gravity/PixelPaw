@echo off
rem 🐾 PixelPaw for Windows: double-click to start. The first run installs everything (about a minute).
setlocal
cd /d "%~dp0"
title PixelPaw

if exist ".venv\Scripts\pythonw.exe" goto launch

echo.
echo   PixelPaw: first-time setup, this takes about a minute...
echo.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto nopython

%PY% -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto failed
echo   Done! Opening PixelPaw...

:launch
start "" ".venv\Scripts\pythonw.exe" "%~dp0PixelPaw.pyw" %*
exit /b 0

:nopython
echo   PixelPaw needs Python 3.10 or newer.
echo   1. Install it from https://www.python.org/downloads/  (tick "Add python.exe to PATH")
echo   2. Double-click PixelPaw.cmd again.
start "" "https://www.python.org/downloads/"
pause
exit /b 1

:failed
echo   Setup failed. Check your internet connection and try again.
if exist ".venv" rmdir /s /q ".venv"
pause
exit /b 1
