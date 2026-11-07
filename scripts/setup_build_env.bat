@echo off
setlocal
set "ROOT=%CD%"
set "VPY=%ROOT%\.build-venv\Scripts\python.exe"

if not exist "%VPY%" (
  echo Creating build venv at .build-venv ...
  python -m venv "%ROOT%\.build-venv"
  if errorlevel 1 exit /b 1
)

"%VPY%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1

rem --- llama-cpp-python: GPU build (NVIDIA) uses the cu124 prebuilt wheel index;
rem --- CPU build installs from PyPI (prebuilt wheels available up to Python 3.12;
rem --- on Python 3.13 the CPU wheel falls back to a source build and needs MSVC).
where nvidia-smi >nul 2>nul
if errorlevel 1 (
  echo Installing llama-cpp-python ^(CPU^) from PyPI ...
  "%VPY%" -m pip install --disable-pip-version-check "llama-cpp-python>=0.3.0"
  if errorlevel 1 exit /b 1
) else (
  echo Installing llama-cpp-python ^(CUDA cu124 prebuilt wheel^) ...
  "%VPY%" -m pip install --disable-pip-version-check "llama-cpp-python>=0.3.0" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
  if errorlevel 1 exit /b 1
  echo Installing CUDA runtime wheels required by the cu124 build ...
  "%VPY%" -m pip install --disable-pip-version-check nvidia-cuda-runtime-cu12 nvidia-cublas-cu12
  if errorlevel 1 exit /b 1
)

echo Installing project dependencies and PyInstaller ...
"%VPY%" -m pip install --disable-pip-version-check -r "%ROOT%\requirements.txt" -r "%ROOT%\requirements-build.txt"
if errorlevel 1 exit /b 1

echo.
echo Build environment ready at .build-venv
exit /b 0
