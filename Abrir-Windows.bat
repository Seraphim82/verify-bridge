@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3"
)

if "%PY_CMD%"=="" (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY_CMD=python"
    )
)

if "%PY_CMD%"=="" (
    where python3 >nul 2>&1
    if not errorlevel 1 (
        python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY_CMD=python3"
    )
)

if "%PY_CMD%"=="" (
    echo [ERRO] Python 3.10 ou superior nao encontrado.
    echo Baixe em: https://www.python.org/downloads/
    echo Na instalacao, marque a opcao "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo Iniciando Verify Bridge no navegador...
%PY_CMD% -m verify_bridge ui
if not %errorlevel%==0 (
    echo.
    echo Ocorreu um erro ao executar o Verify Bridge.
    pause
)
endlocal
