@echo off
setlocal
title JustGlad2BHere ContentBot - FFmpeg Setup
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_ffmpeg.ps1"
echo.
if errorlevel 1 (
    echo FFmpeg setup failed. Review the error above.
) else (
    echo FFmpeg setup completed successfully.
)
pause
endlocal
