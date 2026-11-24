@echo off
title SyncBox installer
echo Installing SyncBox...
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0syncbox.py" --install
  goto :done
)
where python >nul 2>nul
if %errorlevel%==0 (
  python "%~dp0syncbox.py" --install
  goto :done
)
echo.
echo SyncBox needs Python. Your browser will now open the download page.
echo Install Python (tick "Add python.exe to PATH"), then double-click this file again.
start "" https://www.python.org/downloads/
pause
exit /b
:done
timeout /t 4 >nul
