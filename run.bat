@echo off
setlocal enabledelayedexpansion

rem Switch to the script directory
cd /d "%~dp0"

echo ==========================================================
echo   Kalm -- AI Gateway Controller Startup Script
echo ==========================================================
echo [INFO] Current working directory: %CD%
echo.

rem Initialize default parameters
set "PORT="
set "HOST_ADDRESS="
set "INSTALL_DEPS=false"
set "EXIT_CODE=0"

rem Parse command line arguments
:parse_args
if "%~1"=="--port" (
    set "PORT=%~2"
    shift
    shift
    goto parse_args
)
if "%~1"=="--host" (
    set "HOST_ADDRESS=%~2"
    shift
    shift
    goto parse_args
)
if "%~1"=="--install-deps" (
    set "INSTALL_DEPS=true"
    shift
    goto parse_args
)
if not "%~1"=="" (
    echo [ERROR] Unknown option: %1
    echo.
    echo Usage: run.bat [--host ^<host^>] [--port ^<port^>] [--install-deps]
    exit /b 1
)

rem === 1. Python Environment Detection ===
set "PYTHON_CMD="
set "ENV_DIR="

rem Check if conda is available in PATH
where conda >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "CONDA_AVAILABLE=true"
) else (
    set "CONDA_AVAILABLE=false"
)

rem Find candidate local virtual environment directories
if exist "%~dp0python3.11" (
    set "ENV_DIR=%~dp0python3.11"
) else if exist "%~dp0.venv" (
    set "ENV_DIR=%~dp0.venv"
) else if exist "%~dp0venv" (
    set "ENV_DIR=%~dp0venv"
)

if not defined ENV_DIR goto check_conda_env

echo [INFO] Detected local environment directory: %ENV_DIR%
if not "%CONDA_AVAILABLE%"=="true" goto use_direct_python

echo [INFO] Activating Conda virtual environment...
call conda deactivate >nul 2>&1
call conda activate "%ENV_DIR%" >nul 2>&1
if !ERRORLEVEL! equ 0 (
    echo [SUCCESS] Conda environment activated successfully.
    set "PYTHON_CMD=python"
    goto python_found
)
echo [WARNING] Conda activation failed. Trying direct python executable...

:use_direct_python
if exist "%ENV_DIR%\python.exe" (
    set "PYTHON_CMD=%ENV_DIR%\python.exe"
    goto python_found
)
if exist "%ENV_DIR%\Scripts\python.exe" (
    set "PYTHON_CMD=%ENV_DIR%\Scripts\python.exe"
    goto python_found
)

:check_conda_env
if not "%CONDA_AVAILABLE%"=="true" goto check_system_python
echo [INFO] Trying to activate Conda named environment 'kalm'...
call conda deactivate >nul 2>&1
call conda activate kalm >nul 2>&1
if !ERRORLEVEL! equ 0 (
    echo [SUCCESS] Conda environment 'kalm' activated successfully.
    set "PYTHON_CMD=python"
    goto python_found
)

:check_system_python
where python >nul 2>&1
if !ERRORLEVEL! equ 0 (
    echo [INFO] No local environment found. Using system default Python.
    set "PYTHON_CMD=python"
    goto python_found
)

echo [ERROR] No usable Python environment found!
echo Please ensure Python is installed or create a virtual environment in the project root.
set "EXIT_CODE=1"
goto final_exit

:python_found
echo [INFO] Python Interpreter: !PYTHON_CMD!
for /f "delims=" %%i in ('!PYTHON_CMD! --version 2^>^&1') do echo [INFO] %%i
echo.

rem === 2. Dependency Check and Installation ===
set "NEED_INSTALL_DEPS=false"
!PYTHON_CMD! -c "import uvicorn, fastapi, yaml" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    set "NEED_INSTALL_DEPS=true"
)

if not "%NEED_INSTALL_DEPS%"=="true" goto skip_install_deps
echo [WARNING] Current Python environment is missing required packages [fastapi, uvicorn, yaml]...
if not "%INSTALL_DEPS%"=="false" goto do_install

set /p "CHOICE=Would you like to install dependencies from requirements.txt automatically? [y/n]: "
if /i "%CHOICE%"=="y" goto do_install
echo [ERROR] Missing required dependencies. Cannot start service.
set "EXIT_CODE=1"
goto final_exit

:do_install
if not exist "requirements.txt" (
    echo [WARNING] requirements.txt not found. Skipping dependency installation.
    goto skip_install_deps
)
echo [INFO] Installing dependencies, please wait...
!PYTHON_CMD! -m pip install -r requirements.txt
if !ERRORLEVEL! equ 0 (
    echo [SUCCESS] Dependencies installed successfully!
) else (
    echo [ERROR] Failed to install dependencies. Please check your network or pip configuration.
    set "EXIT_CODE=1"
    goto final_exit
)

:skip_install_deps
echo.

rem === 3. Service Start ===
echo ==========================================================
echo   Starting Kalm Main Service... (Press Ctrl+C to stop)
echo ==========================================================
echo.

rem Build command-line arguments to pass to main.py
set "MAIN_ARGS="
if defined HOST_ADDRESS set "MAIN_ARGS=!MAIN_ARGS! --host !HOST_ADDRESS!"
if defined PORT set "MAIN_ARGS=!MAIN_ARGS! --port !PORT!"

!PYTHON_CMD! main.py !MAIN_ARGS!
set "EXIT_CODE=%ERRORLEVEL%"

rem === 4. Final Exit ===
:final_exit
echo.
echo ==========================================================
echo   Kalm Service Stopped (Exit Code: %EXIT_CODE%)
echo ==========================================================

echo %cmdcmdline% | findstr /i "cmd.exe" >nul
if %ERRORLEVEL% equ 0 (
    echo %cmdcmdline% | findstr /i "/c" >nul
    if !ERRORLEVEL! equ 0 (
        echo.
        pause
    )
)

endlocal
exit /b %EXIT_CODE%
