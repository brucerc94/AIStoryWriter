@echo off
setlocal


if not exist .venv (
    echo [ERROR] No se encontro el entorno virtual ".venv".
    echo Ejecuta primero setup.bat
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] No se pudo activar el entorno virtual.
    pause
    exit /b 1
)

rem GTX 1660 Ti (Turing, CC 7.5) no tiene Tensor Cores reales:
rem forzar MMQ para evitar los kernels cuBLAS/Tensor Core.
rem engine\chat.py tambien lo detecta y aplica esto automaticamente,
rem pero se fija aqui tambien por si el backend lo lee al arrancar.
set GGML_CUDA_FORCE_MMQ=1

echo ================================================
echo   AI Story Studio
echo ================================================
echo.

.venv\Scripts\python.exe main.py

if errorlevel 1 (
    echo.
    echo [ERROR] La aplicacion termino con un error. Revisa el mensaje de arriba.
    pause
)
