@echo off
REM pc-eye - double-click this. It installs Pillow if needed, then launches the server + tunnel.
setlocal
cd /d "%~dp0"
title pc-eye (keep this window open - closing it stops the agent)
echo.
echo   pc-eye starting...
echo.
where py >nul 2>nul
if errorlevel 1 (
  set PY=python
) else (
  set PY=py -3
)
%PY% -c "import PIL" >nul 2>nul
if errorlevel 1 (
  echo   installing Pillow ^(one time, needed to read the screen^)...
  %PY% -m pip install --quiet --disable-pip-version-check pillow
)
%PY% launcher.py --port 8790
echo.
echo   pc-eye has stopped. Press any key to close.
pause >nul
