@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  where python3 >nul 2>nul
  if not errorlevel 1 set "PY=python3"
)
if not defined PY (
  echo.
  echo   [ERROR] Python 3.9+ not found.
  echo   Download: https://www.python.org/downloads/
  echo   Remember to check "Add Python to PATH" during install.
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting Anime RSS Reader ...
echo   Tip: run "start-windows.bat --lan" to allow phone / tablet on the same WiFi.
echo.

"%PY%" app.py %*

if errorlevel 1 (
  echo.
  echo   Service exited with an error. Press any key to close.
  pause >nul
)
endlocal
