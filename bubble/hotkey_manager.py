"""全局热键管理器 — 主线程 RegisterHotKey + PeekMessage 轮询。

设计说明（版本无关、不依赖 QAbstractNativeEventFilter）：
- PySide6 的 QAbstractNativeEventFilter 在不同版本间行为不一致
  （6.9.2 会调用 Python 覆盖，6.10.3 实测完全不调用），因此改用主线程
  `RegisterHotKey` + 16ms `PeekMessage(PM_NOREMOVE)` 轮询探测
  WM_HOTKEY，探测到后 PM_REMOVE 取走并触发 Signal。
- 注册与冲突检测全部发生在主线程：reconfigure() 同步返回
  {action: 是否成功}，失败原因可直接反映到界面提示。
- 表达式格式与 PopTrans 完全兼容：`<ctrl>+<alt>+q` 等。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QObject, Qt, QTimer, Signal

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

PM_NOREMOVE = 0x0000
PM_REMOVE = 0x0001

HOTKEY_ID_TRANSLATE = 1
HOTKEY_ID_SHOW = 2
HOTKEY_ID_OCR = 3

POLL_INTERVAL_MS = 16   # ≈ 60Hz 探测，热键延迟无感


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


user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = (
    ctypes.POINTER(HotkeyMessage),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
)
user32.PeekMessageW.restype = wintypes.BOOL

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


class HotkeyManager(QObject):
    """注册/注销全局快捷键（绑定到气泡窗口句柄），并提供 id → 动作映射。

    触发路径：系统把 WM_HOTKEY 投递到气泡窗口 → BubbleWindow.nativeEvent
    → 控制器查询 action_for_hotkey_id() 后执行对应动作。
    该路径不依赖 QAbstractNativeEventFilter（PySide6 6.10+ 实测不再
    调用 Python 覆盖的过滤器，版本无关的窗口级 nativeEvent 才可靠）。
    """

    registration_changed = Signal(bool)  # 本次 reconfigure 是否全部注册成功

    def __init__(self, parent: Optional[QObject] = None) -> None:
        QObject.__init__(self, parent)
        self._bindings: Dict[int, Tuple[str, int, int]] = {}  # id → (action, mods, vk)
        self._all_registered = False
        self._hwnd = 0

    # ── 注册管理 ─────────────────────────────────────────────

    def reconfigure(
        self,
        hwnd: int,
        translate_expr: str,
        show_expr: str,
        ocr_expr: str,
        ocr_enabled: bool,
    ) -> dict:
        """按新配置重注册全部快捷键（注销旧键 → 注册新键）。

        返回 {action: 是否成功}；表达式留空视为"停用该动作"，记为成功。
        """
        debug = os.environ.get("BUBBLE_HOTKEY_DEBUG") == "1"
        self._hwnd = int(hwnd or 0)
        self.unregister_all()
        candidates = [
            ("translate", HOTKEY_ID_TRANSLATE, translate_expr),
            ("show", HOTKEY_ID_SHOW, show_expr),
        ]
        if ocr_enabled:
            candidates.append(("ocr", HOTKEY_ID_OCR, ocr_expr))

        hwnd_param = wintypes.HWND(hwnd) if hwnd else None
        result: dict = {}
        for action, hotkey_id, expr in candidates:
            expr = (expr or "").strip()
            if not expr:
                result[action] = True   # 显式留空 = 停用
                continue
            parsed = parse_hotkey(expr)
            if parsed is None:
                if debug:
                    print(f"[hotkey] {action}: 表达式无法解析: {expr!r}", flush=True)
                result[action] = False
                continue
            modifiers, virtual_key = parsed
            if not user32.RegisterHotKey(
                hwnd_param, hotkey_id, modifiers | MOD_NOREPEAT, virtual_key
            ):
                lasterr = ctypes.get_last_error()
                if debug:
                    print(
                        f"[hotkey] {action}: RegisterHotKey 失败 ({expr!r}, "
                        f"mods=0x{modifiers:X} vk=0x{virtual_key:X}) lasterr={lasterr}",
                        flush=True,
                    )
                result[action] = False
                continue
            self._bindings[hotkey_id] = (action, modifiers, virtual_key)
            if debug:
                print(f"[hotkey] {action}: 已注册 {expr!r} id={hotkey_id}", flush=True)
            result[action] = True
        self._all_registered = all(result.values())
        if debug:
            print(f"[hotkey] 注册结果: {result}", flush=True)
        self.registration_changed.emit(self._all_registered)
        return result

    def unregister_all(self) -> None:
        # 注销必须使用与注册相同的 hwnd
        hwnd_param = wintypes.HWND(self._hwnd) if self._hwnd else None
        for hotkey_id in list(self._bindings):
            user32.UnregisterHotKey(hwnd_param, hotkey_id)
        self._bindings.clear()

    def action_for_hotkey_id(self, hotkey_id: int) -> Optional[str]:
        binding = self._bindings.get(hotkey_id & 0xFFFF)
        return binding[0] if binding else None

    def is_registered(self) -> bool:
        return self._all_registered
