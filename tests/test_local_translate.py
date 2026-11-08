from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bubble import app as ui
from bubble.hotkey_manager import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    parse_hotkey,
)
from bubble.settings_manager import DEFAULTS, SettingsManager


class MockPropertyMock:
    """instance 属性访问代理到类级 callable（用于 mock property）。"""

    def __init__(self, return_value=None):
        self._return_value = return_value

    def __call__(self, _instance=None):
        return self._return_value


class SettingsManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_when_file_missing(self):
        manager = SettingsManager(self.root)
        snapshot = manager.load()
        self.assertEqual(snapshot["server_port"], DEFAULTS["server_port"])
        self.assertEqual(snapshot["acceleration_device"], "auto")
        self.assertEqual(snapshot["hotkey"], "<alt>+q")
        self.assertEqual(snapshot["hotkey_display"], "Alt+Q")
        self.assertEqual(snapshot["ocr_hotkey"], "<alt>+w")
        self.assertEqual(snapshot["ai_idle_minutes"], DEFAULTS["ai_idle_minutes"])
        self.assertTrue(snapshot["ocr_enabled"])

    def test_explicit_empty_hotkey_is_preserved(self):
        (self.root / "settings.json").write_text(
            json.dumps({"hotkey": "", "ocr_hotkey": ""}),
            encoding="utf-8",
        )
        manager = SettingsManager(self.root)
        snapshot = manager.load()
        self.assertEqual(snapshot["hotkey"], "")
        self.assertEqual(snapshot["hotkey_display"], "未设置")
        self.assertEqual(snapshot["ocr_hotkey"], "")

    def test_save_writes_poptrans_compatible_fields(self):
        manager = SettingsManager(self.root)
        manager.load()
        manager.save({"server_port": 9999, "launch_at_login": True})
        data = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["server_port"], 9999)
        self.assertTrue(data["launch_at_login"])
        self.assertIn("acceleration_device", data)
        self.assertIn("hotkey_display", data)

    def test_invalid_values_fall_back_to_defaults(self):
        (self.root / "settings.json").write_text(
            json.dumps(
                {
                    "server_port": 80,
                    "acceleration_device": "tpu",
                    "ai_idle_minutes": -3,
                    "theme": "solarized",
                }
            ),
            encoding="utf-8",
        )
        manager = SettingsManager(self.root)
        snapshot = manager.load()
        self.assertEqual(snapshot["server_port"], DEFAULTS["server_port"])
        self.assertEqual(snapshot["acceleration_device"], "auto")
        self.assertEqual(snapshot["ai_idle_minutes"], DEFAULTS["ai_idle_minutes"])
        self.assertEqual(snapshot["theme"], "system")

    def test_external_change_emits_diff(self):
        manager = SettingsManager(self.root)
        manager.load()
        events: list[dict] = []
        manager.changed.connect(events.append)
        payload = dict(DEFAULTS)
        payload["acceleration_device"] = "gpu"
        manager._suppress_reload = False
        (self.root / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
        # QFileSystemWatcher 依赖事件循环；直接调用内部槽验证行为
        manager._on_file_changed(str(self.root / "settings.json"))
        self.assertEqual(events and events[-1].get("acceleration_device"), "gpu")

    def test_file_creation_triggers_reload(self):
        # 首次运行时 settings.json 不存在；外部新建文件也必须触发热加载
        manager = SettingsManager(self.root)
        manager.load()
        events: list[dict] = []
        manager.changed.connect(events.append)
        payload = dict(DEFAULTS)
        payload["ai_idle_minutes"] = 7
        (self.root / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
        manager._on_directory_changed(str(self.root))
        self.assertEqual(events and events[-1].get("ai_idle_minutes"), 7)


class HotkeyParseTests(unittest.TestCase):
    def test_poptrans_expression_formats(self):
        self.assertEqual(parse_hotkey("<ctrl>+<alt>+q"), (MOD_CONTROL | MOD_ALT, ord("Q")))
        self.assertEqual(parse_hotkey("ctrl+alt+e"), (MOD_CONTROL | MOD_ALT, ord("E")))
        self.assertEqual(parse_hotkey("<ctrl>+<shift>+f12"), (MOD_CONTROL | MOD_SHIFT, 0x70 + 12 - 1))

    def test_invalid_expressions_return_none(self):
        self.assertIsNone(parse_hotkey("q"))            # 缺少修饰键
        self.assertIsNone(parse_hotkey("ctrl+"))        # 缺少主键
        self.assertIsNone(parse_hotkey("ctrl+pause"))   # 不支持的主键


class EngineStatusMappingTests(unittest.TestCase):
    def test_status_mapping_is_friendly(self):
        cases = {
            "正在加载翻译模型...": "正在唤醒本地 GGUF 模型…",
            "翻译引擎就绪 (GPU 加速)": "GPU 加速已就绪",
            "翻译引擎就绪 (CPU)": "CPU 模式已就绪",
            "翻译引擎就绪 (CPU 降级)": "GPU 加载失败，已自动切换 CPU 模式",
            "首次使用需下载 Hy-MT2 模型（约 1.13GB）...": "首次使用，正在下载 Hy-MT2 翻译模型（约 1.13GB）…",
        }
        for raw, expected in cases.items():
            self.assertEqual(ui._friendly_engine_status(raw), expected)

    def test_init_failure_maps_to_friendly_error(self):
        self.assertEqual(
            ui._friendly_engine_status("翻译引擎初始化失败: something"),
            ui.FRIENDLY_ERROR,
        )


class MultipartBodyTests(unittest.TestCase):
    def test_body_contains_file_and_optional_lang(self):
        body, content_type = ui._multipart_body(b"PNGDATA", "en")
        self.assertIn(b'name="file"', body)
        self.assertIn(b"PNGDATA", body)
        self.assertIn(b'target_lang"', body)
        self.assertIn(b"en", body)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))

        body_no_lang, _ = ui._multipart_body(b"PNGDATA", None)
        self.assertNotIn(b"target_lang", body_no_lang)


class NativeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 不在测试机全局注册热键（可能与运行中的实例/其他软件冲突）
        with patch.object(ui.NativeTranslator, "start_bootstrap", lambda *a, **k: None), \
                patch.object(ui, "cleanup_legacy_autostart", lambda: None), \
                patch.object(ui, "set_autostart", lambda _enabled: None), \
                patch.object(ui, "is_autostart_enabled", lambda: False), \
                patch.object(ui.HotkeyManager, "reconfigure", lambda self, *a, **k: {"translate": True, "show": True, "ocr": True}):
            self.controller = ui.NativeTranslator(self.qt_app)

    def tearDown(self):
        self.controller.hotkeys.unregister_all()
        self.controller.panel.close()
        self.controller.bubble.close()
        QTest.qWait(250)

    def test_split_card_retry_and_friendly_error(self):
        panel = self.controller.panel
        self.assertEqual(panel.source.height(), 110)
        self.assertEqual(panel.output.height(), 90)
        self.assertIs(panel.retry.parent(), panel.status_row)
        self.assertTrue(panel.translation_view.isReadOnly())

        panel.set_status("raw command error", error=True)
        self.assertEqual(panel.status_label.text(), ui.FRIENDLY_ERROR)
        self.assertIn("#D9534F", panel.status_label.styleSheet())
        self.assertFalse(panel.retry.isHidden())

    def test_streaming_append_and_clear(self):
        panel = self.controller.panel
        panel.begin_translation_stream()
        panel.set_status("翻译中…")
        panel.append_translation_chunk("你好")
        panel.append_translation_chunk("，世界")
        self.assertEqual(panel.translation_view.toPlainText(), "你好，世界")

    def test_visual_requirements_remain_in_place(self):
        controller = self.controller
        panel = controller.panel
        stylesheet = panel.styleSheet()
        for rule in (
            "rgba(245,245,247,242)",
            "#FFFFFF; border: 1px solid #EAEAEA; border-radius: 12px",
            "padding: 14px",
            "Microsoft YaHei UI",
        ):
            self.assertIn(rule, stylesheet)
        self.assertEqual(panel.hint.text(), "Ctrl+Enter 立即翻译")
        self.assertIsInstance(panel.autostart, ui.PreciseCheckBox)
        # 高刷动画驱动：8ms 精确定时器（≈120 FPS）
        self.assertEqual(controller.bubble._anim_timer.interval(), ui.BubbleWindow.TICK_MS)
        self.assertEqual(controller.bubble._anim_timer.timerType(), Qt.TimerType.PreciseTimer)
        # 预渲染缓存：Logo/圆底/光晕在初始化时合成，paintEvent 仅贴图
        self.assertTrue(controller.bubble._base_pix.width() > 0)
        self.assertTrue(controller.bubble._halo_pix.width() > 0)
        self.assertEqual(controller.bubble.windowTitle(), "本地翻译悬浮球")

        panel.set_translation("Hello")
        self.assertEqual(panel.translation_view.toPlainText(), "Hello")
        self.assertTrue(panel.translation_view.textInteractionFlags())

        checkbox = ui.PreciseCheckBox("开机自启")
        checkbox.resize(130, 24)
        checkbox.setChecked(True)
        checkbox.show()
        self.qt_app.processEvents()
        checkbox_image = checkbox.grab().toImage()
        self.assertEqual(checkbox_image.pixelColor(14, 12).name(), "#333336")
        checkbox.close()

        image = controller.bubble.grab().toImage()
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

    def test_direction_pill_cycles_three_states(self):
        panel = self.controller.panel
        self.assertEqual(panel.language.text(), "自动")
        controller = self.controller
        controller.on_direction_changed()
        self.assertEqual(panel.language.text(), "中 → 英")
        self.assertEqual(controller._target_lang(), "en")
        controller.on_direction_changed()
        self.assertEqual(panel.language.text(), "英 → 中")
        self.assertEqual(controller._target_lang(), "zh")
        controller.on_direction_changed()
        self.assertEqual(panel.language.text(), "自动")
        self.assertIsNone(controller._target_lang())

    def test_bubble_docks_outside_panel_and_returns(self):
        controller = self.controller
        margin = ui.BubbleWindow.MARGIN
        controller.toggle_panel()
        QTest.qWait(260)
        # 视觉边缘停靠在面板右侧外部 16px（窗口含透明边距，需补偿）
        self.assertEqual(
            controller.bubble.x(),
            controller.panel.x() + controller.panel.width() + 16 - margin,
        )
        visual = controller.bubble.geometry().adjusted(margin, margin, -margin, -margin)
        self.assertFalse(controller.panel.frameGeometry().intersects(visual))
        controller.hide_panel()
        QTest.qWait(260)
        self.assertFalse(controller.panel.isVisible())
        self.assertEqual(controller.bubble.pos(), controller.bubble_home)

    def test_capture_auto_opens_panel_near_cursor(self):
        controller = self.controller
        controller._capture_cursor = QPoint(300, 300)
        with patch.object(controller, "start_translation", lambda *a, **k: None):
            controller.on_captured("hello world")
            QTest.qWait(300)
        # 自动展开面板、填入划词文本并立即开始翻译
        self.assertTrue(controller.panel.isVisible())
        self.assertEqual(controller.panel.source.toPlainText(), "hello world")
        self.assertIsNone(controller.pending_source)
        # 气泡停靠在面板右侧外部 16px（视觉间距）
        self.assertEqual(
            controller.bubble.x(),
            controller.panel.x() + controller.panel.width() + 16 - ui.BubbleWindow.MARGIN,
        )

    def test_focus_loss_auto_hides_but_keeps_state(self):
        controller = self.controller
        controller.toggle_panel()
        QTest.qWait(300)
        controller.panel.source.blockSignals(True)
        controller.panel.source.setPlainText("保留文本")
        controller.panel.source.blockSignals(False)
        controller.on_focus_window_changed(None)
        QTest.qWait(250)
        self.assertFalse(controller.panel.isVisible())
        # 隐藏不清空：输入与译文状态保留
        self.assertEqual(controller.panel.source.toPlainText(), "保留文本")
        # 钉住后点击外部不再自动收起
        controller.panel.show()
        controller.panel.setWindowOpacity(1.0)
        controller.panel.pin_button.setChecked(True)
        controller.on_focus_window_changed(None)
        QTest.qWait(250)
        self.assertTrue(controller.panel.isVisible())
        controller.panel.pin_button.setChecked(False)

    def test_bubble_click_debounce_after_autohide(self):
        controller = self.controller
        controller.toggle_panel()
        QTest.qWait(300)
        # 真实时序：点击气泡 → 面板失焦自动收起 → 气泡 mouseRelease 紧随其后（<200ms）
        controller.on_focus_window_changed(None)
        controller.toggle_panel()   # 防抖窗口内：忽略展开点击，防止"关了又开"闪烁
        QTest.qWait(250)
        self.assertFalse(controller.panel.isVisible())
        controller._panel_auto_hidden_at = time.monotonic() - 0.5
        controller.toggle_panel()   # 防抖窗口外：正常展开
        QTest.qWait(300)
        self.assertTrue(controller.panel.isVisible())

    def test_settings_view_toggle_and_recorder_display(self):
        panel = self.controller.panel
        panel.show()
        self.qt_app.processEvents()
        self.assertTrue(panel.editor_card.isVisible())
        panel.show_settings_view(True)
        self.qt_app.processEvents()
        self.assertTrue(panel.settings_visible)
        self.assertTrue(panel.settings_card.isVisible())
        self.assertFalse(panel.editor_card.isVisible())
        self.assertEqual(panel.translate_recorder.text(), "Alt+Q")
        self.assertEqual(panel.ocr_recorder.text(), "Alt+W")
        panel.show_settings_view(False)
        self.qt_app.processEvents()
        self.assertTrue(panel.editor_card.isVisible())

    def test_hotkey_recorder_commit_and_conflict_hint(self):
        controller = self.controller
        saved: dict = {}

        def fake_save(updates):
            saved.update(updates)
            return controller.settings.snapshot()

        with patch.object(controller.settings, "save", side_effect=fake_save), \
                patch.object(controller.hotkeys, "reconfigure",
                             return_value={"translate": True, "show": True, "ocr": True}):
            controller.on_hotkey_recorded("translate", "<alt>+q")
        self.assertEqual(saved.get("hotkey"), "<alt>+q")
        self.assertEqual(saved.get("hotkey_display"), "Alt+Q")
        self.assertEqual(controller.panel.translate_recorder.text(), "Alt+Q")
        self.assertTrue(controller.panel.hotkey_hint.isHidden())

        with patch.object(controller.settings, "save", side_effect=fake_save), \
                patch.object(controller.hotkeys, "reconfigure",
                             return_value={"translate": True, "show": True, "ocr": True}):
            controller.on_hotkey_recorded("show", "<alt>+b")
        self.assertEqual(saved.get("show_hotkey"), "<alt>+b")
        self.assertEqual(saved.get("show_hotkey_display"), "Alt+B")
        self.assertEqual(controller.panel.show_recorder.text(), "Alt+B")

        with patch.object(controller.settings, "save", side_effect=fake_save), \
                patch.object(controller.hotkeys, "reconfigure",
                             return_value={"translate": True, "show": True, "ocr": False}):
            controller.on_hotkey_recorded("ocr", "<ctrl>+<shift>+s")
        self.assertFalse(controller.panel.hotkey_hint.isHidden())
        self.assertIn("快捷键冲突，请更换组合", controller.panel.hotkey_hint.text())

    def test_hotkey_reconfigure_supports_empty_disable(self):
        manager = ui.HotkeyManager()
        result = manager.reconfigure("", "", "", True)
        self.assertTrue(result["translate"])
        self.assertTrue(result["show"])
        self.assertEqual(manager._bindings, {})
        manager.unregister_all()

    def test_settings_hot_reload_restarts_engine(self):
        controller = self.controller
        controller.env_ready = True
        with patch.object(
            ui.EngineSupervisor, "is_running", new_callable=MockPropertyMock, return_value=True
        ), patch.object(controller.supervisor, "restart") as restart:
            controller.on_settings_changed({"acceleration_device": "gpu"})
            restart.assert_called_once()
            controller.on_settings_changed({"hotkey": "<ctrl>+<alt>+t"})
            restart.assert_called_once()  # 快捷键变更不触发引擎重启


if __name__ == "__main__":
    unittest.main()
