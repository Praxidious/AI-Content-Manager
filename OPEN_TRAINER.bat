@echo off
setlocal
title JustGlad2BHere ContentBot Trainer
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"

echo ============================================================
echo  JustGlad2BHere ContentBot Trainer
echo ============================================================
echo.

if not exist "%PYTHON_EXE%" (
    echo The project Python environment has not been set up yet.
    echo Run RUN_CONTENTBOT.bat once to complete setup.
    echo.
    pause
    exit /b 1
)

"%PYTHON_EXE%" -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo The trainer dependency is missing.
    echo Installing the project's required packages...
    "%PYTHON_EXE%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Setup failed. Review the error above.
        pause
        exit /b 1
    )
    echo.
)

echo Starting the trainer and opening http://localhost:5050 ...
"%PYTHON_EXE%" review_clips.py --open-browser %*
if errorlevel 1 (
    echo.
    echo The trainer could not open. Review logs\review_server.log.
    pause
    exit /b 1
)

echo Trainer opened. You may close this window.
timeout /t 2 /nobreak >nul
endlocal
