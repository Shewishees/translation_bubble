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

echo [2/3] Building LocalTranslateBubble.exe (onedir)...
"%PY%" -m PyInstaller --noconfirm --clean --distpath "%ROOT%\dist-ui" --workpath "%ROOT%\build" "%ROOT%\LocalTranslateBubble.spec"
if errorlevel 1 (
  echo UI build failed.
  popd
  exit /b 1
)

echo [3/3] Assembling dist\ (UI onedir + shared models/ai_engine)...
robocopy "%ROOT%\dist-ui\LocalTranslateBubble" "%ROOT%\dist" /E /MOVE /NFL /NDL /NJH /NJS >nul
if errorlevel 8 (
  echo Failed to assemble dist directory.
  popd
  exit /b 1
)
if exist "%ROOT%\dist-ui" rmdir /s /q "%ROOT%\dist-ui"
copy /Y "%ROOT%\assets\icon.ico" "%ROOT%\dist\icon.ico" >nul

echo UI build complete:
echo %ROOT%\dist\LocalTranslateBubble.exe
popd
exit /b 0
