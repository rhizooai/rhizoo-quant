@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"

echo ============================================
echo   Rhizoo Alpha Bot: Environment Setup
echo ============================================

:: Create venv if it doesn't exist
if not exist "%VENV_DIR%\" (
    echo [1/3] Creating virtual environment...
    python -m venv "%VENV_DIR%"
) else (
    echo [1/3] Virtual environment already exists, skipping.
)

:: Activate
call "%VENV_DIR%\Scripts\activate.bat"

:: Upgrade pip
echo [2/3] Upgrading pip...
pip install --upgrade pip --quiet

:: Install dependencies
echo [3/3] Installing dependencies from requirements.txt...
pip install -r "%SCRIPT_DIR%requirements.txt" --quiet

echo.
echo ============================================
echo   Setup complete.
echo   Activate with: %VENV_DIR%\Scripts\activate.bat
echo ============================================

endlocal
