@echo off
setlocal
pushd "%~dp0.."
set "ROOT=%CD%"
set "VERSION=1.0.0"
if not "%~1"=="" set "VERSION=%~1"

if not exist "%ROOT%\dist\LocalTranslateBubble.exe" (
  echo dist\LocalTranslateBubble.exe not found. Run scripts\build_all.bat first.
  popd
  exit /b 1
)

set "STAGE=%ROOT%\dist-stage"
if exist "%STAGE%" rmdir /s /q "%STAGE%"
rem Exclude the ~1.1GB GGUF model from the release zip (auto-downloaded on first run),
rem and keep local runtime state (settings.json / logs) out of the package.
robocopy "%ROOT%\dist" "%STAGE%" /E /XD "Hy-MT2-1.8B-GGUF" /XF settings.json translate.log /NFL /NDL /NJH /NJS >nul
if errorlevel 8 (
  echo Staging failed.
  popd
  exit /b 1
)

powershell -NoProfile -Command "Compress-Archive -Path '%STAGE%\*' -DestinationPath '%ROOT%\LocalTranslateBubble-v%VERSION%-windows-x64.zip' -Force"
if errorlevel 1 (
  echo Compression failed.
  popd
  exit /b 1
)
rmdir /s /q "%STAGE%"

echo Release zip ready:
echo LocalTranslateBubble-v%VERSION%-windows-x64.zip
popd
exit /b 0
