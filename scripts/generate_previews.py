"""生成 README 预览图：气泡 / 面板 / 桌面场景合成图。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageDraw, ImageFilter

from PySide6.QtWidgets import QApplication

from app import BubbleWindow, PanelWindow


def qt_to_pil(pixmap) -> Image.Image:
    image = pixmap.toImage().convertToFormat(
        __import__("PySide6").QtGui.QImage.Format.Format_RGBA8888
    )
    w, h = image.width(), image.height()
    ptr = image.constBits()
    return Image.frombytes("RGBA", (w, h), bytes(ptr[: w * h * 4]))


def paste_with_shadow(canvas: Image.Image, item: Image.Image, xy: tuple, blur: int = 14, alpha: int = 90) -> None:
    shadow = Image.new("RGBA", item.size, (30, 34, 48, 0))
    alpha_channel = item.split()[3].point(lambda a: min(a, alpha))
    shadow.putalpha(alpha_channel)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    sx, sy = xy[0] + 4, xy[1] + 10
    canvas.alpha_composite(shadow, (sx, sy))
    canvas.alpha_composite(item, xy)


def main() -> int:
    app = QApplication.instance() or QApplication([])

    # offscreen 平台不自动加载系统字体，显式注册微软雅黑保证中文渲染
    from PySide6.QtGui import QFont, QFontDatabase

    windows_font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
    if windows_font.exists():
        QFontDatabase.addApplicationFont(str(windows_font))
    app.setFont(QFont("Microsoft YaHei UI", 10))

    out_dir = ROOT / "images"
    out_dir.mkdir(exist_ok=True)

    # ① 悬浮气泡（新图标）
    bubble = BubbleWindow(ROOT / "bubble-icon.svg")
    bubble.show()
    app.processEvents()
    bubble_img = qt_to_pil(bubble.grab())
    big = bubble_img.resize((bubble_img.width * 3, bubble_img.height * 3), Image.LANCZOS)
    canvas = Image.new("RGBA", (big.width + 120, big.height + 120), (0, 0, 0, 0))
    paste_with_shadow(canvas, big, (60, 50), blur=18, alpha=110)
    canvas.save(out_dir / "bubble.png")
    print("bubble.png", canvas.size)

    # ② 翻译面板（示例内容）
    panel = PanelWindow()
    panel.show()
    app.processEvents()
    panel.source.blockSignals(True)
    panel.source.setPlainText("Hello world! LocalTranslateBubble runs a GGUF model fully offline on your PC.")
    panel.source.blockSignals(False)
    panel.begin_translation_stream()
    panel.append_translation_chunk("你好世界！LocalTranslateBubble 在你的电脑上完全离线运行 GGUF 模型。")
    panel.set_status("")
    app.processEvents()
    panel_img = qt_to_pil(panel.grab())
    panel_img.save(out_dir / "panel.png")
    print("panel.png", panel_img.size)

    # ③ 桌面场景合成图（hero）：面板 + 停靠气泡
    scale = 1.6
    pw, ph = int(panel_img.width * scale), int(panel_img.height * scale)
    panel_big = panel_img.resize((pw, ph), Image.LANCZOS)
    bw = int(bubble_img.width * 1.2)
    bubble_big = bubble_img.resize((bw, bw), Image.LANCZOS)

    margin_x, margin_y = 150, 110
    dock_gap = 16
    canvas_w = margin_x * 2 + pw + dock_gap + 24 + bw
    canvas_h = margin_y * 2 + ph
    hero = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    # 柔和桌面渐变背景
    grad = Image.new("RGBA", (canvas_w, canvas_h))
    gd = ImageDraw.Draw(grad)
    top, bottom = (226, 232, 240, 255), (245, 245, 247, 255)
    for y in range(canvas_h):
        t = y / max(1, canvas_h - 1)
        gd.line([(0, y), (canvas_w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    hero.alpha_composite(grad)

    panel_xy = (margin_x, margin_y)
    paste_with_shadow(hero, panel_big, panel_xy, blur=20, alpha=110)
    bubble_x = margin_x + pw + dock_gap - 12
    bubble_y = margin_y + ph - int(110 * scale) + int(12 * scale)
    paste_with_shadow(hero, bubble_big, (bubble_x, bubble_y), blur=10, alpha=110)
    hero.convert("RGB").save(out_dir / "preview.png", quality=95)
    print("preview.png", hero.size)

    bubble.close()
    panel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
