@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0dist\LocalTranslateBubble.exe" (
    start "" "%~dp0dist\LocalTranslateBubble.exe"
    exit /b 0
)

if exist "%~dp0.build-venv\Scripts\pythonw.exe" (
    start "Local Translate Bubble" "%~dp0.build-venv\Scripts\pythonw.exe" -m bubble.launcher
    exit /b 0
)
if exist "%~dp0.build-venv\Scripts\python.exe" (
    start "Local Translate Bubble" "%~dp0.build-venv\Scripts\python.exe" -m bubble.launcher
    exit /b 0
)
if exist "%USERPROFILE%\conda\pythonw.exe" (
    start "Local Translate Bubble" "%USERPROFILE%\conda\pythonw.exe" -m bubble.launcher
    exit /b 0
)
if exist "%USERPROFILE%\conda\python.exe" (
    start "Local Translate Bubble" "%USERPROFILE%\conda\python.exe" -m bubble.launcher
    exit /b 0
)
where pythonw.exe >nul 2>&1
if not errorlevel 1 (
    start "Local Translate Bubble" pythonw.exe -m bubble.launcher
    exit /b 0
)
where python.exe >nul 2>&1
if not errorlevel 1 (
    start "Local Translate Bubble" python.exe -m bubble.launcher
    exit /b 0
)
echo 未找到可用运行环境。请先安装 PySide6 或使用发行版 dist\LocalTranslateBubble.exe。
pause
