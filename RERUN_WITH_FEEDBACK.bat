@echo off
setlocal
title ContentBot - Rescan with your votes
cd /d "%~dp0"
echo Rescanning the latest saved full VOD with your current feedback.
echo Saved audio and transcription will be reused.
".venv\Scripts\python.exe" process_stream.py --rerun-feedback %*
if errorlevel 1 echo Rescan failed. Review the error above and logs.
pause
