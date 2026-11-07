@echo off
setlocal
pushd "%~dp0.."
set "ROOT=%CD%"

set "PY=%USERPROFILE%\conda\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/3] Generating app icon...
if not exist "%ROOT%\assets\icon.ico" (
  "%PY%" scripts\generate_icon.py
  if errorlevel 1 (
    echo Icon generation failed.
    popd
    exit /b 1
  )
)

echo [2/3] Building AI engine...
call scripts\build_ai_engine.bat
if errorlevel 1 (
  echo Build aborted at AI engine stage.
  popd
  exit /b 1
)

echo [3/3] Building UI bubble...
call scripts\build_ui.bat
if errorlevel 1 (
  echo Build aborted at UI stage.
  popd
  exit /b 1
)

echo.
echo Build all complete. Release layout:
echo   dist\LocalTranslateBubble.exe    UI (PySide6)
echo   dist\ai_engine.exe               local AI engine
echo   dist\models\rapidocr\            OCR models
echo   dist\models\Hy-MT2-1.8B-GGUF\    auto-download on first run
echo.
echo Optional: pack a release zip with scripts\package_zip.bat
popd
exit /b 0
