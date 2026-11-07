"""一次性工具：从 bubble-icon.svg 生成 assets/icon.ico（多尺寸）。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image

from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer


def render(size: int) -> Image.Image:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    # 圆角白色底板（深色任务栏/资源管理器上也有辨识度）
    path = QPainterPath()
    path.addRoundedRect(QRectF(1, 1, size - 2, size - 2), size * 0.22, size * 0.22)
    painter.fillPath(path, QColor(250, 250, 252, 255))
    renderer = QSvgRenderer(str(ROOT / "bubble-icon.svg"))
    inset = size * 0.10
    renderer.render(painter, QRectF(inset, inset, size - 2 * inset, size - 2 * inset))
    painter.end()
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombytes("RGBA", (size, size), image.bits().tobytes())


def main() -> int:
    app = QGuiApplication([])
    out_dir = ROOT / "assets"
    out_dir.mkdir(exist_ok=True)
    base = render(256)
    base.save(
        out_dir / "icon.ico",
        format="ICO",
        sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)],
    )
    print("icon.ico written:", (out_dir / "icon.ico").stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
