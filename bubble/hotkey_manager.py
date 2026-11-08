"""全局快捷键管理 — 对齐 PopTrans internal/platform/windows/hotkey.go。

表达式格式与 PopTrans 完全兼容：`<ctrl>+<alt>+q`（也接受 ctrl/alt/shift/win
小写形式），主键支持 a-z / 0-9 / space / enter / tab / esc / 方向键 /
delete / F1-F24，且必须包含至少一个修饰键。

注册使用 user32.RegisterHotKey（线程级），WM_HOTKEY 通过
QAbstractNativeEventFilter 捕获（int(message) 强转，规避
shiboken6 VoidPtr → ctypes 的崩溃问题）。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QObject, Signal

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

HOTKEY_ID_TRANSLATE = 1
HOTKEY_ID_OCR = 2
HOTKEY_ID_SHOW = 3

user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.UnregisterHotKey.restype = wintypes.BOOL


class HotkeyMessage(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "cmd": MOD_WIN,
    "meta": MOD_WIN,
}

_SPECIAL_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "delete": 0x2E,
}


def parse_hotkey(expr: str) -> Optional[Tuple[int, int]]:
    """解析快捷键表达式 → (modifiers, virtual_key)；无效返回 None。

    与 PopTrans ParseHotkey 保持相同语义：必须有修饰键，必须有主键。
    """
    modifiers = 0
    main_key = ""
    for part in str(expr).lower().replace("<", "").replace(">", "").split("+"):
        part = part.strip()
        if not part:
            continue
        if part in _MODIFIERS:
            modifiers |= _MODIFIERS[part]
        else:
            main_key = part

    if modifiers == 0 or not main_key:
        return None

    if len(main_key) == 1:
        char = main_key
        if "a" <= char <= "z":
            return modifiers, ord(char.upper())
        if "0" <= char <= "9":
            return modifiers, ord(char)
        return None

    if main_key in _SPECIAL_KEYS:
        return modifiers, _SPECIAL_KEYS[main_key]

    if main_key.startswith("f"):
        tail = main_key[1:]
        if tail.isdigit():
            number = int(tail)
            if 1 <= number <= 24:
                return modifiers, 0x70 + number - 1
    return None


class HotkeyManager(QObject, QAbstractNativeEventFilter):
    """注册/注销全局快捷键并转发触发事件。

    触发信号带动作名（"translate" / "ocr"），由控制器连接对应流程。
    """

    triggered = Signal(str)
    registration_changed = Signal(bool)  # 本次 reconfigure 是否全部注册成功

    def __init__(self, parent: Optional[QObject] = None) -> None:
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._bindings: Dict[int, Tuple[str, int, int]] = {}  # id → (action, mod, vk)
        self._all_registered = False

    # ── 原生事件过滤 ─────────────────────────────────────────

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt 命名)
        if event_type == QByteArray(b"windows_generic_msg"):
            try:
                native_message = ctypes.cast(
                    int(message), ctypes.POINTER(HotkeyMessage)
                ).contents
            except (TypeError, ValueError):
                return False, 0
            if native_message.message == WM_HOTKEY:
                hotkey_id = int(native_message.wParam) & 0xFFFF
                binding = self._bindings.get(hotkey_id)
                if binding:
                    self.triggered.emit(binding[0])
                    return True, 0
        return False, 0

    # ── 注册管理 ─────────────────────────────────────────────

    def reconfigure(self, translate_expr: str, show_expr: str, ocr_expr: str, ocr_enabled: bool) -> dict:
        """按新配置重注册全部快捷键（注销旧键 → 注册新键）。

        返回 {action: 是否成功}；表达式留空视为"停用该动作"，记为成功。
        """
        self.unregister_all()
        candidates = [
            ("translate", HOTKEY_ID_TRANSLATE, translate_expr),
            ("show", HOTKEY_ID_SHOW, show_expr),
        ]
        if ocr_enabled:
            candidates.append(("ocr", HOTKEY_ID_OCR, ocr_expr))

        result: dict = {}
        for action, hotkey_id, expr in candidates:
            expr = (expr or "").strip()
            if not expr:
                result[action] = True   # 显式留空 = 停用
                continue
            parsed = parse_hotkey(expr)
            if parsed is None:
                result[action] = False
                continue
            modifiers, virtual_key = parsed
            if not user32.RegisterHotKey(
                None, hotkey_id, modifiers | MOD_NOREPEAT, virtual_key
            ):
                result[action] = False
                continue
            self._bindings[hotkey_id] = (action, modifiers, virtual_key)
            result[action] = True
        self._all_registered = all(result.values())
        self.registration_changed.emit(self._all_registered)
        return result

    def unregister_all(self) -> None:
        for hotkey_id in list(self._bindings):
            user32.UnregisterHotKey(None, hotkey_id)
        self._bindings.clear()

    def is_registered(self) -> bool:
        return self._all_registered
