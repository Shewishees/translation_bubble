"""Minimal local diagnostics without recording translation text."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def record_diagnostic(root: Path, component: str, stage: str, error: Optional[BaseException] = None) -> None:
    log_path = root / ".runtime" / "diagnostics.log"
    error_kind = type(error).__name__ if error is not None else ""
    line = f"{datetime.now(timezone.utc).isoformat()}\t{component}\t{stage}\t{error_kind}\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(line)
    except OSError:
        pass
