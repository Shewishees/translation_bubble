@echo off
setlocal
pushd "%~dp0.."
set "ROOT=%CD%"

set "PY=%ROOT%\.build-venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\conda\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/3] Checking PyInstaller...
"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo Installing PyInstaller...
  "%PY%" -m pip install --disable-pip-version-check --no-input "pyinstaller>=6.0,<7"
  if errorlevel 1 (
    echo PyInstaller install failed.
    popd
    exit /b 1
  )
)

echo [2/3] Building ai_engine.exe ...
rem CUDA runtime DLLs (cudart/cublas) are collected explicitly by
rem backend\ai_engine.spec into llama_cpp/lib, so the GPU build is
rem self-contained and does NOT depend on build-time PATH.
"%PY%" -m PyInstaller --noconfirm --clean --distpath "%ROOT%\dist" --workpath "%ROOT%\build" "%ROOT%\backend\ai_engine.spec"
if errorlevel 1 (
  echo AI engine build failed.
  popd
  exit /b 1
)

echo [3/3] Copying RapidOCR models...
"%PY%" -c "from pathlib import Path; import shutil, rapidocr_onnxruntime; src=Path(rapidocr_onnxruntime.__file__).resolve().parent/'models'; dst=Path(r'%ROOT%')/'dist'/'models'/'rapidocr'; dst.mkdir(parents=True, exist_ok=True); [shutil.copy2(item, dst/item.name) for item in src.glob('*.onnx') if not (dst/item.name).exists()]"
if errorlevel 1 (
  echo Failed to copy RapidOCR models.
  popd
  exit /b 1
)

echo AI engine build complete:
echo %ROOT%\dist\ai_engine.exe
popd
exit /b 0
