@echo off
setlocal
title JustGlad2BHere ContentBot
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"

echo ============================================================
echo  JustGlad2BHere ContentBot
echo ============================================================
echo.

where ffmpeg.exe >nul 2>nul
if errorlevel 1 (
    if not exist "tools\ffmpeg\bin\ffmpeg.exe" (
        echo FFmpeg is required but is not installed.
        choice /C YN /M "Download and install the official FFmpeg build inside this project now"
        if errorlevel 2 (
            echo.
            echo Setup cancelled. No files were downloaded.
            pause
            exit /b 1
        )
        echo.
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_ffmpeg.ps1"
        if errorlevel 1 (
            echo.
            echo FFmpeg setup failed. Review the error above.
            pause
            exit /b 1
        )
        echo.
    )
)

if not exist "%PYTHON_EXE%" (
    echo The project Python environment has not been set up yet.
    choice /C YN /M "Create it and install GPU transcription dependencies now"
    if errorlevel 2 (
        echo.
        echo Setup cancelled. No Python packages were installed.
        pause
        exit /b 1
    )
    echo.
    python -m venv .venv
    if errorlevel 1 (
        echo Could not create .venv. Confirm that 64-bit Python is installed.
        pause
        exit /b 1
    )
)

"%PYTHON_EXE%" -c "import ctranslate2, faster_whisper; from pathlib import Path; import sys; root=Path(sys.prefix)/'Lib'/'site-packages'/'nvidia'; assert (root/'cublas'/'bin').is_dir() and (root/'cudnn'/'bin').is_dir()" >nul 2>nul
if errorlevel 1 (
    echo The GPU transcription dependencies are incomplete.
    echo This one-time download is approximately 1.5 GB.
    choice /C YN /M "Install them inside this project's .venv now"
    if errorlevel 2 (
        echo.
        echo Setup cancelled.
        pause
        exit /b 1
    )
    echo.
    "%PYTHON_EXE%" -m pip install -r requirements-windows-gpu.txt
    if errorlevel 1 (
        echo.
        echo Python dependency setup failed. Review the error above.
        pause
        exit /b 1
    )
    echo.
)

if "%~1"=="--setup-only" exit /b 0

echo Starting the requested VOD workflow...
echo.

if "%~1"=="" (
    "%PYTHON_EXE%" process_stream.py
) else (
    "%PYTHON_EXE%" process_stream.py %*
)

if errorlevel 1 (
    echo.
    echo Processing failed. Review the error above and the newest file in logs\.
    pause
) else (
    echo.
    echo Processing complete. The trainer should now be open in your browser.
    pause
)
endlocal
