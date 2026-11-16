@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    echo Python nao encontrado. Instale Python 3.10 ou superior com Tcl/Tk e adicione ao PATH.
    pause
    exit /b 1
)
start "Block Launcher" pythonw "%~dp0launcher.py"
