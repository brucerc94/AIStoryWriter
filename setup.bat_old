@echo off
setlocal enabledelayedexpansion

echo ================================================
echo   AI Story Studio - Setup
echo ================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No se encontro "python" en el PATH.
    echo Instala Python 3.12+ desde https://www.python.org/downloads/
    echo y asegurate de marcar "Add python.exe to PATH" durante la instalacion.
    pause
    exit /b 1
)

if not exist .venv (
    echo [1/5] Creando entorno virtual en .\.venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
) else (
    echo [1/5] El entorno virtual .\.venv ya existe, se reutiliza.
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] No se pudo activar el entorno virtual.
    pause
    exit /b 1
)

echo [2/5] Actualizando pip...
python -m pip install --upgrade pip

echo [3/5] Instalando PySide6...
pip install PySide6
if errorlevel 1 (
    echo [ERROR] Fallo la instalacion de PySide6.
    pause
    exit /b 1
)

echo [4/5] Instalando llama-cpp-python con soporte CUDA (GTX 1660 Ti)...
echo        Esto compila desde el codigo fuente y puede tardar varios minutos.
set CMAKE_ARGS=-DGGML_CUDA=on
set FORCE_CMAKE=1
pip install llama-cpp-python --upgrade --force-reinstall --no-cache-dir

if errorlevel 1 (
    echo.
    echo [AVISO] La compilacion con soporte CUDA fallo.
    echo Verifica que tengas instalado:
    echo   - NVIDIA CUDA Toolkit
    echo   - Visual Studio Build Tools ^(C++^)
    echo   - CMake
    echo.
    echo Se instalara la version CPU como respaldo para que la app funcione igual.
    set CMAKE_ARGS=
    set FORCE_CMAKE=
    pip install llama-cpp-python
)

echo [5/5] Verificando instalacion...
python -c "import PySide6; print('  PySide6 OK')"
python -c "import llama_cpp; print('  llama-cpp-python OK')"

echo.
echo ================================================
echo   Setup completo.
echo   Ejecuta run.bat para iniciar AI Story Studio.
echo ================================================
pause
