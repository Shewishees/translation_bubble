# -*- mode: python ; coding: utf-8 -*-
"""UI 悬浮气泡打包配置：PySide6 原生壳 → LocalTranslateBubble.exe（onedir）。

打包态行为（app.py / engine_supervisor.py 已适配）：
- ROOT = exe 所在目录，与 ai_engine.exe / models / settings.json 同级；
- 图标资源经 ASSET_DIR（sys._MEIPASS）加载；
- 引擎优先拉起同级 ai_engine.exe；启动跳过 pip 引导。
"""
from pathlib import Path

import sys

ROOT = Path(SPECPATH)

binaries = []
# conda 发行版解释器的标准库扩展（_ctypes/_lzma）依赖 <conda>\Library\bin
# 下的运行时 DLL；官方 python.org 解释器无此依赖，存在才收集。
_conda_bin = Path(sys.base_prefix) / "Library" / "bin"
if _conda_bin.is_dir():
    for _name in ("ffi.dll", "liblzma.dll", "libexpat.dll", "sqlite3.dll"):
        _p = _conda_bin / _name
        if _p.is_file():
            binaries.append((str(_p), "."))

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=[
        (str(ROOT / "bubble-icon.svg"), "."),
        (str(ROOT / "retry.svg"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "torch",
        "transformers",
        "backend",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalTranslateBubble",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ROOT / "assets" / "icon.ico")],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LocalTranslateBubble",
)
