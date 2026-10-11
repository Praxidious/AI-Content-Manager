@echo off
setlocal
cd /d "%~dp0"
title JustGlad2BHere ContentBot
rem Preserve direct command-line use; double-clicking opens the menu.
if not "%~1"=="" (
    call "%~dp0process_stream.bat" %*
    exit /b
)
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" start_contentbot.py
) else (
    python start_contentbot.py
)
if errorlevel 1 pause
endlocal
