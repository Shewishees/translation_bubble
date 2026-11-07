"""OCR 截图框选 — 对齐 PopTrans 的屏幕截图交互。

采用"先快照、后框选"的方式：弹出覆盖层前先抓取整屏快照，用户在快照上
框选（Esc 取消），返回选区矩形后由控制器直接从快照裁剪——避免把覆盖层
本身截进图片。识别与翻译通过 /api/v1/ocr_translate 完成（api_server.py）。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

MIN_SELECTION_SIZE = 10
DIM_COLOR = QColor(0, 0, 0, 110)
BORDER_COLOR = QColor("#0A84FF")


class ScreenCaptureOverlay(QWidget):
    """全屏快照框选覆盖层。region_selected 携带选区（覆盖层坐标 = 屏幕坐标）。"""

    region_selected = Signal(QRect)
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setWindowTitle("截图翻译")
        screen = QApplication.primaryScreen()
        self._screen_geometry = screen.geometry()
        self.snapshot: QPixmap = screen.grabWindow(0)
        self.setGeometry(self._screen_geometry)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._origin: Optional[QPoint] = None
        self._current: Optional[QPoint] = None
        self._drag_distance = 0

    # ── 交互 ──────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self._drag_distance = 0
            event.accept()
            self.update()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._origin is not None:
            self._current = event.position().toPoint()
            delta = self._current - self._origin
            self._drag_distance = max(abs(delta.x()), abs(delta.y()))
            event.accept()
            self.update()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._origin is not None:
            rect = QRect(self._origin, event.position().toPoint()).normalized()
            self._origin = None
            self._current = None
            if (
                rect.width() >= MIN_SELECTION_SIZE
                and rect.height() >= MIN_SELECTION_SIZE
                and self._drag_distance >= MIN_SELECTION_SIZE
            ):
                self.region_selected.emit(rect)
            else:
                self.cancelled.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    # ── 绘制 ──────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.snapshot)
        painter.fillRect(self.rect(), DIM_COLOR)

        if self._origin is not None and self._current is not None:
            rect = QRect(self._origin, self._current).normalized().intersected(self.rect())
            if rect.width() > 0 and rect.height() > 0:
                # 选区内还原快照原亮度，形成"挖空"效果
                painter.drawPixmap(rect, self.snapshot.copy(rect))
            pen = QPen(BORDER_COLOR, 1.5)
            painter.setPen(pen)
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            painter.setPen(QColor("#FFFFFF"))
            painter.setFont(QFont("Microsoft YaHei UI", 9))
            label = f"{rect.width()} × {rect.height()}  ·  松开完成，Esc 取消"
            painter.drawText(
                QRect(rect.left(), max(4, rect.top() - 22), rect.width(), 18),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
        painter.end()


def grab_screen_region(rect: QRect) -> Optional[QPixmap]:
    """从当前主屏截取指定区域（用于面板内手动触发的小范围截图）。"""
    screen = QApplication.primaryScreen()
    return screen.grabWindow(
        0, rect.x(), rect.y(), rect.width(), rect.height()
    )
