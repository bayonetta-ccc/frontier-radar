@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem ============================================================
rem  Frontier Radar launcher
rem
rem  Usage:
rem    run.cmd demo
rem    run.cmd serve --open
rem    run.cmd claims list --pending
rem
rem  It finds a Python 3.10+ interpreter automatically.
rem ============================================================

set "PROJECT_DIR=%~dp0"
set "PYEXE="
set "PYARGS="

rem use UTF-8 so Chinese output renders correctly in cmd.exe
chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"

rem 1) local virtualenv
if exist "%PROJECT_DIR%.venv\Scripts\python.exe" set "PYEXE=%PROJECT_DIR%.venv\Scripts\python.exe"

rem 2) py launcher from python.org
if not defined PYEXE (
  set "PYMAJOR="
  for /f "delims=" %%V in ('py -3 -c "import sys;print(sys.version_info[0])" 2^>nul') do set "PYMAJOR=%%V"
  if "!PYMAJOR!"=="3" (
    set "PYEXE=py"
    set "PYARGS=-3"
  )
)

rem 3) python on PATH, verified by real output (skips the Store stub)
if not defined PYEXE (
  set "PYMAJOR="
  for /f "delims=" %%V in ('python -c "import sys;print(sys.version_info[0])" 2^>nul') do set "PYMAJOR=%%V"
  if "!PYMAJOR!"=="3" set "PYEXE=python"
)

rem 4) bundled runtime of this environment
if not defined PYEXE (
  set "BUNDLED=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
  if exist "!BUNDLED!" set "PYEXE=!BUNDLED!"
)

rem 5) common install locations
if not defined PYEXE (
  for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles%\Python311\python.exe"
    "%ProgramFiles%\Python310\python.exe"
  ) do (
    if not defined PYEXE if exist %%P set "PYEXE=%%~P"
  )
)

if not defined PYEXE (
  echo [ERROR] No Python 3.10+ found.
  echo         Install Python 3.11+ from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH", then run this script again.
  exit /b 2
)

rem minimum version check
"%PYEXE%" %PYARGS% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 3)" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python is older than 3.10: %PYEXE%
  echo         Please upgrade Python and try again.
  exit /b 3
)

cd /d "%PROJECT_DIR%"
if "%~1"=="" (
  "%PYEXE%" %PYARGS% -m frontier_radar --help
) else (
  "%PYEXE%" %PYARGS% -m frontier_radar %*
)
exit /b %errorlevel%
