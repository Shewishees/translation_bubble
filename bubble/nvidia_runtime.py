"""NVIDIA CUDA 运行时 DLL 路径处理。

llama-cpp-python 的 CUDA 预编译轮（abetlen cu124 索引，Python 3.13 下唯一的
预编译渠道）不捆绑 cudart/cublas，依赖 `nvidia-cuda-runtime-cu12` /
`nvidia-cublas-cu12` pip 包提供的 DLL。这些 DLL 位于 site-packages/nvidia/
之下、不在系统 PATH 中，因此引擎子进程启动前需要把对应目录加入 PATH。
"""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

NVIDIA_RUNTIME_PACKAGES = ["nvidia-cuda-runtime-cu12", "nvidia-cublas-cu12"]


def nvidia_dll_dirs() -> list[Path]:
    """返回已安装的 NVIDIA 运行时 DLL 目录（不存在则跳过）。"""
    base = Path(sysconfig.get_paths().get("purelib") or "")
    if not str(base):
        return []
    candidates = (
        base / "nvidia" / "cuda_runtime" / "bin",
        base / "nvidia" / "cublas" / "bin",
    )
    return [path for path in candidates if path.is_dir()]


def extend_env_path(env: dict) -> dict:
    """把 NVIDIA 运行时 DLL 目录前置进环境副本的 PATH。"""
    dirs = nvidia_dll_dirs()
    if not dirs:
        return env
    prefix = os.pathsep.join(str(path) for path in dirs) + os.pathsep
    env["PATH"] = prefix + env.get("PATH", "")
    return env


def subprocess_env() -> dict:
    """构造带 NVIDIA DLL 路径的子进程环境。"""
    return extend_env_path(os.environ.copy())
