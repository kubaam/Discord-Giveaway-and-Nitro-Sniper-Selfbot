@echo off
setlocal enabledelayedexpansion

set "VENV_DIR=.venv"

if not exist "%VENV_DIR%\" (
    echo Creating virtual environment in %VENV_DIR%...
    py -3 -m venv "%VENV_DIR%" >nul 2>&1
    if errorlevel 1 (
        echo Python launcher not found or failed. Trying "python"...
        python -m venv "%VENV_DIR%"
        if errorlevel 1 (
            echo Failed to create virtual environment.
            exit /b 1
        )
    )
)

if exist "%VENV_DIR%\Scripts\activate.bat" (
    call "%VENV_DIR%\Scripts\activate.bat"
) else (
    echo Virtual environment activation script not found.
    exit /b 1
)

python -m pip install --upgrade pip
if exist requirements.txt (
    pip install -r requirements.txt
) else (
    echo requirements.txt not found. Skipping dependency installation.
)

python main.py

endlocal
pause
