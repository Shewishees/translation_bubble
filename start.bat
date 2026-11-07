@echo off
setlocal
cd /d "%~dp0"
if exist "%USERPROFILE%\conda\pythonw.exe" (
    start "Local Translate Bubble" "%USERPROFILE%\conda\pythonw.exe" "%~dp0launcher.py"
    exit /b 0
)
if exist "%USERPROFILE%\conda\python.exe" (
    start "Local Translate Bubble" "%USERPROFILE%\conda\python.exe" "%~dp0launcher.py"
    exit /b 0
)
where pythonw.exe >nul 2>&1
if not errorlevel 1 (
    start "Local Translate Bubble" pythonw.exe "%~dp0launcher.py"
    exit /b 0
)
where python.exe >nul 2>&1
if not errorlevel 1 (
    start "Local Translate Bubble" python.exe "%~dp0launcher.py"
    exit /b 0
)
echo 未找到带 PySide6 的 Python。请使用 Conda 环境或安装 PySide6 后重试。
pause

