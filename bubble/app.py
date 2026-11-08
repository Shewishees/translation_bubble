"""Native Windows floating translator powered by the PopTrans local AI engine.

双语对话气泡 + macOS 浅色翻译面板（PySide6 原生），翻译能力对齐 PopTrans：
- 本地 GGUF 推理引擎（backend/：FastAPI + llama-cpp-python + RapidOCR）以独立
  子进程运行，由 engine_supervisor 守护（健康轮询 / 空闲休眠 / 崩溃重启）；
- 划词快捷翻译（模拟 Ctrl+C）+ 截图 OCR 翻译，气泡平滑飞至光标附近；
- settings.json 与 PopTrans 字段规范一致，保存即热加载。
"""

from __future__ import annotations

import ctypes
import io
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import winreg
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import (
    QBuffer,
    QIODevice,
    QEasingCurve,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QPropertyAnimation,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QTextBlockFormat,
    QTextCursor,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .diagnostics import record_diagnostic
from .engine_supervisor import EngineSupervisor
from .hotkey_manager import HotkeyManager
from .screen_capture import ScreenCaptureOverlay
from .settings_manager import SettingsManager, hotkey_display_from_expression
from .text_capture import capture_selected_text


if getattr(sys, "frozen", False):
    # PyInstaller 打包态：ROOT = exe 所在目录（settings/models/ai_engine.exe 同级），
    # ASSET_DIR = 打包资源目录（svg 图标等 datas，spec 收集到 _MEIPASS/assets）
    ROOT = Path(sys.executable).resolve().parent
    ASSET_DIR = Path(getattr(sys, "_MEIPASS", ROOT)) / "assets"
else:
    ROOT = Path(__file__).resolve().parent.parent
    ASSET_DIR = ROOT / "assets"
BOOTSTRAP_MODULE = "bubble.bootstrap_backend"
MUTEX_NAME = "LocalTranslateBubble.Native.SingleInstance"
ACTIVATE_MESSAGE = 0x8000 + 0x51
FRIENDLY_ERROR = "⚠️ 本地翻译引擎未就绪，请点击右侧重试"
IDLE_STATUS = "引擎空闲休眠中，翻译时自动唤醒"

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "LocalTranslateBubble"

DIRECTION_STATES = ("auto", "zh-en", "en-zh")


def _text_looks_chinese(text: str) -> bool:
    """简化的中英文判别（与引擎 _is_chinese 的 30% 规则一致），用于自动档方向提示。"""
    body = (text or "").replace(" ", "").replace("\n", "").replace("\t", "")
    if not body:
        return False
    cjk = sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf")
    return cjk / len(body) > 0.3


class WindowsMessage(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


class StatusRelay(QObject):
    message = Signal(str)


def render_svg(path: Path, size: QSize) -> QPixmap:
    pixmap = QPixmap(size)
    pixmap.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(str(path))
    painter = QPainter(pixmap)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(painter)
    painter.end()
    return pixmap


def get_cursor_pos() -> QPoint:
    point = wintypes.POINT()
    ctypes.WinDLL("user32").GetCursorPos(ctypes.byref(point))
    return QPoint(point.x, point.y)


# ── 开机自启（对齐 PopTrans launch_at_login：HKCU Run 注册表键） ──────────

def _legacy_shortcut_path() -> Path:
    startup = Path(os.environ.get("APPDATA", ROOT)) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return startup / "LocalTranslateBubble.lnk"


def cleanup_legacy_autostart() -> None:
    """迁移清理：删除旧版 Startup 目录 .lnk 自启方案。"""
    try:
        _legacy_shortcut_path().unlink()
    except OSError:
        pass


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    if not enabled:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            pass
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else Path(sys.executable)
    command = f'"{target}" "{ROOT / "launcher.py"}"'
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)


def acquire_single_instance():
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        return False
    return handle


def activate_running_instance() -> bool:
    if os.name != "nt":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.PostMessageW.restype = wintypes.BOOL
    window = user32.FindWindowW(None, "本地翻译悬浮球")
    if not window:
        return False
    return bool(user32.PostMessageW(window, ACTIVATE_MESSAGE, 0, 0))


def _proxy_free_opener() -> urllib.request.OpenerDirector:
    """直连回环地址，避免系统代理拦截 127.0.0.1 请求。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _friendly_engine_status(raw: str) -> str:
    """把引擎 translator_status 映射为界面友好文案（不暴露底层细节）。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("翻译引擎初始化失败"):
        return FRIENDLY_ERROR
    if text.startswith("首次使用需下载"):
        return "首次使用，正在下载 Hy-MT2 翻译模型（约 1.13GB）…"
    if text.startswith("正在从") or text.startswith("正在下载模型"):
        return text.replace("...", "…")
    if text.startswith("下载中断") or text.startswith("连接官网失败"):
        return f"模型下载重试中…（{text.split('：')[-1].strip()}）"
    if text.startswith("正在加载翻译模型"):
        return "正在唤醒本地 GGUF 模型…"
    if text.startswith("翻译引擎就绪 (GPU 加速"):
        return "GPU 加速已就绪"
    if text.startswith("翻译引擎就绪 (CPU 降级"):
        return "GPU 加载失败，已自动切换 CPU 模式"
    if text.startswith("翻译引擎就绪 (CPU"):
        return "CPU 模式已就绪"
    if text.startswith("模型下载完成"):
        return "模型下载完成，正在唤醒本地 GGUF 模型…"
    return text.replace("...", "…")


class BootstrapThread(QThread):
    message = Signal(str)
    succeeded = Signal()
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.process: Optional[subprocess.Popen[str]] = None

    def run(self) -> None:
        try:
            python = Path(sys.executable)
            if python.name.lower() == "pythonw.exe":
                python = python.with_name("python.exe")
            command = [str(python), "-m", BOOTSTRAP_MODULE]
            self.process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert self.process.stdout
            with self.process.stdout as output:
                for raw in output:
                    message = raw.strip()
                    if message:
                        self.message.emit(message)
            code = self.process.wait()
            if code != 0:
                self.failed.emit(FRIENDLY_ERROR)
                return
            self.succeeded.emit()
        except Exception as exc:
            record_diagnostic(ROOT, "bootstrap-thread", "process-failed", exc)
            if not self.isInterruptionRequested():
                self.failed.emit(FRIENDLY_ERROR)

    def stop(self) -> None:
        self.requestInterruption()
        if self.process and self.process.poll() is None:
            try:
                self.process.kill()
            except OSError:
                pass


class TranslationStreamThread(QThread):
    """OpenAI 兼容 SSE 流式翻译（/v1/chat/completions，stream=true）。"""

    chunk = Signal(int, str)
    succeeded = Signal(int, str)
    failed = Signal(int, str, bool)  # (request_id, friendly_message, engine_offline)

    def __init__(
        self,
        base_url_provider: Callable[[], str],
        text: str,
        target_lang: Optional[str],
        request_id: int,
    ) -> None:
        super().__init__()
        self.base_url_provider = base_url_provider
        self.text = text
        self.target_lang = target_lang
        self.request_id = request_id
        self._response = None

    def run(self) -> None:
        payload = {
            "messages": [{"role": "user", "content": self.text}],
            "stream": True,
        }
        if self.target_lang:
            payload["target_lang"] = self.target_lang
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url_provider().rstrip("/") + "/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        accumulated: list[str] = []
        try:
            opener = _proxy_free_opener()
            self._response = opener.open(request, timeout=300)
            with self._response as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    self.failed.emit(self.request_id, FRIENDLY_ERROR, False)
                    return
                for raw_line in response:
                    if self.isInterruptionRequested():
                        return
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                        delta = event["choices"][0].get("delta", {}).get("content")
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
                    if delta:
                        accumulated.append(delta)
                        self.chunk.emit(self.request_id, delta)
            full_text = "".join(accumulated).strip()
            if full_text:
                self.succeeded.emit(self.request_id, full_text)
            else:
                self.failed.emit(self.request_id, "翻译返回空结果，请重试", False)
        except urllib.error.HTTPError as exc:
            if exc.code == 503:
                self.failed.emit(self.request_id, "模型正在唤醒，请稍候…", True)
            else:
                self.failed.emit(self.request_id, FRIENDLY_ERROR, False)
        except (urllib.error.URLError, OSError):
            self.failed.emit(self.request_id, "引擎未就绪，正在唤醒…", True)
        except Exception as exc:
            record_diagnostic(ROOT, "translation-stream", "failed", exc)
            if not self.isInterruptionRequested():
                self.failed.emit(self.request_id, FRIENDLY_ERROR, False)

    def stop(self) -> None:
        self.requestInterruption()
        response = self._response
        if response is not None:
            try:
                response.close()
            except OSError:
                pass


def _multipart_body(png_bytes: bytes, target_lang: Optional[str]) -> tuple[bytes, str]:
    boundary = "----LocalTranslateBubbleBoundary7d1a2c"
    parts = io.BytesIO()
    parts.write(f"--{boundary}\r\n".encode("ascii"))
    parts.write(
        b'Content-Disposition: form-data; name="file"; filename="screenshot.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
    )
    parts.write(png_bytes)
    parts.write(b"\r\n")
    if target_lang:
        parts.write(f"--{boundary}\r\n".encode("ascii"))
        parts.write(b'Content-Disposition: form-data; name="target_lang"\r\n\r\n')
        parts.write(target_lang.encode("utf-8"))
        parts.write(b"\r\n")
    parts.write(f"--{boundary}--\r\n".encode("ascii"))
    return parts.getvalue(), f"multipart/form-data; boundary={boundary}"


class OcrTranslateThread(QThread):
    """截图 OCR + 翻译（/api/v1/ocr_translate）。"""

    succeeded = Signal(str, str)  # source_text, translation
    failed = Signal(str, bool)    # friendly_message, engine_offline

    def __init__(
        self,
        base_url_provider: Callable[[], str],
        png_bytes: bytes,
        target_lang: Optional[str],
    ) -> None:
        super().__init__()
        self.base_url_provider = base_url_provider
        self.png_bytes = png_bytes
        self.target_lang = target_lang

    def run(self) -> None:
        body, content_type = _multipart_body(self.png_bytes, self.target_lang)
        request = urllib.request.Request(
            self.base_url_provider().rstrip("/") + "/api/v1/ocr_translate",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with _proxy_free_opener().open(request, timeout=300) as response:
                result = json.loads(response.read().decode("utf-8", "replace"))
            self.succeeded.emit(
                str(result.get("source_text") or ""),
                str(result.get("translation") or ""),
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 503:
                self.failed.emit("模型正在唤醒，请稍后再试", True)
            elif exc.code == 400:
                self.failed.emit("截图格式无法识别，请重试", False)
            else:
                self.failed.emit("OCR 识别失败，请重试", False)
        except (urllib.error.URLError, OSError):
            self.failed.emit("引擎未就绪，正在唤醒…", True)
        except Exception as exc:
            record_diagnostic(ROOT, "ocr-translate", "failed", exc)
            self.failed.emit("OCR 识别失败，请重试", False)


class CaptureWorker(QThread):
    """在后台线程执行模拟 Ctrl+C 取词，避免阻塞气泡动画。"""

    captured = Signal(object)  # Optional[str]

    def run(self) -> None:
        try:
            self.captured.emit(capture_selected_text())
        except Exception as exc:
            record_diagnostic(ROOT, "text-capture", "failed", exc)
            self.captured.emit(None)


class DragHandle(QFrame):
    moved = Signal(QPoint)
    released = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._last_global: Optional[QPoint] = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._last_global = event.globalPosition().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._last_global is not None and event.buttons() & Qt.MouseButton.LeftButton:
            current = event.globalPosition().toPoint()
            self.moved.emit(current - self._last_global)
            self._last_global = current
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._last_global = None
            self.released.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class PreciseCheckBox(QCheckBox):
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        indicator = QRect(0, (self.height() - 16) // 2, 16, 16)
        checked = self.isChecked()
        painter.setPen(QPen(QColor("#333336" if checked else "#D2D2D7"), 1))
        painter.setBrush(QColor("#333336" if checked else "#FFFFFF"))
        painter.drawRoundedRect(indicator.adjusted(0, 0, -1, -1), 4, 4)
        if checked:
            mark = QPainterPath()
            mark.moveTo(indicator.left() + 3.5, indicator.center().y())
            mark.lineTo(indicator.left() + 6.5, indicator.center().y() + 3)
            mark.lineTo(indicator.right() - 3, indicator.center().y() - 3.5)
            painter.setPen(QPen(QColor("#FFFFFF"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(mark)
        painter.setPen(self.palette().color(self.foregroundRole()))
        label = QRect(23, 0, max(0, self.width() - 23), self.height())
        painter.drawText(label, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())

    def sizeHint(self) -> QSize:
        return QSize(23 + self.fontMetrics().horizontalAdvance(self.text()), max(16, self.fontMetrics().height()))


class SourceEdit(QTextEdit):
    """源文输入框；Ctrl+Enter 立即提交翻译（对齐 PopTrans 工作台快捷键）。"""

    submit_requested = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.submit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class BubbleWindow(QWidget):
    """翻译对话悬浮气泡 — 亚像素浮动渲染管线（60~120 FPS 丝滑）。

    性能设计：
    - 非拖拽状态窗口几何保持静止，上下浮动与呼吸缩放完全通过 paintEvent
      内的浮点平移/缩放实现（QPointF/QRectF 亚像素插值），绝不 window.move()；
    - 窗口比气泡视觉尺寸每侧多留 12px（共 24px）透明边距，容纳阴影与
      浮动位移空间；
    - Logo、半透明毛玻璃圆底、柔和径向渐变光晕（代替
      QGraphicsDropShadowEffect）在初始化时按屏幕 DPI 一次性预渲染为
      缓存 QPixmap；paintEvent 仅做 drawPixmap 贴图与变换；
    - 8ms（≈120 FPS）PreciseTimer 驱动 + time.perf_counter() 真实时间戳
      正弦相位，按真实时间平滑插值，偶发主线程微卡也不掉帧；
    - 拖拽期间暂停浮动动画并冻结当前相位，鼠标 1:1 绝对跟手。
    """

    clicked = Signal()
    activate_requested = Signal()
    moved = Signal(QPoint)
    quit_requested = Signal()
    open_requested = Signal()
    capture_requested = Signal()
    ocr_requested = Signal()
    settings_requested = Signal()

    BUBBLE_TIP = "本地翻译：点击输入 · Alt+Q 划词 · Alt+W 截图 · Alt+B 唤起面板"

    MARGIN = 12                    # 每侧透明边距（容纳阴影 + 浮动位移空间）
    VISUAL_SIZE = 44               # 气泡视觉盒边长（原 88 的一半）
    LOGO_SIZE = 33                 # 方形图标边长（原 66 的一半）
    DISC_PADDING = 3.5             # 图标到圆底边缘的留白（原 7 的一半）
    FLOAT_AMPLITUDE = 2.6          # 上下浮动幅度（浮点 px）
    FLOAT_SPEED = 1.75             # 浮动角速度（rad/s，≈3.6s 周期）
    GLOW_SPEED = 2.3               # 光晕呼吸角速度（rad/s）
    TICK_MS = 8                    # ≈120 FPS 精确定时器
    SCALE_EASE = 14.0              # 缩放/歪头的帧率无关指数趋近速率

    def __init__(self, logo_path: Path) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setWindowTitle("本地翻译悬浮球")
        self.setToolTip(self.BUBBLE_TIP)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self.VISUAL_SIZE + 2 * self.MARGIN, self.VISUAL_SIZE + 2 * self.MARGIN)
        self.setMouseTracking(True)
        self.logo_path = logo_path
        self.hovered = False
        self.pressed = False
        self.visual_scale = 1.0
        self._scale_target = 1.0
        self._hover_intensity = 0.0
        self._hover_target = 0.0
        self._float_offset_y = 0.0
        self._glow_alpha = 0.0
        self._anim_time = 0.0
        self._last_ts = time.perf_counter()
        self._dragging = False
        self._last_global: Optional[QPoint] = None
        self._drag_distance = 0
        self._build_caches()
        self._anim_timer = QTimer(self)
        self._anim_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim_timer.setInterval(self.TICK_MS)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start()

    # ── 预渲染缓存（初始化一次性完成） ──────────────────────

    def _screen_dpr(self) -> float:
        screen = QApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen is not None else 1.0
        return max(float(dpr), 1.0)

    def _build_caches(self) -> None:
        """按屏幕 DPI 预渲染：径向渐变光晕 + 毛玻璃圆底 + 翻译对话图标。"""
        dpr = self._screen_dpr()
        self._cache_dpr = dpr
        disc_radius = self.LOGO_SIZE / 2 + self.DISC_PADDING
        base_size = int(disc_radius * 2 + 6)

        # ① 呼吸光晕：柔和径向渐变阴影（代替 QGraphicsDropShadowEffect）
        halo_size = int(base_size * 1.4)
        halo = QPixmap(halo_size, halo_size)
        halo.fill(Qt.GlobalColor.transparent)
        painter = QPainter(halo)
        gradient = QRadialGradient(halo_size / 2, halo_size / 2, halo_size / 2)
        gradient.setColorAt(0.0, QColor(70, 70, 82, 88))
        gradient.setColorAt(0.55, QColor(70, 70, 82, 30))
        gradient.setColorAt(1.0, QColor(70, 70, 82, 0))
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(0, 0, halo_size, halo_size))
        painter.end()

        # ② 毛玻璃圆底 + 复古 Logo 合成为一张高清缓存图（×DPR）
        canvas = QPixmap(int(base_size * dpr), int(base_size * dpr))
        canvas.setDevicePixelRatio(dpr)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        center = base_size / 2
        disc = QRadialGradient(center, center - disc_radius * 0.3, disc_radius)
        disc.setColorAt(0.0, QColor(255, 255, 255, 218))
        disc.setColorAt(0.72, QColor(255, 255, 255, 158))
        disc.setColorAt(1.0, QColor(255, 255, 255, 108))
        painter.setBrush(QBrush(disc))
        painter.setPen(QPen(QColor(255, 255, 255, 130), 1))
        painter.drawEllipse(
            QRectF(center - disc_radius, center - disc_radius, disc_radius * 2, disc_radius * 2)
        )
        logo = render_svg(self.logo_path, QSize(int(self.LOGO_SIZE * dpr), int(self.LOGO_SIZE * dpr)))
        logo.setDevicePixelRatio(dpr)
        painter.drawPixmap(
            QPointF(center - self.LOGO_SIZE / 2, center - self.LOGO_SIZE / 2),
            logo,
        )
        painter.end()

        self._base_pix = canvas
        self._halo_pix = halo
        self._base_rect = QRectF(
            (self.width() - base_size) / 2,
            (self.height() - base_size) / 2,
            base_size,
            base_size,
        )
        self._halo_rect = QRectF(
            (self.width() - halo_size) / 2,
            (self.height() - halo_size) / 2,
            halo_size,
            halo_size,
        )

    # ── 动画驱动（真实时间正弦相位，帧率无关） ──────────────

    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(now - self._last_ts, 0.1)   # 偶发长卡时钳制单帧步长
        self._last_ts = now
        self._anim_time += dt
        self._float_offset_y = self.FLOAT_AMPLITUDE * math.sin(self._anim_time * self.FLOAT_SPEED)
        self._glow_alpha = 0.5 + 0.5 * math.sin(self._anim_time * self.GLOW_SPEED + 1.1)
        ease = 1.0 - math.exp(-dt * self.SCALE_EASE)
        self.visual_scale += (self._scale_target - self.visual_scale) * ease
        self._hover_intensity += (self._hover_target - self._hover_intensity) * ease
        self.update()

    def _pause_animation(self) -> None:
        """拖拽期间冻结浮动相位（保留当前偏移，松手无缝续播）。"""
        if self._anim_timer.isActive():
            self._anim_timer.stop()

    def _resume_animation(self) -> None:
        if not self._anim_timer.isActive():
            self._last_ts = time.perf_counter()
            self._anim_timer.start()

    def pulse(self) -> None:
        """划词捕获成功后的呼吸脉冲提示（时间驱动平滑趋近）。"""
        self._scale_target = 1.14
        QTimer.singleShot(320, self._restore_scale_target)

    def _restore_scale_target(self) -> None:
        self._scale_target = 1.08 if self.hovered else 1.0

    # ── 渲染（每帧仅贴图与变换） ────────────────────────────

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        center_x = self.width() / 2.0
        center_y = self.height() / 2.0

        # 光晕呼吸：透明度随正弦相位变化
        painter.save()
        painter.setOpacity(0.42 + 0.40 * self._glow_alpha)
        painter.drawPixmap(self._halo_rect, self._halo_pix, QRectF(self._halo_pix.rect()))
        painter.restore()

        # 气泡主体：浮点平移（亚像素浮动）+ 呼吸缩放 + 悬停歪头
        painter.save()
        painter.translate(center_x, center_y + self._float_offset_y)
        intensity = self._hover_intensity
        if intensity > 0.002:
            painter.shear(-0.025 * intensity, 0)
            painter.rotate(-0.8 * intensity)
        scale = self.visual_scale
        painter.scale(scale, scale)
        painter.translate(-center_x, -center_y)
        painter.setOpacity(1.0)
        painter.drawPixmap(self._base_rect, self._base_pix, QRectF(self._base_pix.rect()))
        painter.restore()
        painter.end()

    def showEvent(self, event) -> None:
        # 跨屏拖动导致 DPI 变化时重建预渲染缓存
        if abs(self._screen_dpr() - getattr(self, "_cache_dpr", 0.0)) > 1e-6:
            self._build_caches()
        super().showEvent(event)

    def nativeEvent(self, event_type, message):
        # shiboken6 的 VoidPtr 不能直接被 ctypes 接受，必须先转 int
        try:
            native_message = ctypes.cast(int(message), ctypes.POINTER(WindowsMessage)).contents
        except (TypeError, ValueError):
            return super().nativeEvent(event_type, message)
        if native_message.message == ACTIVATE_MESSAGE:
            self.activate_requested.emit()
            return True, 0
        return super().nativeEvent(event_type, message)

    def enterEvent(self, event) -> None:
        self.hovered = True
        self._scale_target = 1.08
        self._hover_target = 1.0
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.hovered = False
        self._hover_target = 0.0
        if not self.pressed:
            self._scale_target = 1.0
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            menu.setStyleSheet(
                "QMenu { background: #FFFFFF; border: 1px solid #E5E5EA; border-radius: 10px; padding: 6px; }"
                "QMenu::item { padding: 6px 22px; border-radius: 6px; color: #1D1D1F; font-size: 12px; }"
                "QMenu::item:selected { background: #F0F0F2; }"
            )
            open_action = menu.addAction("打开翻译面板")
            capture_action = menu.addAction("划词翻译")
            ocr_action = menu.addAction("截图翻译")
            settings_action = menu.addAction("偏好设置")
            menu.addSeparator()
            quit_action = menu.addAction("退出翻译气泡")
            chosen = menu.exec(event.globalPosition().toPoint())
            if chosen is open_action:
                self.open_requested.emit()
            elif chosen is capture_action:
                self.capture_requested.emit()
            elif chosen is ocr_action:
                self.ocr_requested.emit()
            elif chosen is settings_action:
                self.settings_requested.emit()
            elif chosen is quit_action:
                self.quit_requested.emit()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.pressed = True
            self._dragging = True
            self._scale_target = 0.94
            self._pause_animation()
            self._last_global = event.globalPosition().toPoint()
            self._drag_distance = 0
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._last_global is not None and event.buttons() & Qt.MouseButton.LeftButton:
            current = event.globalPosition().toPoint()
            delta = current - self._last_global
            self._drag_distance += abs(delta.x()) + abs(delta.y())
            self.moved.emit(delta)
            self._last_global = current
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            was_click = self._drag_distance < 7
            self._last_global = None
            self.pressed = False
            self._dragging = False
            self._scale_target = 1.08 if self.hovered else 1.0
            self._resume_animation()
            self.update()
            if was_click:
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


_MODIFIER_KEYS = {
    Qt.Key.Key_Control,
    Qt.Key.Key_Alt,
    Qt.Key.Key_Shift,
    Qt.Key.Key_Meta,
}


class HotkeyRecorder(QPushButton):
    """快捷键录制胶囊。

    交互（对齐需求）：点击胶囊进入录制态（全局热键由控制器临时注销）；
    按住组合键实时预览，松开非修饰键即完成绑定；Esc 取消；
    Backspace 清空快捷键（= 停用该动作）。纯主键（无修饰键）不予绑定。
    """

    recording_started = Signal()
    recording_stopped = Signal()
    hotkey_recorded = Signal(str)   # "<mods>+key" 表达式；"" = 清空停用

    def __init__(self, action: str, parent: Optional[QWidget] = None) -> None:
        super().__init__("未设置", parent)
        self.action = action
        self._recording = False
        self._mods = 0
        self._main_key = 0
        self.setFixedWidth(150)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("hotkeyPill")

    def set_display(self, text: str) -> None:
        if not self._recording:
            self.setText(text or "未设置")

    def mousePressEvent(self, event) -> None:
        if not self._recording and event.button() == Qt.MouseButton.LeftButton:
            self.start_recording()
            event.accept()
            return
        super().mousePressEvent(event)

    def start_recording(self) -> None:
        self._recording = True
        self._mods = 0
        self._main_key = 0
        self.setText("请按下快捷键…")
        self.setProperty("recording", True)
        self._repolish()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.recording_started.emit()

    def stop_recording(self) -> None:
        was_recording = self._recording
        self._recording = False
        self._mods = 0
        self._main_key = 0
        self.setProperty("recording", False)
        self._repolish()
        if was_recording:
            self.recording_stopped.emit()

    def cancel_recording(self) -> None:
        self.stop_recording()

    @property
    def recording(self) -> bool:
        return self._recording

    def _repolish(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)

    def keyPressEvent(self, event) -> None:
        if not self._recording:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.stop_recording()
            event.accept()
            return
        if key == Qt.Key.Key_Backspace:
            self.stop_recording()
            self.setText("未设置")
            self.hotkey_recorded.emit("")
            event.accept()
            return
        if key in _MODIFIER_KEYS:
            self._mods = int(event.modifiers())
            self._refresh_preview()
            event.accept()
            return
        self._mods = int(event.modifiers())
        self._main_key = key
        self._refresh_preview()
        event.accept()

    def keyReleaseEvent(self, event) -> None:
        if self._recording and self._main_key and event.key() == self._main_key:
            self._main_key = 0
            has_mods = self._mods != 0
            expr = self._build_expression()
            if expr is None:
                self.setText("不支持该按键，请换一个…")
                event.accept()
                return
            if not has_mods:
                self.setText("请至少搭配 Ctrl / Alt / Shift…")
                event.accept()
                return
            self.stop_recording()
            self.setText(hotkey_display_from_expression(expr))
            self.hotkey_recorded.emit(expr)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _refresh_preview(self) -> None:
        parts = self._modifier_names()
        main = QKeySequence(self._main_key).toString() if self._main_key else ""
        if main:
            self.setText(" + ".join(parts + [main]))
        else:
            prefix = " + ".join(parts)
            self.setText(f"{prefix} + …" if prefix else "请按下快捷键…")

    def _modifier_names(self) -> list:
        mods = self._mods
        parts = []
        if mods & int(Qt.KeyboardModifier.ControlModifier):
            parts.append("Ctrl")
        if mods & int(Qt.KeyboardModifier.AltModifier):
            parts.append("Alt")
        if mods & int(Qt.KeyboardModifier.ShiftModifier):
            parts.append("Shift")
        if mods & int(Qt.KeyboardModifier.MetaModifier):
            parts.append("Win")
        return parts

    def _build_expression(self) -> Optional[str]:
        main = QKeySequence(self._main_key).toString().strip().lower()
        if not main:
            return None
        pieces = []
        mods = self._mods
        if mods & int(Qt.KeyboardModifier.ControlModifier):
            pieces.append("<ctrl>")
        if mods & int(Qt.KeyboardModifier.AltModifier):
            pieces.append("<alt>")
        if mods & int(Qt.KeyboardModifier.ShiftModifier):
            pieces.append("<shift>")
        if mods & int(Qt.KeyboardModifier.MetaModifier):
            pieces.append("<win>")
        if not pieces:
            return ""
        return "+".join(pieces + [main])


class PanelWindow(QWidget):
    moved = Signal(QPoint)
    close_requested = Signal()
    retry_requested = Signal()
    direction_changed = Signal()
    settings_toggled = Signal()
    hotkey_recorded = Signal(str, str)   # (action, 表达式；""=清空停用)
    recording_active = Signal(bool)

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setWindowTitle("本地翻译")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(420, 370)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._direction_index = 0
        self._active_recorder: Optional[HotkeyRecorder] = None
        self._build_ui()

    def _build_ui(self) -> None:
        card = QFrame(self)
        card.setObjectName("card")
        card.setGeometry(8, 8, 404, 354)
        root = QVBoxLayout(card)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        header = DragHandle(card)
        header.setFixedHeight(30)
        header.moved.connect(self.moved)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        self.language = QPushButton("自动", header)
        language = self.language
        language.setObjectName("languagePill")
        language.setFixedSize(88, 24)
        language.setToolTip("切换翻译方向：自动 / 中译英 / 英译中")
        language.clicked.connect(self.direction_changed)
        title = QLabel("本地翻译", header)
        title.setObjectName("title")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        close_button = QPushButton("×", header)
        close_button.setObjectName("closeButton")
        close_button.setFixedSize(28, 28)
        close_button.clicked.connect(self.close_requested)
        self.pin_button = QPushButton("📌", header)
        self.pin_button.setObjectName("pinButton")
        self.pin_button.setFixedSize(28, 28)
        self.pin_button.setCheckable(True)
        self.pin_button.setToolTip("钉住面板：点击空白处不自动收起")
        self.gear_button = QPushButton("⚙", header)
        self.gear_button.setObjectName("gearButton")
        self.gear_button.setFixedSize(28, 28)
        self.gear_button.setToolTip("偏好设置：自定义全局快捷键")
        self.gear_button.clicked.connect(self.settings_toggled)
        header_layout.addWidget(language)
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        header_layout.addWidget(self.pin_button)
        header_layout.addWidget(self.gear_button)
        header_layout.addWidget(close_button)
        root.addWidget(header)

        self.editor_card = QFrame(card)
        self.editor_card.setObjectName("editorCard")
        self.editor_card.setFixedHeight(203)
        editor_layout = QVBoxLayout(self.editor_card)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)
        self.source = SourceEdit(self.editor_card)
        self.source.setObjectName("sourceText")
        self.source.setPlaceholderText("输入中文或英文…")
        self.source.setAcceptRichText(False)
        self.source.setFixedHeight(110)
        self.source.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.source.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        paragraph = QTextBlockFormat()
        paragraph.setLineHeight(128, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor = QTextCursor(self.source.document())
        cursor.setBlockFormat(paragraph)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.source.setTextCursor(cursor)
        separator = QFrame(self.editor_card)
        separator.setObjectName("separator")
        separator.setFixedHeight(1)
        self.output = QFrame(self.editor_card)
        self.output.setObjectName("output")
        self.output.setFixedHeight(90)
        output_layout = QVBoxLayout(self.output)
        output_layout.setContentsMargins(14, 10, 14, 10)
        output_layout.setSpacing(4)
        self.status_row = QWidget(self.output)
        status_layout = QHBoxLayout(self.status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(7)
        self.status_dot = QLabel(self.status_row)
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(7, 7)
        self.status_label = QLabel(self.status_row)
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.status_label.setWordWrap(True)
        self.retry = QPushButton("重试", self.status_row)
        self.retry.setObjectName("retryButton")
        self.retry.setFixedHeight(26)
        self.retry.setIcon(QIcon(str(ASSET_DIR / "retry.svg")))
        self.retry.setIconSize(QSize(13, 13))
        self.retry.clicked.connect(self.retry_requested)
        self.retry.hide()
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_label, 1)
        status_layout.addWidget(self.retry)
        self.translation_view = QTextEdit(self.output)
        self.translation_view.setObjectName("translationView")
        self.translation_view.setReadOnly(True)
        self.translation_view.setAcceptRichText(False)
        self.translation_view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.translation_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.translation_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.translation_view.setPlaceholderText("译文会显示在这里，可选择并复制")
        output_layout.addWidget(self.status_row)
        output_layout.addWidget(self.translation_view, 1)
        editor_layout.addWidget(self.source)
        editor_layout.addWidget(separator)
        editor_layout.addWidget(self.output)
        root.addWidget(self.editor_card)

        # ── 偏好设置视图（与编辑卡片同高，互斥显示） ────────────
        self.settings_card = QFrame(card)
        self.settings_card.setObjectName("editorCard")
        self.settings_card.setFixedHeight(203)
        settings_layout = QVBoxLayout(self.settings_card)
        settings_layout.setContentsMargins(16, 14, 16, 10)
        settings_layout.setSpacing(8)

        settings_title = QLabel("偏好设置", self.settings_card)
        settings_title.setObjectName("settingsTitle")
        settings_layout.addWidget(settings_title)

        def _hotkey_row(label_text: str, action: str) -> HotkeyRecorder:
            row = QHBoxLayout()
            row.setSpacing(10)
            label = QLabel(label_text, self.settings_card)
            label.setObjectName("settingsLabel")
            recorder = HotkeyRecorder(action, self.settings_card)
            recorder.hotkey_recorded.connect(
                lambda expr, a=action: self.hotkey_recorded.emit(a, expr)
            )
            recorder.recording_started.connect(
                lambda r=recorder: self._set_active_recorder(r)
            )
            recorder.recording_stopped.connect(lambda: self._set_active_recorder(None))
            row.addWidget(label)
            row.addStretch(1)
            row.addWidget(recorder)
            settings_layout.addLayout(row)
            return recorder

        self.translate_recorder = _hotkey_row("划词翻译", "translate")
        self.show_recorder = _hotkey_row("唤起面板", "show")
        self.ocr_recorder = _hotkey_row("识图翻译（OCR）", "ocr")

        self.hotkey_hint = QLabel(self.settings_card)
        self.hotkey_hint.setObjectName("conflictHint")
        self.hotkey_hint.setWordWrap(True)
        self.hotkey_hint.hide()
        settings_layout.addWidget(self.hotkey_hint)
        settings_layout.addStretch(1)

        settings_caption = QLabel("点击胶囊后按下新组合键；Esc 取消，Backspace 清空并停用", self.settings_card)
        settings_caption.setObjectName("settingsCaption")
        settings_layout.addWidget(settings_caption)

        self.settings_card.hide()
        root.addWidget(self.settings_card)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(10)
        self.autostart = PreciseCheckBox("开机自启", card)
        self.autostart.setChecked(is_autostart_enabled())
        self.autostart.stateChanged.connect(self._toggle_autostart)
        self.hint = QLabel("Ctrl+Enter 立即翻译", card)
        self.hint.setObjectName("hint")
        footer.addWidget(self.autostart)
        footer.addStretch(1)
        footer.addWidget(self.hint)
        root.addLayout(footer)

        self._status_effect = QGraphicsOpacityEffect(self.status_dot)
        self.status_dot.setGraphicsEffect(self._status_effect)
        self._status_animation = QPropertyAnimation(self._status_effect, b"opacity", self)
        self._status_animation.setDuration(1100)
        self._status_animation.setStartValue(0.35)
        self._status_animation.setKeyValueAt(0.5, 1.0)
        self._status_animation.setEndValue(0.35)
        self._status_animation.setLoopCount(-1)

        self.setStyleSheet(
            """
            QWidget { color: #1D1D1F; font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC', 'Segoe UI'; }
            QFrame#card { background: rgba(245,245,247,242); border: 1px solid rgba(229,229,234,215); border-radius: 16px; }
            QPushButton#languagePill { color: #6E6E73; background: #FFFFFF; border: 1px solid #EAEAEA; border-radius: 12px; font-size: 11px; }
            QPushButton#languagePill:hover { background: #F7F7F9; color: #1D1D1F; }
            QPushButton#languagePill:pressed { background: #ECECEF; }
            QLabel#title { color: #1D1D1F; font-size: 14px; font-weight: 700; }
            QPushButton#closeButton { color: #A1A1A6; background: transparent; border: 0; border-radius: 8px; font-size: 21px; }
            QPushButton#closeButton:hover { background: #E5E5EA; color: #6E6E73; }
            QFrame#editorCard { background: #FFFFFF; border: 1px solid #EAEAEA; border-radius: 12px; }
            QTextEdit#sourceText { background: #FFFFFF; color: #1D1D1F; border: 0; border-radius: 12px 12px 0 0; padding: 14px; font-size: 14px; selection-background-color: #D9D9DE; }
            QTextEdit#sourceText:focus { background: #FFFFFF; }
            QFrame#separator { background: #F0F0F2; }
            QFrame#output { background: #FFFFFF; border: 0; border-radius: 0 0 12px 12px; }
            QLabel#statusLabel, QLabel#hint { color: #86868B; font-size: 11px; }
            QLabel#statusDot { background: #A1A1A6; border-radius: 3px; }
            QTextEdit#translationView { background: #FFFFFF; color: #1D1D1F; border: 0; padding: 0; font-size: 13px; selection-background-color: #D9D9DE; }
            QTextEdit#translationView QScrollBar:vertical { width: 4px; background: transparent; }
            QTextEdit#translationView QScrollBar::handle:vertical { background: #D2D2D7; border-radius: 2px; }
            QCheckBox { color: #1D1D1F; font-size: 11px; spacing: 7px; }
            QPushButton#retryButton { color: #1D1D1F; background: #FFFFFF; border: 1px solid #D2D2D7; border-radius: 13px; padding: 0 10px; font-size: 11px; }
            QPushButton#retryButton:hover { background: #F0F0F2; border-color: #C7C7CC; }
            QPushButton#retryButton:pressed { padding-top: 2px; }
            QPushButton#pinButton, QPushButton#gearButton { color: #A1A1A6; background: transparent; border: 0; border-radius: 8px; font-size: 13px; }
            QPushButton#pinButton:hover, QPushButton#gearButton:hover { background: #E5E5EA; color: #6E6E73; }
            QPushButton#pinButton:checked { background: #EAF2FF; }
            QPushButton#gearButton[active="true"] { background: #EAF2FF; color: #0A84FF; }
            QPushButton#hotkeyPill { color: #1D1D1F; background: #FFFFFF; border: 1px solid #D2D2D7; border-radius: 13px; font-size: 11px; padding: 0 10px; }
            QPushButton#hotkeyPill:hover { border-color: #A1A1A6; }
            QPushButton#hotkeyPill[recording="true"] { background: #EAF2FF; border-color: #0A84FF; color: #0A84FF; }
            QLabel#settingsTitle { font-size: 13px; font-weight: 700; }
            QLabel#settingsLabel { color: #3A3A3C; font-size: 12px; }
            QLabel#settingsCaption { color: #A1A1A6; font-size: 10px; }
            QLabel#conflictHint { color: #C2554F; font-size: 11px; }
            """
        )

    autostart_toggled = Signal(bool)

    def _toggle_autostart(self, state: int) -> None:
        self.autostart_toggled.emit(bool(state))

    # ── 状态与译文 ─────────────────────────────────────────

    def set_status(self, message: str, error: bool = False) -> None:
        if error:
            message = FRIENDLY_ERROR
        self.retry.setVisible(error)
        self.status_label.setText(message)
        self.status_label.setStyleSheet("color: #D9534F;" if error else "color: #86868B;")
        self.status_dot.setStyleSheet("background: #D9534F; border-radius: 3px;" if error else "background: #A1A1A6; border-radius: 3px;")
        visible = bool(message)
        self.status_row.setVisible(visible)
        if visible and not error:
            self._status_animation.start()
        else:
            self._status_animation.stop()
        if error:
            self.translation_view.hide()
        else:
            self.translation_view.show()

    def set_translation(self, text: str) -> None:
        self.translation_view.setPlainText(text)

    def begin_translation_stream(self) -> None:
        self.translation_view.clear()
        self.translation_view.show()

    def append_translation_chunk(self, delta: str) -> None:
        self.translation_view.moveCursor(QTextCursor.MoveOperation.End)
        self.translation_view.insertPlainText(delta)
        self.translation_view.ensureCursorVisible()

    def set_direction_state(self, index: int) -> None:
        self._direction_index = index % len(DIRECTION_STATES)
        state = DIRECTION_STATES[self._direction_index]
        if state == "auto":
            self.language.setText("自动")
            self.source.setPlaceholderText("输入中文或英文…")
            self.language.setToolTip("当前方向：自动中英互译，点击切换")
        elif state == "zh-en":
            self.language.setText("中 → 英")
            self.source.setPlaceholderText("输入中文…")
            self.language.setToolTip("当前方向：中文翻译成英文，点击切换")
        else:
            self.language.setText("英 → 中")
            self.source.setPlaceholderText("Type English…")
            self.language.setToolTip("当前方向：英文翻译成中文，点击切换")
        self.source.setFocus()

    @property
    def direction_state(self) -> str:
        return DIRECTION_STATES[self._direction_index]

    def update_auto_direction_hint(self, text: str) -> None:
        """自动档下把当前判定方向透出在药丸上（自动 → 英 / 自动 → 中），消除方向困惑。"""
        if DIRECTION_STATES[self._direction_index] != "auto":
            return
        if not text.strip():
            self.language.setText("自动")
        elif _text_looks_chinese(text):
            self.language.setText("自动 → 中")
        else:
            self.language.setText("自动 → 英")

    # ── 视图切换 / 图钉 / 快捷键显示 ────────────────────────

    @property
    def pinned(self) -> bool:
        return self.pin_button.isChecked()

    def _set_active_recorder(self, recorder: Optional[HotkeyRecorder]) -> None:
        """记录正在录制的胶囊；面板级按键事件委托给它，避免焦点漂移导致录制失灵。"""
        self._active_recorder = recorder

    def keyPressEvent(self, event) -> None:
        if self._active_recorder is not None:
            self._active_recorder.keyPressEvent(event)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if self._active_recorder is not None:
            self._active_recorder.keyReleaseEvent(event)
            return
        super().keyReleaseEvent(event)

    @property
    def settings_visible(self) -> bool:
        return self.settings_card.isVisible()

    def show_settings_view(self, show: bool) -> None:
        self.settings_card.setVisible(show)
        self.editor_card.setVisible(not show)
        self.gear_button.setProperty("active", show)
        self.gear_button.style().unpolish(self.gear_button)
        self.gear_button.style().polish(self.gear_button)

    def set_hotkey_display(self, action: str, text: str) -> None:
        recorders = {
            "translate": self.translate_recorder,
            "show": self.show_recorder,
            "ocr": self.ocr_recorder,
        }
        recorders[action].set_display(text)

    def set_conflict_hint(self, message: str) -> None:
        if message:
            self.hotkey_hint.setText(message)
            self.hotkey_hint.show()
        else:
            self.hotkey_hint.hide()

    def cancel_recording(self) -> None:
        for recorder in (self.translate_recorder, self.ocr_recorder):
            recorder.cancel_recording()


class NativeTranslator:
    def __init__(self, app: QApplication) -> None:
        self.app = app

        self.settings = SettingsManager(ROOT)
        self.settings.load()
        self.settings.changed.connect(self.on_settings_changed)

        self.bubble = BubbleWindow(ASSET_DIR / "bubble-icon.svg")
        self.panel = PanelWindow()
        self.bubble.clicked.connect(self.toggle_panel)
        self.bubble.activate_requested.connect(self.open_panel)
        self.bubble.moved.connect(self.move_pair)
        self.bubble.quit_requested.connect(self.quit)
        self.bubble.open_requested.connect(self.open_panel)
        self.bubble.capture_requested.connect(self.start_capture)
        self.bubble.ocr_requested.connect(self.start_ocr)
        self.panel.moved.connect(self.move_pair)
        self.panel.close_requested.connect(self.hide_panel)
        self.panel.retry_requested.connect(self.retry_engine)
        self.panel.direction_changed.connect(self.on_direction_changed)
        self.panel.autostart_toggled.connect(self.on_autostart_toggled)
        self.panel.source.textChanged.connect(self.schedule_translation)
        self.panel.source.submit_requested.connect(self.start_translation)
        self.panel.settings_toggled.connect(self.toggle_settings_view)
        self.panel.hotkey_recorded.connect(self.on_hotkey_recorded)
        self.panel.recording_active.connect(self.on_recording_active)
        self.app.focusWindowChanged.connect(self.on_focus_window_changed)
        self.bubble.settings_requested.connect(self.open_settings)

        self.supervisor = EngineSupervisor(
            ROOT,
            port_provider=self.settings.server_port,
            logging_provider=lambda: bool(self.settings.get("logging_enabled")),
            idle_minutes_provider=lambda: int(self.settings.get("ai_idle_minutes", 15)),
        )
        self.supervisor.status_changed.connect(self.on_engine_status)
        self.supervisor.ready_changed.connect(self.on_engine_ready)
        self.supervisor.failed.connect(self.on_engine_failed)
        self.supervisor.idle_exited.connect(self.on_engine_idle)
        self.supervisor.process_died.connect(self.on_engine_died)

        self.hotkeys = HotkeyManager()
        self.app.installNativeEventFilter(self.hotkeys)
        self.hotkeys.triggered.connect(self.on_hotkey)
        self.apply_hotkeys()

        self.status_relay = StatusRelay()
        self.status_relay.message.connect(self.set_status)
        self.debounce = QTimer()
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(600)
        self.debounce.timeout.connect(self.start_translation)

        self.bootstrap: Optional[BootstrapThread] = None
        self.translation: Optional[TranslationStreamThread] = None
        self.ocr_thread: Optional[OcrTranslateThread] = None
        self.capture_thread: Optional[CaptureWorker] = None
        self.capture_overlay: Optional[ScreenCaptureOverlay] = None
        self.pending_source: Optional[str] = None
        self._capture_cursor = QPoint(0, 0)
        self.request_id = 0
        self.env_ready = False
        self.error_state = False
        self.closing = False
        self.device_info: dict = {}
        self._bubble_animation: Optional[QPropertyAnimation] = None
        self._panel_fade: Optional[QPropertyAnimation] = None
        self._panel_auto_hidden_at = 0.0
        self._recording_hotkey = False
        self._hotkey_status: dict = {}

        cleanup_legacy_autostart()
        self._sync_autostart(bool(self.settings.get("launch_at_login")), initial=True)
        self.panel.set_direction_state(0)

        self._place_initial()
        self.panel.hide()
        self.bubble.showNormal()
        self.bubble.raise_()
        self.bubble.activateWindow()
        self.app.processEvents()
        self.start_bootstrap()

    # ── 几何与气泡停靠 ───────────────────────────────────────

    def _screen(self):
        return self.app.primaryScreen().availableGeometry()

    def _place_initial(self) -> None:
        screen = self._screen()
        m = self.bubble.MARGIN
        visual_w = self.bubble.width() - 2 * m          # 气泡视觉宽度 88
        panel_x = screen.right() - self.panel.width() - visual_w - 16 - 22
        panel_y = screen.bottom() - self.panel.height() - 24
        panel_x = max(screen.left() + 8, panel_x)
        panel_y = max(screen.top() + 8, panel_y)
        self.panel.move(panel_x, panel_y)
        bubble_x = max(screen.left() + 8 - m, screen.right() - 18 - visual_w - m)
        bubble_y = max(screen.top() + 8 - m, screen.bottom() - 18 - visual_w - m)
        self.bubble_home = QPoint(bubble_x, bubble_y)
        self.bubble.move(self.bubble_home)

    def _bubble_target_for_panel(self) -> QPoint:
        """气泡视觉边缘停靠在面板右侧外部 16px 处（补偿窗口透明边距）。"""
        screen = self._screen()
        m = self.bubble.MARGIN
        x = self.panel.x() + self.panel.width() + 16 - m
        max_x = screen.right() - self.bubble.width() - 8
        x = min(x, max_x)
        y = self.panel.y() + self.panel.height() - self.bubble.height() - 10 + m
        y = max(screen.top() + 8 - m, min(y, screen.bottom() - self.bubble.height() - 8 + m))
        return QPoint(x, y)

    def _panel_target_for_bubble(self) -> QPoint:
        """从光标附近唤起时，面板贴在气泡左侧（视觉间距仍为 16px）。"""
        screen = self._screen()
        m = self.bubble.MARGIN
        x = self.bubble.x() - self.panel.width() - 16 + m
        y = self.bubble.y() + self.bubble.height() - self.panel.height() - m
        max_x = screen.right() - self.panel.width() - self.bubble.width() - 24 + m
        x = max(screen.left() + 8, min(x, max_x))
        y = max(screen.top() + 8, min(y, screen.bottom() - self.panel.height() - 8))
        return QPoint(x, y)

    def _animate_bubble(self, target: QPoint) -> None:
        if self._bubble_animation:
            try:
                self._bubble_animation.stop()
            except RuntimeError:
                pass
        animation = QPropertyAnimation(self.bubble, b"pos")
        animation.setParent(self.bubble)
        animation.setDuration(220)
        animation.setStartValue(self.bubble.pos())
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._bubble_animation = animation
        animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def move_pair(self, delta: QPoint) -> None:
        screen = self._screen()
        m = self.bubble.MARGIN
        if self.panel.isVisible():
            min_x = screen.left() + 8
            max_x = screen.right() - self.panel.width() - self.bubble.width() - 24 + m
            min_y = screen.top() + 8
            max_y = screen.bottom() - self.panel.height() - 8
            new_x = max(min_x, min(max_x, self.panel.x() + delta.x()))
            new_y = max(min_y, min(max_y, self.panel.y() + delta.y()))
            self.panel.move(new_x, new_y)
            self.bubble.move(self._bubble_target_for_panel())
            self.bubble_home = self.bubble.pos()
        else:
            self.bubble.move(self.bubble.pos() + delta)
            self.panel.move(self.panel.pos() + delta)
            self.bubble_home = self.bubble.pos()

    # ── 面板开合 ─────────────────────────────────────────────

    def toggle_panel(self) -> None:
        if self.panel.isVisible():
            self.hide_panel()
            return
        # 刚因失焦自动收起的 200ms 内忽略气泡展开点击（防"关了又开"闪烁）
        if time.monotonic() - self._panel_auto_hidden_at < 0.2:
            return
        self.open_panel()

    def open_panel(self) -> None:
        if self.panel.isVisible():
            self.panel.raise_()
            self.panel.activateWindow()
            return
        if self.pending_source:
            self.panel.move(self._panel_target_for_bubble())
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()
        self.bubble.raise_()
        dock_target = self._bubble_target_for_panel()
        self._animate_bubble(dock_target)
        self.bubble_home = dock_target
        self.panel.setWindowOpacity(0.0)
        animation = QPropertyAnimation(self.panel, b"windowOpacity", self.panel)
        animation.setDuration(180)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        if self.pending_source:
            QTimer.singleShot(0, self._begin_pending_translation)

    def hide_panel(self, animated: bool = True) -> None:
        """收起面板。仅 hide()，不清理任何输入/译文状态（保留翻译结果）。"""
        self.panel.cancel_recording()
        if not self.panel.isVisible():
            self._animate_bubble(self.bubble_home)
            return
        if not animated:
            self.panel.hide()
            self.panel.setWindowOpacity(1.0)
            self._animate_bubble(self.bubble_home)
            return
        fade = QPropertyAnimation(self.panel, b"windowOpacity", self.panel)
        fade.setDuration(140)
        fade.setStartValue(self.panel.windowOpacity())
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InQuad)
        fade.finished.connect(self._finish_panel_hide)
        self._panel_fade = fade
        fade.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _finish_panel_hide(self) -> None:
        self.panel.hide()
        self.panel.setWindowOpacity(1.0)
        self._animate_bubble(self.bubble_home)

    # ── 失焦自动收起（点击外部空白区域） ────────────────────

    def _our_focus_handles(self) -> set:
        handles = set()
        widgets = [self.panel, self.bubble]
        if self.capture_overlay is not None:
            widgets.append(self.capture_overlay)
        for widget in widgets:
            handle = widget.windowHandle()
            if handle is not None:
                handles.add(handle)
        return handles

    @Slot(object)
    def on_focus_window_changed(self, focus_window) -> None:
        """面板显示且未钉住时，焦点落到面板/气泡/截图层之外（桌面或其他
        软件，Qt 侧表现为 None）→ 平滑收起。状态保留，仅隐藏。"""
        if self.closing or not self.panel.isVisible() or self.panel.pinned:
            return
        if self._recording_hotkey:
            return
        if focus_window is not None and focus_window in self._our_focus_handles():
            return
        self._panel_auto_hidden_at = time.monotonic()
        self.hide_panel()

    # ── 状态展示 ─────────────────────────────────────────────

    def set_status(self, message: str, error: bool = False) -> None:
        self.panel.set_status(message, error)

    @Slot(str)
    def on_engine_status(self, raw: str) -> None:
        friendly = _friendly_engine_status(raw)
        if not friendly:
            return
        if friendly == FRIENDLY_ERROR:
            self.set_status(friendly, True)
        else:
            self.set_status(friendly)

    @Slot(bool, dict)
    def on_engine_ready(self, ready: bool, device: dict) -> None:
        if not ready:
            return
        self.device_info = device
        self.error_state = False
        text = self.panel.source.toPlainText().strip()
        if text and self.panel.isVisible():
            self.start_translation()

    @Slot(str)
    def on_engine_failed(self, message: str) -> None:
        self.error_state = True
        self.set_status(message if message == FRIENDLY_ERROR else FRIENDLY_ERROR, True)
        self.panel.retry.show()

    @Slot()
    def on_engine_idle(self) -> None:
        if self.panel.isVisible():
            self.set_status(IDLE_STATUS)

    @Slot()
    def on_engine_died(self) -> None:
        if self.panel.isVisible():
            self.set_status("引擎已休眠，翻译时自动唤醒")

    # ── 引导与环境准备 ────────────────────────────────────────

    def start_bootstrap(self) -> None:
        if getattr(sys, "frozen", False):
            # 打包态：依赖与 OCR 模型已内置于发布目录，直接拉起 ai_engine.exe
            self.env_ready = True
            self.supervisor.ensure_running()
            return
        if self.bootstrap and self.bootstrap.isRunning():
            return
        self.panel.retry.hide()
        self.set_status("正在准备本地 AI 引擎环境…")
        self.bootstrap = BootstrapThread()
        self.bootstrap.message.connect(self.on_bootstrap_message)
        self.bootstrap.succeeded.connect(self.on_bootstrap_succeeded)
        self.bootstrap.failed.connect(self.on_bootstrap_failed)
        self.bootstrap.start()

    def on_bootstrap_message(self, message: str) -> None:
        if message.startswith("⚠️"):
            self.set_status(FRIENDLY_ERROR, True)
        else:
            self.set_status(message)

    @Slot()
    def on_bootstrap_succeeded(self) -> None:
        self.env_ready = True
        self.supervisor.ensure_running()

    def on_bootstrap_failed(self, _message: str) -> None:
        self.error_state = True
        self.set_status(FRIENDLY_ERROR, True)
        self.panel.retry.show()

    def retry_engine(self) -> None:
        self.error_state = False
        self.panel.retry.hide()
        if not self.env_ready:
            self.start_bootstrap()
            return
        self.set_status("正在唤醒本地 GGUF 模型…")
        self.supervisor.restart()

    # ── 翻译流程 ─────────────────────────────────────────────

    def _target_lang(self) -> Optional[str]:
        return {"auto": None, "zh-en": "en", "en-zh": "zh"}.get(self.panel.direction_state)

    @Slot()
    def schedule_translation(self) -> None:
        self.request_id += 1
        text = self.panel.source.toPlainText().strip()
        if not text:
            self.debounce.stop()
            self.panel.set_translation("")
            self.panel.update_auto_direction_hint("")
            if self.supervisor.is_ready:
                self.set_status("")
            else:
                self.set_status("正在唤醒本地 GGUF 模型…")
            return
        self.panel.update_auto_direction_hint(text)
        if not self.supervisor.is_running:
            self.supervisor.ensure_running()
        self.set_status("翻译中…" if self.supervisor.is_ready else "正在唤醒本地 GGUF 模型…")
        self.debounce.start()

    def start_translation(self) -> None:
        text = self.panel.source.toPlainText().strip()
        if not text:
            return
        if not self.supervisor.is_running:
            self.supervisor.ensure_running()
            self.set_status("正在唤醒本地 GGUF 模型…")
            return
        if not self.supervisor.is_ready:
            self.set_status("正在唤醒本地 GGUF 模型…")
            return
        if self.translation and self.translation.isRunning():
            self.translation.stop()
        request_id = self.request_id
        self.panel.begin_translation_stream()
        self.set_status("翻译中…")
        self.supervisor.touch()
        self.translation = TranslationStreamThread(
            self.settings.base_url,
            text,
            self._target_lang(),
            request_id,
        )
        self.translation.chunk.connect(self.on_translation_chunk)
        self.translation.succeeded.connect(self.on_translation_succeeded)
        self.translation.failed.connect(self.on_translation_failed)
        self.translation.start()

    def _begin_pending_translation(self) -> None:
        text = self.pending_source
        self.pending_source = None
        if not text:
            return
        self.panel.source.blockSignals(True)
        self.panel.source.setPlainText(text)
        self.panel.source.blockSignals(False)
        self.request_id += 1
        self.start_translation()

    @Slot(int, str)
    def on_translation_chunk(self, request_id: int, delta: str) -> None:
        if request_id != self.request_id:
            return
        self.supervisor.touch()
        self.panel.append_translation_chunk(delta)

    @Slot(int, str)
    def on_translation_succeeded(self, request_id: int, full_text: str) -> None:
        if request_id != self.request_id:
            return
        self.panel.set_translation(full_text)
        self.set_status("")

    @Slot(int, str, bool)
    def on_translation_failed(self, request_id: int, message: str, engine_offline: bool) -> None:
        if request_id != self.request_id:
            return
        if engine_offline:
            self.supervisor.ensure_running()
            self.set_status(message)
        elif message == FRIENDLY_ERROR:
            self.error_state = True
            self.set_status(message, True)
            self.panel.retry.show()
        else:
            self.set_status(message)

    @Slot()
    def on_direction_changed(self) -> None:
        self.panel.set_direction_state(DIRECTION_STATES.index(self.panel.direction_state) + 1)
        self.request_id += 1
        self.debounce.stop()
        self.panel.set_translation("")
        text = self.panel.source.toPlainText().strip()
        if not text:
            self.panel.update_auto_direction_hint("")
            return
        self.panel.update_auto_direction_hint(text)
        # 切换方向后立即重译（不等 600ms 防抖），避免"切了没反应"的困惑
        self.start_translation()

    # ── 划词捕获 ─────────────────────────────────────────────

    @Slot(str)
    def on_hotkey(self, action: str) -> None:
        if action == "translate":
            self.start_capture()
        elif action == "ocr":
            self.start_ocr()
        elif action == "show":
            # 迅速唤起：面板显示中则收起，否则立即唤起（保留上次状态）
            if self.panel.isVisible():
                self.hide_panel()
            else:
                self.open_panel()

    def start_capture(self) -> None:
        if self.capture_thread and self.capture_thread.isRunning():
            return
        self._capture_cursor = get_cursor_pos()
        self.capture_thread = CaptureWorker()
        self.capture_thread.captured.connect(self.on_captured)
        self.capture_thread.start()

    def on_captured(self, text) -> None:
        if not text:
            if self.panel.isVisible():
                self.set_status("未能捕获选中文本")
            else:
                self.bubble.setToolTip("未能捕获选中文本")
                QTimer.singleShot(2500, lambda: self.bubble.setToolTip(BubbleWindow.BUBBLE_TIP))
            return
        self.pending_source = text
        # 面板与气泡平滑移动到光标附近（含屏幕边缘碰撞检测），自动展开并立即翻译
        self.panel.move(self._panel_target_for_cursor(self._capture_cursor))
        if self.panel.settings_visible:
            self.panel.show_settings_view(False)
        self.open_panel()
        self.bubble.pulse()

    def _panel_target_for_cursor(self, cursor: QPoint) -> QPoint:
        """划词唤起时面板出现在光标附近（屏幕边缘碰撞检测，保留气泡停靠空间）。"""
        screen = self._screen()
        m = self.bubble.MARGIN
        x = cursor.x() + 24
        y = cursor.y() - self.panel.height() - 24
        max_x = screen.right() - self.panel.width() - self.bubble.width() - 24 + m
        x = max(screen.left() + 8, min(x, max_x))
        y = max(screen.top() + 8, min(y, screen.bottom() - self.panel.height() - 8))
        return QPoint(x, y)

    # ── OCR 截图翻译 ─────────────────────────────────────────

    def start_ocr(self) -> None:
        if not bool(self.settings.get("ocr_enabled")):
            if self.panel.isVisible():
                self.set_status("截图翻译未开启，可在 settings.json 中设置 ocr_enabled=true")
            return
        if self.capture_overlay is not None:
            return
        if not self.supervisor.is_running:
            self.supervisor.ensure_running()
        if self.panel.isVisible():
            # 先收起面板，避免面板/气泡被截进快照
            self.hide_panel(animated=False)
        overlay = ScreenCaptureOverlay()
        overlay.region_selected.connect(self.on_region_selected)
        overlay.cancelled.connect(self.on_capture_cancelled)
        self.capture_overlay = overlay
        overlay.show()
        overlay.raise_()
        overlay.activateWindow()

    def on_capture_cancelled(self) -> None:
        if self.capture_overlay is not None:
            self.capture_overlay.close()
            self.capture_overlay = None

    def on_region_selected(self, rect: QRect) -> None:
        overlay = self.capture_overlay
        self.capture_overlay = None
        if overlay is None:
            return
        # 高分屏适配：快照像素尺寸 = 逻辑尺寸 × devicePixelRatio，
        # 选区是逻辑坐标，裁剪前必须等比换算，否则截图错位/模糊
        dpr = overlay.snapshot.devicePixelRatio() or 1.0
        device_rect = QRect(
            round(rect.x() * dpr),
            round(rect.y() * dpr),
            round(rect.width() * dpr),
            round(rect.height() * dpr),
        ).intersected(overlay.snapshot.rect())
        pixmap = overlay.snapshot.copy(device_rect)
        overlay.close()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buffer, "PNG")
        png_bytes = bytes(buffer.data())
        buffer.close()

        self.open_panel()
        self.panel.source.blockSignals(True)
        self.panel.source.setPlainText("[正在识别图片文字...]")
        self.panel.source.blockSignals(False)
        self.panel.set_translation("")
        self.set_status("正在识别截图文字…")
        self.ocr_thread = OcrTranslateThread(self.settings.base_url, png_bytes, self._target_lang())
        self.ocr_thread.succeeded.connect(self.on_ocr_done)
        self.ocr_thread.failed.connect(self.on_ocr_failed)
        self.ocr_thread.start()

    @Slot(str, str)
    def on_ocr_done(self, source_text: str, translation: str) -> None:
        self.panel.source.blockSignals(True)
        self.panel.source.setPlainText(source_text)
        self.panel.source.blockSignals(False)
        self.panel.set_translation(translation)
        self.set_status("")
        self.supervisor.touch()
        if not source_text:
            self.set_status("未识别到图片文字")

    @Slot(str, bool)
    def on_ocr_failed(self, message: str, engine_offline: bool) -> None:
        if engine_offline:
            self.supervisor.ensure_running()
        self.set_status(message)

    # ── 配置热加载 ────────────────────────────────────────────

    def apply_hotkeys(self) -> None:
        self._hotkey_status = self.hotkeys.reconfigure(
            str(self.settings.get("hotkey") or ""),
            str(self.settings.get("show_hotkey") or ""),
            str(self.settings.get("ocr_hotkey") or ""),
            bool(self.settings.get("ocr_enabled")),
        )
        failed = [action for action, ok in self._hotkey_status.items() if not ok]
        if failed:
            record_diagnostic(ROOT, "hotkey", "register-failed", RuntimeError(",".join(failed)))
        self._refresh_hotkey_displays()
        self._refresh_hotkey_hint()

    def _refresh_hotkey_displays(self) -> None:
        self.panel.set_hotkey_display(
            "translate", str(self.settings.get("hotkey_display") or "未设置")
        )
        self.panel.set_hotkey_display(
            "show", str(self.settings.get("show_hotkey_display") or "未设置")
        )
        self.panel.set_hotkey_display(
            "ocr", str(self.settings.get("ocr_hotkey_display") or "未设置")
        )

    def _refresh_hotkey_hint(self) -> None:
        names = {"translate": "划词翻译", "show": "唤起面板", "ocr": "识图翻译"}
        failed = [names.get(action, action) for action, ok in self._hotkey_status.items() if not ok]
        if failed:
            self.panel.set_conflict_hint("快捷键冲突，请更换组合：" + "、".join(failed))
        else:
            self.panel.set_conflict_hint("")

    @Slot()
    def toggle_settings_view(self) -> None:
        self.panel.show_settings_view(not self.panel.settings_visible)

    @Slot()
    def open_settings(self) -> None:
        if not self.panel.isVisible():
            self.open_panel()
        self.panel.show_settings_view(True)
        self.panel.raise_()
        self.panel.activateWindow()

    @Slot(bool)
    def on_recording_active(self, recording: bool) -> None:
        self._recording_hotkey = recording
        if recording:
            # 录制期间临时注销全局热键，避免按下新组合被全局拦截
            self.hotkeys.unregister_all()
        else:
            self.apply_hotkeys()

    @Slot(str, str)
    def on_hotkey_recorded(self, action: str, expr: str) -> None:
        expr = (expr or "").strip()
        key_map = {
            "translate": ("hotkey", "hotkey_display"),
            "show": ("show_hotkey", "show_hotkey_display"),
            "ocr": ("ocr_hotkey", "ocr_hotkey_display"),
        }
        key, display_key = key_map[action]
        updates = {
            key: expr,
            display_key: hotkey_display_from_expression(expr) or "未设置",
        }
        try:
            self.settings.save(updates)
        except OSError:
            self.panel.set_conflict_hint("配置写入失败，请重试")
            return
        # save 已抑制自身触发的热加载，这里手动重注册并刷新胶囊/提示
        self.apply_hotkeys()

    def on_settings_changed(self, diff: dict) -> None:
        if "launch_at_login" in diff:
            self._sync_autostart(bool(diff["launch_at_login"]))
        if any(key in diff for key in ("hotkey", "ocr_hotkey", "ocr_enabled")):
            self.apply_hotkeys()
        if "server_port" in diff or "acceleration_device" in diff:
            if self.supervisor.is_running:
                self.set_status("正在应用新配置，重启本地引擎…")
                self.supervisor.restart()

    def on_autostart_toggled(self, enabled: bool) -> None:
        self.settings.save({"launch_at_login": enabled})

    def _sync_autostart(self, enabled: bool, initial: bool = False) -> None:
        failed = False
        try:
            set_autostart(enabled)
        except OSError:
            failed = True
            enabled = is_autostart_enabled()
        self.panel.autostart.blockSignals(True)
        self.panel.autostart.setChecked(enabled)
        self.panel.autostart.blockSignals(False)
        if failed and not initial:
            self.set_status("开机自启设置失败，请检查权限")

    # ── 退出 ────────────────────────────────────────────────

    def quit(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.debounce.stop()
        for thread in (self.capture_thread, self.translation, self.ocr_thread, self.bootstrap):
            if thread and thread.isRunning():
                if hasattr(thread, "stop"):
                    thread.stop()
                thread.wait(1000)
        self.supervisor.stop(permanent=True)
        self.supervisor.join_shutdown(3.0)   # 等待后台 taskkill/清扫完成，防止孤儿引擎
        self.hotkeys.unregister_all()
        self.panel.close()
        self.bubble.close()
        self.app.quit()


def main() -> int:
    mutex = acquire_single_instance()
    if mutex is False:
        activate_running_instance()
        return 0
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    windows_font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
    if windows_font.exists():
        QFontDatabase.addApplicationFont(str(windows_font))
    app.setFont(QFont("Microsoft YaHei UI", 10))
    controller = NativeTranslator(app)
    app.aboutToQuit.connect(controller.quit)
    code = app.exec()
    if os.name == "nt" and mutex not in (True, False):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(mutex)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        try:
            (ROOT / "startup-error.log").write_text(str(exc), encoding="utf-8")
        finally:
            raise
