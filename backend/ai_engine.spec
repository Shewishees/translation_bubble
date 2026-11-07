# -*- mode: python ; coding: utf-8 -*-
"""AI 引擎打包配置（对齐 PopTrans backend/ai_engine.spec）。

产出单文件 ai_engine.exe：内嵌 FastAPI + llama-cpp-python + RapidOCR，
用户无需安装 Python。GGUF 翻译模型不打进 exe（约 1.08GB，首次运行
自动联网下载），RapidOCR ONNX 模型由构建脚本复制到 dist/models/rapidocr。
"""
from pathlib import Path
import sysconfig

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


ROOT = Path(SPECPATH).parent

import sys

binaries = collect_dynamic_libs("llama_cpp")

# conda 发行版解释器的标准库扩展（_ctypes/_lzma/pyexpat/_sqlite3/ssl）
# 依赖 <conda>\Library\bin 下的运行时 DLL；官方 python.org 解释器把这些
# 依赖静态嵌入、无此文件。存在则收集，不存在（官方 Python）则跳过。
_conda_bin = Path(sys.base_prefix) / "Library" / "bin"
if _conda_bin.is_dir():
    for _name in (
        "ffi.dll",
        "liblzma.dll",
        "libexpat.dll",
        "sqlite3.dll",
        "libssl-3-x64.dll",
        "libcrypto-3-x64.dll",
    ):
        _p = _conda_bin / _name
        if _p.is_file():
            binaries.append((str(_p), "."))
# NVIDIA CUDA 运行时（cudart/cublas）是 ggml-cuda.dll 的依赖，
# llama-cpp-python 的 CUDA wheel 不捆绑它们，依赖构建环境 PATH 中
# 的 nvidia pip 包。强制收进 llama_cpp/lib，与 llama.dll/ggml-*.dll
# 同目录，保证任意 DLL 搜索策略下都能命中（GPU 构建自包含）。
nvidia_base = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
for _sub in ("cuda_runtime", "cublas"):
    _bin = nvidia_base / _sub / "bin"
    if _bin.is_dir():
        for _dll in _bin.glob("*.dll"):
            binaries.append((str(_dll), "llama_cpp/lib"))

datas = collect_data_files(
    "rapidocr_onnxruntime",
    includes=["config.yaml"],
)
datas += copy_metadata("python-multipart")

hiddenimports = collect_submodules("uvicorn")
hiddenimports += collect_submodules("rapidocr_onnxruntime")
hiddenimports += [
    "multipart",
    "python_multipart",
    "python_multipart.multipart",
]

a = Analysis(
    [str(ROOT / "backend" / "api_server.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "jupyter",
        "matplotlib",
        "onnxruntime.quantization",
        "onnxruntime.tools",
        "tkinter",
        "PySide6",
        # rapidocr<=1.2.3 会条件性 import torch；确保即使构建环境装了 torch
        # 也不会把它（~2GB）和分析链拉进引擎包
        "torch",
        "transformers",
        "PyQt5",
        "scipy",
        "pandas",
        # 可选依赖修剪：在"全家桶"环境下，huggingface_hub→fsspec 等可选实现
        # 链会把下列重型包拖进分析图。引擎运行时（fastapi/llama.cpp/rapidocr）
        # 完全不需要它们；在干净 venv 构建时这些排除项天然无副作用。
        "dask",
        "distributed",
        "bokeh",
        "panel",
        "plotly",
        "altair",
        "sklearn",
        "scikit-learn",
        "skimage",
        "statsmodels",
        "patsy",
        "astropy",
        "sphinx",
        "docutils",
        "notebook",
        "jupyterlab",
        "nbformat",
        "nbconvert",
        "sqlalchemy",
        "pyarrow",
        "xarray",
        "numba",
        "llvmlite",
        "lxml",
        "h5py",
        "imageio",
        "pywt",
        "pytest",
        "botocore",
        "boto3",
        "intake",
        "zope",
        "win32com",
        "pythoncom",
        "gi",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ai_engine",
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
