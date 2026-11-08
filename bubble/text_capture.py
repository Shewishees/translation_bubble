"""划词取词 — 对齐 PopTrans「模拟 Ctrl+C + 多次智能重试」的选中文本捕获。

纯 ctypes 实现，不依赖 Qt，可在任意线程调用：
1. 保存剪贴板现有文本；
2. 向前台窗口发送 Ctrl+C（SendInput）；
3. 轮询读取剪贴板，文本发生变化即认为捕获成功；
4. 恢复原剪贴板内容。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_C = 0x43

MAX_ATTEMPTS = 3


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # union 必须以最大成员 MOUSEINPUT 对齐（x64 上 INPUT 总长 40 字节），
    # 否则 SendInput 以 ERROR_INVALID_PARAMETER (87) 拒绝注入，按键模拟失效
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUTUNION),
    ]


# SendInput / 剪贴板 API 签名
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = ()
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = ()
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalUnlock.restype = wintypes.BOOL


def _send_ctrl_c() -> None:
    def key_event(vk: int, flags: int) -> INPUT:
        item = INPUT()
        item.type = INPUT_KEYBOARD
        item.union.ki = KEYBDINPUT(
            wVk=vk,
            wScan=0,
            dwFlags=flags,
            time=0,
            dwExtraInfo=None,
        )
        return item

    sequence = [
        key_event(VK_CONTROL, 0),
        key_event(VK_C, 0),
        key_event(VK_C, KEYEVENTF_KEYUP),
        key_event(VK_CONTROL, KEYEVENTF_KEYUP),
    ]
    array = (INPUT * len(sequence))(*sequence)
    return user32.SendInput(len(sequence), array, ctypes.sizeof(INPUT)) == len(sequence)


def _open_clipboard(retries: int = 6) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


def _read_clipboard_text() -> Optional[str]:
    """返回剪贴板文本；空剪贴板返回空串，访问失败返回 None。"""
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _write_clipboard_text(text: str) -> None:
    if not _open_clipboard():
        return
    try:
        user32.EmptyClipboard()
        if not text:
            return
        size = (len(text) + 1) * ctypes.sizeof(wintypes.WCHAR)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return
        try:
            ctypes.memmove(
                pointer,
                ctypes.create_unicode_buffer(text).raw,
                size,
            )
        finally:
            kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)  # 系统接管内存所有权
    finally:
        user32.CloseClipboard()


def capture_selected_text(max_attempts: int = MAX_ATTEMPTS) -> Optional[str]:
    """捕获当前选中的文本；未能捕获时返回 None（不会返回旧剪贴板内容）。

    兼容 PopTrans 行为：若剪贴板文本始终未变化（应用不支持 Ctrl+C 复制、
    无选中内容等），视为捕获失败并恢复原剪贴板。
    """
    if ctypes.sizeof(INPUT) == 0 or not hasattr(user32, "SendInput"):
        return None

    original: Optional[str] = None
    for attempt in range(max_attempts):
        time.sleep(0.03 if attempt else 0.0)
        current = _read_clipboard_text()
        if current is not None:
            original = current if original is None else original
            break
    if original is None:
        original = ""

    try:
        for attempt in range(max_attempts):
            _send_ctrl_c()
            # 首次稍短，后续放宽（对齐"智能重试"的退避节奏）
            time.sleep(0.08 + 0.05 * attempt)
            text = _read_clipboard_text()
            if text and text != original:
                return text.strip() or None
        return None
    finally:
        _write_clipboard_text(original or "")
