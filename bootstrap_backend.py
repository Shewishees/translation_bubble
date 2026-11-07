"""准备 PopTrans AI 引擎运行环境（依赖安装 + RapidOCR ONNX 模型复制）。

职责划分与 PopTrans 一致：
- 本脚本只负责 pip 依赖与 OCR ONNX 模型落位（优先从 rapidocr-onnxruntime
  wheel 内置模型复制，对齐其 scripts/build_ai_engine.bat 的做法）；
- Hy-MT2 GGUF 翻译模型（约 1.13GB）由引擎 backend/translator.py 按需
  断点续传下载，进度经 /health 的 translator_status 实时回传到界面。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from diagnostics import record_diagnostic
from nvidia_runtime import NVIDIA_RUNTIME_PACKAGES, subprocess_env

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

ROOT = Path(__file__).resolve().parent
MARKER = ROOT / ".runtime" / "python-ready.json"

CUDA_WHEEL_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cu124"

PIP_BUNDLE = [
    "fastapi>=0.104.0",
    "uvicorn>=0.23.2",
    "python-multipart>=0.0.6",
    # 注意：rapidocr-onnxruntime>=1.3.0 仅支持 Python<3.13；
    # 不加上限约束让 pip 按当前解释器自动选择兼容版本
    "rapidocr-onnxruntime",
    "requests>=2.31.0",
    "Pillow>=10.0.0",
    "pyyaml>=6.0",
    "huggingface_hub>=0.20.0",
]

REQUIRED_MODULES = (
    "fastapi",
    "uvicorn",
    "rapidocr_onnxruntime",
    "requests",
    "PIL",
    "yaml",
    "huggingface_hub",
)

# rapidocr wheel 内置的 ONNX 模型全量复制（v3/v4 文件名随 wheel 版本不同）

GGUF_RELATIVE = Path("models") / "Hy-MT2-1.8B-GGUF" / "Hy-MT2-1.8B-Q4_K_M.gguf"


def emit(message: str) -> None:
    print(message, flush=True)


def module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def multipart_present() -> bool:
    # python-multipart 的导入名在 0.0.13 前后为 multipart / python_multipart 二选一
    return module_present("multipart") or module_present("python_multipart")


def run_pip(arguments: list[str]) -> int:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        *arguments,
    ]
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return process.wait()


def ensure_common_dependencies() -> bool:
    missing = [name for name in REQUIRED_MODULES if not module_present(name)]
    if not multipart_present():
        missing.append("python-multipart")
    if not missing:
        return True
    emit("正在安装本地推理组件…")
    return run_pip(PIP_BUNDLE) == 0


def llama_importable() -> bool:
    """用子进程真实 import llama_cpp 验证 DLL 依赖完整（含 NVIDIA 运行时 PATH）。"""
    try:
        check = subprocess.run(
            [sys.executable, "-c", "import llama_cpp"],
            env=subprocess_env(),
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return check.returncode == 0


def install_cpu_llama() -> int:
    return run_pip(["llama-cpp-python>=0.3.0", "--force-reinstall"])


def ensure_llama_cpp() -> bool:
    """安装 llama-cpp-python：优先 NVIDIA CUDA 预编译轮，import 验证失败回退 CPU 轮。"""
    has_nvidia = shutil.which("nvidia-smi") is not None

    if module_present("llama_cpp"):
        if llama_importable():
            return True
        if has_nvidia:
            emit("正在补齐 CUDA 运行时组件…")
            if run_pip(NVIDIA_RUNTIME_PACKAGES) == 0 and llama_importable():
                return True
            emit("CUDA 版本不可用，回退通用 CPU 版本…")
            install_cpu_llama()
            if llama_importable():
                return True
        else:
            emit("已安装的推理组件无法加载，正在重装…")
            install_cpu_llama()
            if llama_importable():
                return True
        emit("⚠️ GGUF 推理组件加载失败（当前 Python 可能缺少预编译组件），请点击重试")
        return False

    emit("正在安装 GGUF 推理组件（llama-cpp-python）…")
    if has_nvidia:
        emit("检测到 NVIDIA 显卡，安装 CUDA 加速版本…")
        if run_pip(["llama-cpp-python>=0.3.0", "--extra-index-url", CUDA_WHEEL_INDEX]) == 0:
            emit("正在安装 CUDA 运行时组件…")
            run_pip(NVIDIA_RUNTIME_PACKAGES)
            if llama_importable():
                return True
        emit("CUDA 版本安装失败，回退通用 CPU 版本…")
        install_cpu_llama()
        if llama_importable():
            return True
    else:
        if run_pip(["llama-cpp-python>=0.3.0"]) == 0 and llama_importable():
            return True
    emit("⚠️ llama-cpp-python 安装失败（当前 Python 可能缺少预编译组件），请点击重试")
    return False


def copy_ocr_models() -> None:
    """从 rapidocr-onnxruntime wheel 内置模型目录复制全部 ONNX 到 models/rapidocr/。"""
    target_dir = ROOT / "models" / "rapidocr"
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        import rapidocr_onnxruntime

        source_dir = Path(rapidocr_onnxruntime.__file__).resolve().parent / "models"
    except Exception:
        return
    for source in sorted(source_dir.glob("*.onnx")):
        target = target_dir / source.name
        if not target.is_file():
            try:
                shutil.copy2(source, target)
            except OSError:
                pass


def model_hint() -> str:
    if GGUF_RELATIVE.is_file():
        return "翻译模型已就绪"
    return "首次使用将自动下载 Hy-MT2 翻译模型（约 1.13GB），完成后完全离线"


def main() -> int:
    try:
        record_diagnostic(ROOT, "bootstrap", "dependency-check")
        if not ensure_common_dependencies():
            record_diagnostic(ROOT, "bootstrap", "pip-failed", RuntimeError("common deps"))
            emit("⚠️ 本地翻译依赖安装失败，请点击重试")
            return 1
        if not ensure_llama_cpp():
            record_diagnostic(ROOT, "bootstrap", "pip-failed", RuntimeError("llama_cpp"))
            return 1
        copy_ocr_models()
        record_diagnostic(ROOT, "bootstrap", "environment-ready")
        try:
            MARKER.parent.mkdir(parents=True, exist_ok=True)
            MARKER.write_text(
                json.dumps(
                    {
                        "backend": "poptrans-gguf",
                        "model": "tencent/Hy-MT2-1.8B-GGUF",
                        "ocr": "rapidocr-onnxruntime",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            # 标记文件写入失败不影响功能（历史故障点，降级处理）
            record_diagnostic(ROOT, "bootstrap", "marker-write-skipped", OSError("marker"))
        emit(model_hint())
        emit("本地 AI 引擎环境已准备好")
        return 0
    except Exception as exc:
        record_diagnostic(ROOT, "bootstrap", "prepare-failed", exc)
        emit("⚠️ 本地翻译引擎未就绪，请点击右侧重试")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
