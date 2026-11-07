"""Show actionable errors when the app is started without a console."""

from __future__ import annotations

import ctypes
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ERROR_LOG = ROOT / "startup-error.log"


def report_startup_error(error: Exception) -> None:
    details = traceback.format_exc()
    try:
        ERROR_LOG.write_text(details, encoding="utf-8")
    except OSError:
        pass
    message = (
        f"程序启动失败：{type(error).__name__}: {error}\n\n"
        f"详细信息已保存到：\n{ERROR_LOG}"
    )
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "本地翻译启动失败",
            0x00000010 | 0x00010000,
        )
    except Exception:
        pass


def main() -> int:
    try:
        from app import main as run_app

        return run_app()
    except Exception as error:
        report_startup_error(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
