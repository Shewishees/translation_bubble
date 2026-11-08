"""settings.json 管理器 — 对齐 PopTrans internal/config/config.go 的语义。

字段名、默认值、校验回退规则与 PopTrans 保持一致；文件保存在项目根目录，
保存后自动触发热加载（PopTrans：保存即生效，无需重启）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, QFileSystemWatcher, Signal

MIN_SERVER_PORT = 1024
MAX_SERVER_PORT = 65535
DEFAULT_SERVER_PORT = 8989
DEFAULT_UI_IDLE_MINUTES = 5
DEFAULT_AI_IDLE_MINUTES = 15

DEFAULTS: Dict[str, Any] = {
    "hotkey": "<alt>+q",
    "hotkey_display": "Alt+Q",
    "show_hotkey": "<alt>+b",
    "show_hotkey_display": "Alt+B",
    "ocr_enabled": True,
    "ocr_hotkey": "<alt>+w",
    "ocr_hotkey_display": "Alt+W",
    "logging_enabled": False,
    "server_port": DEFAULT_SERVER_PORT,
    "theme": "system",
    "acceleration_device": "auto",
    "ui_idle_minutes": DEFAULT_UI_IDLE_MINUTES,
    "ai_idle_minutes": DEFAULT_AI_IDLE_MINUTES,
    "launch_at_login": False,
}

# 这些字段缺失时必须回退默认值（对应 Go 的 mergeLoadedDefaults rawKeys 判定）
_KEY_REQUIRED_DEFAULTS = ("acceleration_device", "ui_idle_minutes", "ai_idle_minutes")


def validate_server_port(port: int) -> bool:
    return MIN_SERVER_PORT <= int(port) <= MAX_SERVER_PORT


def hotkey_display_from_expression(expr: str) -> str:
    """从快捷键表达式派生展示文本（Ctrl+Alt+Q 风格）。"""
    pieces = []
    for part in str(expr).lower().replace("<", "").replace(">", "").split("+"):
        part = part.strip()
        if not part:
            continue
        if part in ("ctrl", "control"):
            pieces.append("Ctrl")
        elif part == "alt":
            pieces.append("Alt")
        elif part == "shift":
            pieces.append("Shift")
        elif part in ("win", "cmd", "meta"):
            pieces.append("Win")
        else:
            pieces.append(part.upper() if len(part) == 1 else part.capitalize())
    return "+".join(pieces)


class SettingsManager(QObject):
    """settings.json 读写 + 文件热加载。

    changed 信号携带"被修改字段 → 新值"，供控制器决定
    （快捷键重注册 / 引擎重启 / 自启同步）。
    """

    changed = Signal(dict)
    reload_failed = Signal(str)

    def __init__(self, root: Path, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.root = root
        self.path = root / "settings.json"
        self._lock = threading.Lock()
        self._suppress_reload = False
        self._data: Dict[str, Any] = dict(DEFAULTS)
        self._previous: Dict[str, Any] = dict(DEFAULTS)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_directory_changed)

    # ── 加载 ──────────────────────────────────────────────

    def load(self) -> Dict[str, Any]:
        with self._lock:
            loaded = self._read_sync()
            if loaded is None:
                self._data = dict(DEFAULTS)
            else:
                self._data = loaded
            self._previous = dict(self._data)
        self._start_watch()
        return dict(self._data)

    def _read_sync(self) -> Optional[Dict[str, Any]]:
        """读取失败（文件缺失/损坏）返回 None，由调用方区分处理。"""
        data = dict(DEFAULTS)
        try:
            raw = self.path.read_text(encoding="utf-8")
            loaded = json.loads(raw) if raw.strip() else {}
            if isinstance(loaded, dict):
                raw_keys = loaded.keys()
                data.update({k: v for k, v in loaded.items()})
                self._apply_validation(data, raw_keys)
        except (OSError, ValueError, TypeError):
            return None
        return data

    @staticmethod
    def _apply_validation(data: Dict[str, Any], raw_keys) -> None:
        # 对齐 config.go mergeDefaults / mergeLoadedDefaults 的回退规则。
        # 快捷键允许显式留空（= 停用该动作），仅当字段"缺失"时回退默认值。
        if "hotkey" not in raw_keys:
            data["hotkey"] = DEFAULTS["hotkey"]
        if "hotkey_display" not in raw_keys or not data.get("hotkey_display"):
            data["hotkey_display"] = (
                hotkey_display_from_expression(data.get("hotkey") or "")
                or "未设置"
            )
        if "show_hotkey" not in raw_keys:
            data["show_hotkey"] = DEFAULTS["show_hotkey"]
        if "show_hotkey_display" not in raw_keys or not data.get("show_hotkey_display"):
            data["show_hotkey_display"] = (
                hotkey_display_from_expression(data.get("show_hotkey") or "")
                or "未设置"
            )
        if "ocr_hotkey" not in raw_keys:
            data["ocr_hotkey"] = DEFAULTS["ocr_hotkey"]
        if "ocr_hotkey_display" not in raw_keys or not data.get("ocr_hotkey_display"):
            data["ocr_hotkey_display"] = (
                hotkey_display_from_expression(data.get("ocr_hotkey") or "")
                or "未设置"
            )
        if not data.get("server_port"):
            data["server_port"] = DEFAULT_SERVER_PORT
        if data.get("theme") not in ("light", "dark", "system"):
            data["theme"] = DEFAULTS["theme"]
        if data.get("acceleration_device") not in ("auto", "gpu", "cpu"):
            data["acceleration_device"] = DEFAULTS["acceleration_device"]
        for key in ("ui_idle_minutes", "ai_idle_minutes"):
            try:
                value = int(data.get(key))
            except (TypeError, ValueError):
                value = -1
            if value < 0:
                data[key] = DEFAULTS[key]
        if not validate_server_port(data.get("server_port", 0)):
            data["server_port"] = DEFAULT_SERVER_PORT
        for key in _KEY_REQUIRED_DEFAULTS:
            if key not in raw_keys:
                data[key] = DEFAULTS[key]
        data["launch_at_login"] = bool(data.get("launch_at_login", False))
        data["ocr_enabled"] = bool(data.get("ocr_enabled", False))
        data["logging_enabled"] = bool(data.get("logging_enabled", False))

    # ── 保存 ──────────────────────────────────────────────

    def save(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """合并更新并写盘；返回保存后的完整配置。"""
        with self._lock:
            data = dict(self._data)
            data.update(updates)
            self._apply_validation(data, data.keys())
            payload = {key: data[key] for key in DEFAULTS}
            self._suppress_reload = True
            try:
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
            except OSError:
                self._suppress_reload = False
                raise
            self._data = data
            self._previous = dict(data)
            snapshot = dict(data)
        self._start_watch()
        return snapshot

    # ── 访问 ──────────────────────────────────────────────

    def get(self, key: str, fallback: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, fallback))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def server_port(self) -> int:
        return int(self.get("server_port", DEFAULT_SERVER_PORT))

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port()}"

    # ── 热加载 ─────────────────────────────────────────────

    def _start_watch(self) -> None:
        """挂载文件与目录监听。

        目录监听用于捕捉 settings.json 的首次创建（文件不存在时
        QFileSystemWatcher 无法跟踪文件本身）。
        """
        if str(self.path) not in self._watcher.files() and self.path.exists():
            self._watcher.addPath(str(self.path))
        if str(self.root) not in self._watcher.directories():
            self._watcher.addPath(str(self.root))

    def _reload_and_diff(self) -> None:
        previous = self._previous
        with self._lock:
            loaded = self._read_sync()
            if loaded is None:
                # 文件被编辑器原子替换的瞬间可能暂不可读：跳过本轮，
                # 避免误发全量 diff 触发引擎无谓重启
                return
            self._data = loaded
        current = self.snapshot()
        self._previous = current
        self._start_watch()
        diff = {
            key: current[key]
            for key in DEFAULTS
            if previous.get(key) != current.get(key)
        }
        if diff:
            self.changed.emit(diff)

    def _on_directory_changed(self, _path: str) -> None:
        if self._suppress_reload:
            return
        if not self.path.exists():
            return
        if str(self.path) not in self._watcher.files():
            # settings.json 是新建的：挂上文件监听并加载
            self._reload_and_diff()

    def _on_file_changed(self, _path: str) -> None:
        if self._suppress_reload:
            self._suppress_reload = False
            self._start_watch()
            return
        # QFileSystemWatcher 触发后会丢失对文件的跟踪，重新挂上
        self._reload_and_diff()
